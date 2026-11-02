#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_double_run.py —— 双跑汇总与切流判据（批次 2 / A4）。

切流判据是「连续 N 天零未解释差异」。这句话只有在**能被一条命令回答**时才成立，
否则就是"记得看一眼 state/double_run/"。这里钉住三件事：

    1. 未解释差异 / 缺失 / 裁定不一致 —— 任一条都算不通过（不许用"模型波动"解释）
    2. `--require` 的样本门槛真的会拦（样本不足 ≠ 通过）
    3. 坏文件不让整体观测失败（一份 JSON 坏掉不该让结论变成"没有数据"）
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import double_run                                       # noqa: E402


def payload(rid, *, ok=True, unexplained=0, missing=0, folded=0,
            verdict_new="warn", verdict_legacy="warn", errors=()):
    return {
        "review_id": rid,
        "engine": "shadow",
        "status": "completed",
        "errors": list(errors),
        "diff": {
            "ok": ok,
            "verdict_new": verdict_new,
            "verdict_legacy": verdict_legacy,
            "unexplained": [["R-1", "引用"] for _ in range(unexplained)],
            "missing": [["R-2", "引用"] for _ in range(missing)],
            "folded": [["R-3", "引用"] for _ in range(folded)],
        },
    }


class SummarizeTests(unittest.TestCase):
    def test_empty_is_not_a_pass_with_require(self):
        summary = double_run.summarize([])
        self.assertEqual(summary["samples"], 0)
        passed, reasons = double_run.check(summary, require=1)
        self.assertFalse(passed)
        self.assertIn("样本不足", reasons[0])

    def test_clean_samples_pass(self):
        summary = double_run.summarize([payload("R-20261101-001"),
                                        payload("R-20261101-002")])
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["ok_samples"], 2)
        passed, reasons = double_run.check(summary)
        self.assertTrue(passed, reasons)

    def test_unexplained_diff_fails(self):
        summary = double_run.summarize([payload("R-20261101-003", ok=False,
                                                unexplained=2)])
        passed, reasons = double_run.check(summary)
        self.assertFalse(passed)
        self.assertIn("未解释差异", reasons[0])
        self.assertEqual(summary["failed_samples"], 1)

    def test_missing_diff_fails(self):
        summary = double_run.summarize([payload("R-20261101-004", ok=False,
                                                missing=1)])
        passed, reasons = double_run.check(summary)
        self.assertFalse(passed)
        self.assertIn("缺失", reasons[0])

    def test_verdict_conflict_fails_even_if_diff_ok(self):
        """同一份材料两个裁定：即使 findings 全对上，也不许切流。"""
        summary = double_run.summarize([payload("R-20261101-005", verdict_new="fail",
                                                verdict_legacy="pass")])
        passed, reasons = double_run.check(summary)
        self.assertFalse(passed)
        self.assertIn("裁定不一致", reasons[0])

    def test_folded_is_not_a_failure(self):
        """折叠只是"不在清单里展开"，不是差异。"""
        summary = double_run.summarize([payload("R-20261101-006", folded=3)])
        self.assertEqual(summary["folded"], 3)
        passed, _ = double_run.check(summary)
        self.assertTrue(passed)

    def test_since_filters_by_review_date(self):
        rows = [payload("R-20261030-001"), payload("R-20261101-001", ok=False,
                                                   unexplained=1)]
        summary = double_run.summarize(rows, since="2026-11-01")
        self.assertEqual(summary["samples"], 1)
        self.assertEqual(summary["unexplained"], 1)
        self.assertEqual(summary["first_date"], "2026-11-01")
        earlier = double_run.summarize(rows, since="2026-10-01")
        self.assertEqual(earlier["samples"], 2)

    def test_sample_requirement_blocks_small_batches(self):
        summary = double_run.summarize([payload("R-20261101-001")])
        passed, reasons = double_run.check(summary, require=20)
        self.assertFalse(passed)
        self.assertIn("样本不足", reasons[0])


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / "state" / "double_run"
        self.base.mkdir(parents=True)

    def write(self, name, data):
        (self.base / name).write_text(json.dumps(data, ensure_ascii=False),
                                      encoding="utf-8")

    def test_missing_dir_is_empty(self):
        self.assertEqual(double_run.load_payloads(self.base / "nope"), [])

    def test_bad_file_is_skipped(self):
        self.write("R-20261101-001.json", payload("R-20261101-001"))
        (self.base / "R-20261101-002.json").write_text("{坏文件", encoding="utf-8")
        rows = double_run.load_payloads(self.base)
        self.assertEqual([r["review_id"] for r in rows], ["R-20261101-001"])

    def test_cli_exit_codes(self):
        self.write("R-20261101-001.json", payload("R-20261101-001"))
        self.assertEqual(double_run.main(["--root", str(self.base.parents[1])]), 0)
        self.write("R-20261101-002.json", payload("R-20261101-002", ok=False,
                                                  unexplained=1))
        self.assertEqual(double_run.main(["--root", str(self.base.parents[1])]), 1)


if __name__ == "__main__":
    unittest.main()
