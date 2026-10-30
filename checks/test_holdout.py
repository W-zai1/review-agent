#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_holdout.py —— holdout 验收器的公开回归锁。

测什么：
  1. Wilson 95% 区间与已知值一致（允许 0.1 个百分点误差）；
  2. 空 holdout 必须退出 2，不能静默打 0 分；
  3. --save-manifest 后正常运行通过；
  4. 改一个 case 的一个字 → 退出 2（sha256 不符）；
  5. 清单外新增 case → 退出 2；
  6. 缺 label 的干净 case 计入 clean，且不产生误报；
  7. 门槛不达标 → 退出 1。

所有用例都在系统 tempfile 临时目录里造，不依赖真实 holdout/ 内容。仓库外的样本
会由评测器复制到 outputs/ 下的临时快照（仅为了满足 review.run_one 的路径约束），
用例结束即清理，不会写正式报告。
跑法：
    python -m unittest discover -s checks -v
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import holdout_eval  # noqa: E402

# 与 tests/golden/cases/c14_internal_progress.txt 同文；金标准已锁定它是干净文本。
CLEAN_TEXT = (
    "本周项目进展：后端接口联调完成 8/10，前端页面进入内测。"
    "当前无阻塞项，风险可控。\n"
)

# 真实审查用例要按 agent 定义跑 review.run_one；agents/ 是本地私有资产
# （公开 clone 不含，见 .gitignore），缺失时跳过这些用例，其余 CLI 行为用例照常。
HAS_AGENTS = (ROOT / "agents").is_dir()
needs_agent = unittest.skipUnless(
    HAS_AGENTS, "agents/ 不在仓库（公开 clone 不含 Agent 定义），跳过真实审查用例")
DEFAULT_HOLDOUT = holdout_eval.HOLDOUT
DEFAULT_OUT = holdout_eval.OUT_DIR


class HoldoutTestBase(unittest.TestCase):
    """每个测试一份临时 holdout，并注入到模块路径常量。"""

    def setUp(self):
        # 故意放在系统临时目录（仓库外），验证评测器不要求 holdout 必须在仓库内。
        self._tmp = tempfile.TemporaryDirectory(prefix="holdout-test-")
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.holdout = base / "holdout"
        (self.holdout / "cases").mkdir(parents=True)
        (self.holdout / "labels").mkdir()
        self.out_dir = base / "outputs"
        holdout_eval.configure(self.holdout, self.out_dir)
        # https://docs.python.org/3/library/unittest.html#unittest.TestCase.addCleanup
        # 后注册先执行：先恢复路径，再删临时目录。
        self.addCleanup(holdout_eval.configure, DEFAULT_HOLDOUT, DEFAULT_OUT)

    # ---- helpers ----
    def write_case(self, name, text=CLEAN_TEXT):
        path = self.holdout / "cases" / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_labels(self, name, errors):
        path = self.holdout / "labels" / name
        path.write_text(
            json.dumps({"errors": errors}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = holdout_eval.main(list(args))
        return code, buf.getvalue()

    def freeze(self):
        code, out = self.run_cli("--save-manifest")
        self.assertEqual(code, 0, out)
        self.assertIn("已冻结", out)
        return json.loads((self.holdout / "MANIFEST.json").read_text(encoding="utf-8"))

    def result_path(self):
        return Path(self._tmp.name) / "result.json"

    def read_result(self):
        return json.loads(self.result_path().read_text(encoding="utf-8"))


class WilsonIntervalTests(unittest.TestCase):
    """已知值来自标准 Wilson score interval（z=1.959964）。"""

    def assert_ci(self, k, n, low_pct, high_pct):
        low, high = holdout_eval.wilson_interval(k, n)
        self.assertAlmostEqual(low * 100.0, low_pct, delta=0.1,
                               msg=f"{k}/{n} lower={low * 100:.4f}")
        self.assertAlmostEqual(high * 100.0, high_pct, delta=0.1,
                               msg=f"{k}/{n} upper={high * 100:.4f}")

    def test_zero_of_ten(self):
        # 0/10 的 Wilson 上限约 27.75%，不是朴素的 0%
        self.assert_ci(0, 10, 0.0, 27.75)

    def test_ten_of_ten(self):
        # 10/10 的 Wilson 下限约 72.25%，不是朴素的 100%
        self.assert_ci(10, 10, 72.25, 100.0)

    def test_five_of_ten(self):
        self.assert_ci(5, 10, 23.66, 76.34)

    def test_zero_denominator_is_zero_interval(self):
        self.assertEqual(holdout_eval.wilson_interval(0, 0), (0.0, 0.0))
        block = holdout_eval.rate_block(0, 0)
        self.assertIsNone(block["pct"])
        self.assertEqual(block["ci95_pct"], [0.0, 0.0])


class SummarizeTests(unittest.TestCase):
    """标注级 precision：labels_hit / (labels_hit + fp)，并保留 k/n。"""

    def test_totals_and_precision_denominator(self):
        rows = [
            {"case": "a.txt", "kind": "positive", "labels_total": 2,
             "labels_hit": 1, "fp": 1, "issue_count": 3},
            {"case": "b.txt", "kind": "positive", "labels_total": 1,
             "labels_hit": 1, "fp": 0, "issue_count": 1},
            {"case": "c.txt", "kind": "clean", "labels_total": 0,
             "labels_hit": 0, "fp": 2, "issue_count": 2},
        ]
        metrics = holdout_eval.summarize(rows)
        self.assertEqual(metrics["cases"], 3)
        self.assertEqual(metrics["positive_cases"], 2)
        self.assertEqual(metrics["clean_cases"], 1)
        self.assertEqual(metrics["labels_total"], 3)
        self.assertEqual(metrics["labels_hit"], 2)
        self.assertEqual(metrics["fp_total"], 3)
        self.assertEqual(metrics["issue_count"], 6)
        self.assertEqual(metrics["recall"]["k"], 2)
        self.assertEqual(metrics["recall"]["n"], 3)
        self.assertAlmostEqual(metrics["recall"]["pct"], 66.7, delta=0.1)
        # 2 次命中 /（2 次命中 + 3 条标注外 issue）
        self.assertEqual(metrics["precision"]["k"], 2)
        self.assertEqual(metrics["precision"]["n"], 5)
        self.assertAlmostEqual(metrics["precision"]["pct"], 40.0, delta=0.1)


class HoldoutCliTests(HoldoutTestBase):

    def test_empty_cases_exits_2_with_actionable_message(self):
        code, out = self.run_cli()
        self.assertEqual(code, 2, out)
        self.assertIn("没有真实 holdout 文档", out)
        self.assertIn("holdout/README.md", out)
        self.assertIn("30–50", out)

    def test_missing_manifest_exits_2(self):
        self.write_case("clean.txt")
        code, out = self.run_cli()
        self.assertEqual(code, 2, out)
        self.assertIn("缺少冻结清单", out)
        self.assertIn("--save-manifest", out)

    @needs_agent
    def test_save_manifest_then_run_passes(self):
        self.write_case("clean.txt")
        self.write_labels("clean.labels.json", [{
            "id": "L1",
            "desc": "占位标注：用于验证清单包含 labels 文件",
            "match": [["不会命中的占位符"]],
            "exclude_dimensions": ["style"],
        }])
        manifest = self.freeze()

        self.assertEqual(manifest["count"], 1)
        self.assertEqual(manifest["label_count"], 1)
        self.assertEqual(manifest["total_files"], 2)
        self.assertIn("frozen_at", manifest)
        self.assertEqual(set(manifest["files"]),
                         {"cases/clean.txt", "labels/clean.labels.json"})
        for digest in manifest["files"].values():
            self.assertEqual(len(digest), 64)

        code, out = self.run_cli("--json", str(self.result_path()))
        self.assertEqual(code, 0, out)
        self.assertTrue(self.result_path().exists())

        payload = self.read_result()
        self.assertTrue(payload["manifest"]["sha256_verified"])
        self.assertFalse(payload["config"]["use_llm"])
        self.assertEqual(payload["metrics"]["cases"], 1)
        self.assertEqual(payload["metrics"]["labels_total"], 1)
        self.assertEqual(payload["metrics"]["labels_hit"], 0)
        self.assertEqual(payload["metrics"]["recall"]["n"], 1)
        self.assertEqual(payload["metrics"]["recall"]["pct"], 0.0)
        self.assertEqual(payload["cases"][0]["labels_hit"], 0)
        self.assertEqual(payload["cases"][0]["labels_total"], 1)

    def test_changing_one_character_fails_hash_check(self):
        case_path = self.write_case("clean.txt")
        self.freeze()
        original = case_path.read_text(encoding="utf-8")
        changed = original.replace("风险可控", "风险可测")
        self.assertNotEqual(original, changed, "测试文本没被改动")
        self.assertEqual(len(original), len(changed), "应保持长度不变，只改一个字")
        case_path.write_text(changed, encoding="utf-8")

        code, out = self.run_cli("--json", str(self.result_path()))
        self.assertEqual(code, 2, out)
        self.assertIn("sha256 变化", out)
        self.assertIn("cases/clean.txt", out)
        self.assertFalse(self.result_path().exists(),
                         "hash 不符时不应产出结果 JSON")

    def test_extra_case_not_in_manifest_exits_2(self):
        self.write_case("clean.txt")
        self.freeze()
        self.write_case("sneaked_in.txt")

        code, out = self.run_cli()
        self.assertEqual(code, 2, out)
        self.assertIn("清单外多出文件", out)
        self.assertIn("cases/sneaked_in.txt", out)

    @needs_agent
    def test_clean_case_without_label_is_clean_not_false_positive(self):
        self.write_case("clean.txt")
        self.freeze()

        code, out = self.run_cli("--json", str(self.result_path()))
        self.assertEqual(code, 0, out)
        payload = self.read_result()
        row = payload["cases"][0]
        self.assertEqual(row["kind"], "clean")
        self.assertEqual(row["labels_total"], 0)
        self.assertEqual(row["fp"], 0)
        self.assertEqual(row["issue_count"], 0)
        self.assertEqual(payload["metrics"]["clean_cases"], 1)
        self.assertEqual(payload["metrics"]["positive_cases"], 0)
        self.assertEqual(payload["metrics"]["fp_total"], 0)
        self.assertIsNone(payload["metrics"]["recall"]["pct"])
        self.assertEqual(payload["metrics"]["recall"]["ci95_pct"], [0.0, 0.0])

    @needs_agent
    def test_default_output_path_is_under_injected_out_dir(self):
        self.write_case("clean.txt")
        self.freeze()
        code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        written = sorted(self.out_dir.glob("holdout-*.json"))
        self.assertEqual(len(written), 1, out)
        self.assertIn("结果 JSON", out)

    @needs_agent
    def test_below_threshold_exits_1(self):
        self.write_case("clean.txt")
        self.write_labels("clean.labels.json", [{
            "id": "L1",
            "desc": "不会被命中的标注",
            "match": [["不会命中的占位符"]],
            "exclude_dimensions": ["style"],
        }])
        self.freeze()

        code, out = self.run_cli(
            "--min-recall", "0.8", "--json", str(self.result_path()))
        self.assertEqual(code, 1, out)
        payload = self.read_result()
        self.assertFalse(payload["passed"])
        self.assertTrue(any("召回" in item for item in payload["failures"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
