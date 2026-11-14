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
import contextlib
import io
from pathlib import Path
from unittest import mock

from review_agent.infrastructure.task_lock import TaskResumeLock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402

TARGETS = (
    "targets/answer_001.txt",
    "targets/contract_001.md",
    "targets/adversarial_meeting_001.txt",
    "targets/code_001.py",
)

# 双跑比对要在真实样本上跑，依赖本地私有资产（targets/ 样本 + agents/ Agent 定义）；
# 公开 clone 不含这两者（见 .gitignore），缺失时跳过本用例，本地带资产时照常运行。
PRIVATE_OK = all((ROOT / t).exists() for t in TARGETS) and (ROOT / "agents").is_dir()


class DoubleRunTests(unittest.TestCase):
    def setUp(self):
        self._saved = {name: getattr(review, name)
                       for name in ("OUTPUTS", "LOGS", "LEDGER", "STATE",
                                    "GAP_LOG")}
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger", "state"):
            (base / name).mkdir()
        review.OUTPUTS = base / "outputs"
        review.LOGS = base / "logs"
        review.LEDGER = base / "ledger"
        review.STATE = base / "state"
        review.GAP_LOG = base / "state" / "gap_log.jsonl"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(review, name, value)
        self.tmp.cleanup()

    @unittest.skipUnless(PRIVATE_OK,
                         "私有审查资产缺失（targets/ 样本或 agents/ 定义不在公开仓库），"
                         "跳过双跑比对")
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

    @unittest.skipUnless(PRIVATE_OK, "私有审查资产缺失，跳过无存储检查")
    def test_storeless_pipeline_does_not_write_checkpoints(self):
        state, outcome, _ = review.run_typed_pipeline(
            (ROOT / "targets" / "answer_001.txt").resolve(),
            review.load_rules(), review_id="R-20261030-805", use_llm=False)
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertFalse((review.STATE / "checkpoints").exists(),
                         "未请求持久恢复的调用不应写运行检查点")

    @unittest.skipUnless(PRIVATE_OK, "私有审查资产缺失，跳过续跑检查")
    def test_completed_run_can_load_without_reexecuting_nodes(self):
        review_id = "R-20261030-806"
        run_id = f"{review_id}-RUN01"
        query = dict(
            target_path=(ROOT / "targets" / "answer_001.txt").resolve(),
            rules=review.load_rules(), review_id=review_id, use_llm=False,
        )
        first = review._run_v1_core(**query)
        second = review._run_v1_core(
            **query, resume_run_id=run_id)

        self.assertTrue(first["outcome"].ok, first["outcome"].errors)
        self.assertTrue(second["outcome"].ok, second["outcome"].errors)
        self.assertEqual(second["outcome"].events, ())
        self.assertEqual(second["state"].run_id, run_id)

    @unittest.skipUnless(PRIVATE_OK, "私有审查资产缺失，跳过中断续跑检查")
    def test_interrupted_analysis_resumes_to_report(self):
        review_id = "R-20261030-807"
        run_id = f"{review_id}-RUN01"
        target = (ROOT / "targets" / "answer_001.txt").resolve()

        def interrupt(_step):
            raise KeyboardInterrupt("模拟分析阶段进程中断")

        with mock.patch.object(
                review, "rule_executor_from", return_value=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                review._run_v1_core(
                    target, review.load_rules(), review_id=review_id,
                    use_llm=False)

        self.assertTrue((review.STATE / "runs" / f"{run_id}.state.json").exists())
        info = review._run_v1_core(
            target, review.load_rules(), review_id=review_id,
            use_llm=False, resume_run_id=run_id)
        self.assertTrue(info["outcome"].ok, info["outcome"].errors)
        self.assertIsNotNone(info["report_path"])
        self.assertTrue(Path(info["report_path"]).exists())

    @unittest.skipUnless(PRIVATE_OK, "私有审查资产缺失，跳过任务级续跑检查")
    def test_task_record_resumes_after_interruption(self):
        review_id = "R-20261114-901"
        target = (ROOT / "targets" / "answer_001.txt").resolve()

        def interrupt(_step):
            raise KeyboardInterrupt("模拟进程在规则分析中中断")

        with mock.patch.object(
                review, "rule_executor_from", return_value=interrupt), \
                mock.patch.object(
                    review, "_allocate_task_review_id",
                    return_value=review_id):
            with self.assertRaises(KeyboardInterrupt):
                review._run_v1_via_task(
                    target, review.load_rules(), use_llm=False)

        record = review.FileTaskStore(
            review.STATE / "tasks").get(review_id)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.execution["manifest_version"], 1)
        self.assertTrue(
            review.has_run_state(record.run_id),
            "中断前应已持久化阶段状态")

        with TaskResumeLock(review.STATE / "tasks", review_id):
            with self.assertRaises(review.InputError):
                review.resume_task(review_id)

        with contextlib.redirect_stdout(io.StringIO()):
            resumed = review.resume_task(review_id)
        self.assertEqual(resumed.status, "completed")
        self.assertTrue(resumed.report_path)
        self.assertTrue(Path(resumed.report_path).exists())
        self.assertFalse(
            (review.STATE / "tasks" / f".{review_id}.resume.lock").exists())


if __name__ == "__main__":
    unittest.main()
