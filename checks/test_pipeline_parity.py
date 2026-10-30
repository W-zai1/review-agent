#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_pipeline_parity.py —— 双跑比对：新六节点流水线 vs 旧 S1~S6。

REFACTOR_PLAN 阶段 2 的硬判据：**任何发现的差异都要解释，否则按新实现 bug 处理**。
这里在同一份输入上各跑一遍：

    旧链路：review.run_one(...)              → 报告 dict（issues / suppressed / verdict）
    新链路：review.run_typed_pipeline(...)   → RunState（findings / decision）

比对规则（`review.compare_with_report`）：
    * 新链路保留下来的每条发现，必须对上旧报告的 issues 或 suppressed
    * 旧报告的每条 issue，必须在新链路里存在
    * verdict 必须一致

`suppressed` 也算对上：同片段归口折叠目前仍只在旧流程里跑，折叠不等于丢结论。

目前只比 rule-only（`use_llm=False`）：语义审入队仍是 Session 语义，没接进新链路。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402

TARGETS = (
    "targets/answer_001.txt",
    "targets/contract_001.md",
    "targets/adversarial_meeting_001.txt",
    "targets/code_001.py",
)


class DoubleRunTests(unittest.TestCase):
    def setUp(self):
        self._saved = {name: getattr(review, name)
                       for name in ("OUTPUTS", "LOGS", "LEDGER", "GAP_LOG")}
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger"):
            (base / name).mkdir()
        review.OUTPUTS = base / "outputs"
        review.LOGS = base / "logs"
        review.LEDGER = base / "ledger"
        review.GAP_LOG = base / "state" / "gap_log.jsonl"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(review, name, value)
        self.tmp.cleanup()

    def test_six_node_pipeline_matches_legacy(self):
        rules = review.load_rules()
        for index, target in enumerate(TARGETS, start=1):
            with self.subTest(target=target):
                path = (ROOT / target).resolve()
                report = review.run_one(path, rules, use_llm=False)
                state, outcome, _session = review.run_typed_pipeline(
                    path, rules, review_id=f"R-20261030-{800 + index}")
                self.assertEqual(outcome.status, "completed",
                                 f"{target}: {outcome.errors}")
                diff = review.compare_with_report(report, state)
                self.assertTrue(
                    diff["ok"],
                    f"{target}: 未解释差异 unexplained={diff['unexplained']} "
                    f"missing={diff['missing']} "
                    f"verdict {diff['verdict_new']} vs {diff['verdict_legacy']}")
                # 保留下来的发现都必须能回查原文（证据阶段的硬要求）
                for finding in review.surviving_findings(state):
                    if finding.quote:
                        self.assertIn(finding.quote, report["target_text"])

    def test_unknown_target_fails_the_same_way(self):
        with self.assertRaises(review.UnreadableTargetError):
            review.run_typed_pipeline(
                (ROOT / "targets" / "__nope__.txt").resolve(), review.load_rules())


if __name__ == "__main__":
    unittest.main()
