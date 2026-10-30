#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_audit_stream.py —— 统一审计事件流（阶段 4 最后一项）。

旧实现把「发生了什么」记在三处（`task.json` / `tools.jsonl` / `decisions.jsonl`），
字段各不相同，审计中心只能按文件分别展示，回答不了「第 7 步之前调了哪个工具」。

这里钉住四件事：

    1. 汇流：四个来源（节点事件 / 工具 / 决策 / 检查点）进一条流，序号从 1 连续
    2. 投影：旧的三个文件由事件流生成，**内容与旧实现逐字一致**（UI 不用改）
    3. 往返：写出去的 jsonl 能读回来，坏行跳过而不是整份读不出
    4. 边界诚实：跨来源没有统一时钟，`seq` 是「来源优先级 + 来源内顺序」，
       不是墙钟顺序——这条限制写在模块文档里，也在这里被固定下来
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402
from review_agent.application import (  # noqa: E402
    audit_to_legacy,
    build_audit_stream,
    load_audit_stream,
    write_audit_stream,
)
from review_agent.domain import AuditKind, ReviewProfile, Stage  # noqa: E402
from review_agent.runtime import NodeEvent, RunOutcome, RunState  # noqa: E402

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"

NODE_EVENTS = (
    NodeEvent(stage=Stage.ANALYSIS, node="rule-analyzer", ok=True, attempt=1,
              latency_ms=12, errors=(), warnings=()),
    NodeEvent(stage=Stage.EVIDENCE, node="evidence", ok=False, attempt=2,
              latency_ms=30, errors=("锚定失败",), warnings=()),
)
TOOL_LOGS = (
    {"tool": "pattern_rule", "target": "logic", "result": "命中 2 条",
     "latency_ms": 3, "status": "success"},
)
DECISIONS = (
    {"kind": "rule", "step": "S6 裁定", "detail": "无 blocker", "next": "pass"},
)


def make_state():
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.INTAKE,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
    )


class BuildStreamTests(unittest.TestCase):
    def setUp(self):
        self.stream = build_audit_stream(
            run_id=RUN_ID, node_events=NODE_EVENTS, tool_logs=TOOL_LOGS,
            decisions=DECISIONS)

    def test_sequence_is_continuous_from_one(self):
        self.assertEqual([e.seq for e in self.stream],
                         list(range(1, len(self.stream) + 1)))

    def test_kinds_follow_source_priority(self):
        self.assertEqual([e.kind for e in self.stream],
                         [AuditKind.STEP, AuditKind.STEP,
                          AuditKind.TOOL, AuditKind.DECISION])

    def test_failed_node_is_marked_failed(self):
        failed = [e for e in self.stream if e.status == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("evidence", failed[0].name)

    def test_detail_keeps_the_original_record(self):
        step = self.stream[0]
        self.assertEqual(dict(step.detail), NODE_EVENTS[0].as_dict(),
                         "事件明细必须能还原成旧记录，否则投影会失真")

    def test_empty_sources_produce_empty_stream(self):
        self.assertEqual(build_audit_stream(run_id=RUN_ID), ())


class LegacyProjectionTests(unittest.TestCase):
    def setUp(self):
        self.stream = build_audit_stream(
            run_id=RUN_ID, node_events=NODE_EVENTS, tool_logs=TOOL_LOGS,
            decisions=DECISIONS)

    def test_projection_matches_old_shapes(self):
        legacy = audit_to_legacy(self.stream)
        self.assertEqual(legacy["steps"],
                         [e.as_dict() for e in NODE_EVENTS])
        self.assertEqual(legacy["tools"], [dict(t) for t in TOOL_LOGS])
        self.assertEqual(legacy["decisions"], [dict(d) for d in DECISIONS])


class StreamIoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.stream = build_audit_stream(
            run_id=RUN_ID, node_events=NODE_EVENTS, tool_logs=TOOL_LOGS,
            decisions=DECISIONS)

    def test_round_trip(self):
        count = write_audit_stream(self.path, self.stream)
        self.assertEqual(count, len(self.stream))
        restored = load_audit_stream(self.path)
        self.assertEqual(len(restored), len(self.stream))
        self.assertEqual([(e.seq, e.kind, e.name) for e in restored],
                         [(e.seq, e.kind, e.name) for e in self.stream])

    def test_bad_line_is_skipped(self):
        write_audit_stream(self.path, self.stream)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("{这不是 json\n")
        self.assertEqual(len(load_audit_stream(self.path)), len(self.stream))

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_audit_stream(Path(self.tmp.name) / "nope.jsonl"), ())


class WriteV1LogsTests(unittest.TestCase):
    """`_write_v1_logs` 必须同时产出事件流与旧的三个文件。"""

    def setUp(self):
        self._saved = {name: getattr(review, name)
                       for name in ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger", "state"):
            (base / name).mkdir()
        review.OUTPUTS = base / "outputs"
        review.LOGS = base / "logs"
        review.LEDGER = base / "ledger"
        review.STATE = base / "state"
        review.GAP_LOG = base / "state" / "gap_log.jsonl"
        self.addCleanup(self._restore)
        self.log_dir = base / "logs" / "v1" / REVIEW_ID

    def _restore(self):
        for name, value in self._saved.items():
            setattr(review, name, value)

    def _write(self):
        state = make_state()
        outcome = RunOutcome(state=state, status="completed",
                             events=NODE_EVENTS)

        class _Session:
            tool_logs = [dict(t) for t in TOOL_LOGS]
            decisions = [dict(d) for d in DECISIONS]

        return review._write_v1_logs(_Session(), outcome, REVIEW_ID)

    def test_stream_and_legacy_files_are_both_written(self):
        self._write()
        self.assertTrue((self.log_dir / "events.jsonl").exists())
        self.assertTrue((self.log_dir / "task.json").exists())
        self.assertTrue((self.log_dir / "tools.jsonl").exists())
        self.assertTrue((self.log_dir / "decisions.jsonl").exists())

    def test_event_count_covers_every_source(self):
        self._write()
        stream = load_audit_stream(self.log_dir / "events.jsonl")
        self.assertEqual(len(stream),
                         len(NODE_EVENTS) + len(TOOL_LOGS) + len(DECISIONS))

    def test_task_json_steps_still_match_old_shape(self):
        self._write()
        task = json.loads((self.log_dir / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["steps"], [e.as_dict() for e in NODE_EVENTS])
        self.assertEqual(task["final_status"], "completed")

    def test_tools_jsonl_lines_match_session_logs(self):
        self._write()
        lines = (self.log_dir / "tools.jsonl").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line) for line in lines],
                         [dict(t) for t in TOOL_LOGS])

    def test_directory_hash_is_returned(self):
        digest = self._write()
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, "0" * 64)


if __name__ == "__main__":
    unittest.main()
