#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_engine_switch.py —— 引擎开关：legacy / v1 / shadow。

切流前的刹车不是一个开关本身，而是三条语义：

    1. **默认不变**：不选就是 legacy；非法值直接报错，绝不静默回退
       （用户以为跑了新引擎、实际还是旧链路，是最坏的结果）
    2. **v1 不写正式报告**：六节点流水线还没接语义审与报告写出，
       往 `outputs/` 写一份同名报告等于让用户看到「看起来一样、实际少一半」的东西
    3. **shadow 双跑**：旧报告照出，同时跑一遍新流水线并把差异写进 `state/double_run/`；
       未解释差异要能被列出来，而不是只打印一句「有差异」

这里用进程内调用（临时目录重定向 outputs/logs/ledger/state），不污染仓库。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402

TARGET = "targets/contract_001.md"


class EngineParseTests(unittest.TestCase):
    def test_flag_forms(self):
        self.assertEqual(review._take_engine(["--engine", "v1", "t.md"])[0], "v1")
        self.assertEqual(review._take_engine(["--engine=shadow", "t.md"])[0], "shadow")
        engine, rest = review._take_engine(["--engine", "v1", "t.md"])
        self.assertEqual(rest, ["t.md"])

    def test_default_is_legacy(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(review.ENGINE_ENV, None)
            self.assertEqual(review._take_engine(["t.md"])[0], "legacy")

    def test_env_is_read(self):
        with mock.patch.dict("os.environ", {review.ENGINE_ENV: "v1"}):
            self.assertEqual(review._take_engine(["t.md"])[0], "v1")

    def test_flag_wins_over_env(self):
        with mock.patch.dict("os.environ", {review.ENGINE_ENV: "v1"}):
            self.assertEqual(
                review._take_engine(["--engine", "legacy", "t.md"])[0], "legacy")

    def test_unknown_engine_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            review._take_engine(["--engine", "v2", "t.md"])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_value_is_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            review._take_engine(["t.md", "--engine"])
        self.assertEqual(ctx.exception.code, 2)


class EngineRunTests(unittest.TestCase):
    """进程内跑 main()：确认三条语义真的成立。"""

    def setUp(self):
        self._saved = {name: getattr(review, name)
                       for name in ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")}
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger", "state"):
            (base / name).mkdir()
        review.OUTPUTS = base / "outputs"
        review.LOGS = base / "logs"
        review.LEDGER = base / "ledger"
        review.STATE = base / "state"
        review.GAP_LOG = base / "state" / "gap_log.jsonl"
        self.base = base

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(review, name, value)
        self.tmp.cleanup()

    def run_main(self, argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["review.py", *argv]), \
                mock.patch.dict("os.environ", env or {}), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                review.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def reports(self):
        return sorted((self.base / "outputs").glob("R-*.json"))

    def test_shadow_requires_no_llm(self):
        code, out, err = self.run_main([TARGET, "--engine", "shadow", "--llm"])
        self.assertEqual(code, 2)
        self.assertIn("纯规则", out)
        self.assertIn("REVIEW_ERROR", err)
        self.assertEqual(self.reports(), [], "被拒绝的请求不该产出报告")

    def test_v1_without_llm_degrades_with_notice(self):
        # 没配 LLM 时自动降级为纯规则审，但必须明确说出来，不能假装审了模型。
        # 这里把配置固定为「不可用」：本机可能真的配了 Key，测试不能因此发真请求。
        unavailable = mock.Mock(available=False, enabled=True, model="none")
        with mock.patch.object(review.llm_client, "load_config",
                               return_value=unavailable):
            code, out, err = self.run_main([TARGET, "--engine", "v1"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("语义审", out)
        self.assertIn("纯规则审", out)
        self.assertEqual(self.reports(), [])

    def test_v1_writes_state_not_report(self):
        code, out, err = self.run_main([TARGET, "--no-llm", "--engine", "v1"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("[v1]", out)
        self.assertEqual(self.reports(), [], "v1 不写正式报告")
        states = list((self.base / "state" / "runs").glob("*.state.json"))
        self.assertEqual(len(states), 1, "v1 必须落状态，否则恢复无从谈起")

    def test_shadow_writes_report_and_diff(self):
        code, out, err = self.run_main([TARGET, "--no-llm", "--engine", "shadow"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("[shadow]", out)
        reports = self.reports()
        self.assertEqual(len(reports), 1, "shadow 仍由旧链路出正式报告")
        review_id = reports[0].stem
        diff_path = self.base / "state" / "double_run" / f"{review_id}.json"
        self.assertTrue(diff_path.exists(), "shadow 必须留差异文件")
        payload = json.loads(diff_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["engine"], "shadow")
        self.assertTrue(payload["diff"]["ok"],
                        f"双跑出现未解释差异：{payload['diff']}")
        self.assertEqual(payload["status"], "completed")

    def test_v1_runs_do_not_reuse_review_id(self):
        # v1 不写报告；如果 ID 分配只看 outputs/，第二次 v1 会覆盖上一份状态
        for _ in range(2):
            code, out, err = self.run_main([TARGET, "--no-llm", "--engine", "v1"])
            self.assertEqual(code, 0, out + err)
        states = sorted((self.base / "state" / "runs").glob("*.state.json"))
        self.assertEqual(len(states), 2, f"v1 两次运行应产生两份状态：{states}")


if __name__ == "__main__":
    unittest.main()
