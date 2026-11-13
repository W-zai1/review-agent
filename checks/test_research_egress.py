"""决策书 4.6 验收：Research 外发必须「先登记、写不进就不发、结束后有结果」。

对应用户选的 A 档（只改 Research 两个分支，不动 LLM 阻断）：

1. 外发**之前**必须已经有一条 `phase="attempt"` 记录；
2. 审计写不进去时**不发起检索**，把原因记进 `research.errors`；
3. 请求成功或失败都要写 `phase="result"`，并与 attempt 用 `attempt_id` 关联；
4. 记录只带元信息（实体/指标/年份/查询哈希/字符数/域名/耗时），
   不落原文数值、不落 prompt。

这些用例不联网：`research.verify` / 注册表都被替换成假实现，
只有「谁先写、写不写得进、写完有没有配对」是真的。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import research                                            # noqa: E402
import review                                              # noqa: E402
from review_agent.capabilities.tools import ToolResult

REVIEW_ID = "R-20261111-500"
RUN_ID = "R-20261111-500-RUN01"
TEXT = "本公司 2025 年营业收入 100 亿元。"
CLAIM = {
    "quote": "2025 年营业收入 100 亿元", "entity": "本公司",
    "metric": "营业收入", "kind": "number", "value": 100.0,
    "unit": "亿元", "year": 2025, "sentence_index": 1, "raw": "100",
}
VERDICT = {
    "status": "supported", "confidence": 0.9, "value": 100.0,
    "display": "100 亿元", "reason": "官网披露", "query": "本公司 营业收入 2025",
    "providers": ["web"],
    "sources": [
        {"url": "https://www.example.com/a", "title": "来源 A"},
        {"url": "https://sub.example.com/b", "title": "来源 B"},
    ],
}
CFG = {"providers": [], "max_claims_per_doc": 1, "max_seconds": 60}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self._saved_state = review.STATE
        review.STATE = self.base / "state"
        self.addCleanup(self._restore)
        self.egress = self.base / "state" / "egress.log"
        self.verify_calls: list[dict] = []
        patches = [
            mock.patch.object(research, "extract_claims",
                              lambda *a, **k: [dict(CLAIM)]),
            mock.patch.object(research, "lookup_verdict",
                              lambda *a, **k: (None, None)),
            mock.patch.object(research, "remember_verdict", lambda *a, **k: None),
            mock.patch.object(review.retrieve, "load_facts",
                              lambda: {"assertions": []}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore(self):
        review.STATE = self._saved_state

    def rows(self) -> list[dict]:
        if not self.egress.exists():
            return []
        return [json.loads(line) for line in
                self.egress.read_text(encoding="utf-8").splitlines() if line]

    def executor(self, *, use_tool=False, record_egress=True, web=False):
        return review.make_research_executor(
            review_id=REVIEW_ID, run_id=RUN_ID, cfg=dict(CFG),
            web=web, use_tool=use_tool, record_egress=record_egress)

    def run_executor(self, **kwargs):
        return asyncio.run(self.executor(**kwargs)(TEXT))


class AttemptBeforeCallTests(_Base):
    def test_attempt_is_written_before_the_request(self):
        """外发函数被调用时，attempt 行必须已经在文件里。"""
        seen: list[list[str]] = []

        def fake_verify(claim, cfg=None, web=True, provider=None):
            seen.append([row["phase"] for row in self.rows()])
            self.verify_calls.append(dict(claim))
            return dict(VERDICT)

        with mock.patch.object(research, "verify", fake_verify):
            self.run_executor()
        self.assertEqual(seen, [["attempt"]],
                         "先外发后登记 = 4.6 要修的那条路径")

    def test_result_is_paired_with_attempt(self):
        with mock.patch.object(research, "verify",
                               lambda *a, **k: dict(VERDICT)):
            self.run_executor()
        rows = self.rows()
        self.assertEqual([r["phase"] for r in rows], ["attempt", "result"])
        attempt, result = rows
        self.assertEqual(attempt["attempt_id"], result["attempt_id"])
        self.assertEqual(result["result"], "ok")
        self.assertIsInstance(result["duration_ms"], int)
        self.assertGreaterEqual(result["duration_ms"], 0)
        self.assertEqual(result["run_id"], RUN_ID)
        self.assertEqual(result["target"], REVIEW_ID)

    def test_records_metadata_not_source_text(self):
        with mock.patch.object(research, "verify",
                               lambda *a, **k: dict(VERDICT)):
            self.run_executor()
        rows = self.rows()
        attempt, result = rows
        self.assertEqual(attempt["detail"], "外部检索：本公司营业收入（2025）")
        self.assertEqual(attempt["providers"], [])
        self.assertEqual(len(attempt["query_sha256"]), 16)
        self.assertEqual(attempt["query_chars"],
                         len(research.build_query(dict(CLAIM))))
        self.assertNotIn("100 亿元", json.dumps(rows, ensure_ascii=False),
                         "审计日志不该出现原文/数值")
        self.assertEqual(result["sources"], 2)
        self.assertEqual(result["domains"],
                         ["www.example.com", "sub.example.com"])


class BlockOnWriteFailureTests(_Base):
    def test_unwritable_audit_blocks_the_request(self):
        """审计写不进去：宁可不核验，也不发「没有记录的外发」。"""
        blocked = self.base / "blocked"
        blocked.write_text("我是文件不是目录", encoding="utf-8")
        review.STATE = blocked / "state"       # mkdir 会抛 NotADirectoryError

        def fake_verify(*a, **k):
            raise AssertionError("审计失败时不允许发起外发")

        with mock.patch.object(research, "verify", fake_verify):
            outcome = self.run_executor()
        self.assertTrue(any("已阻止外部检索" in e for e in outcome.errors),
                        outcome.errors)
        self.assertFalse(self.egress.exists())

    def test_record_egress_false_still_calls_without_writing(self):
        fake = mock.Mock(return_value=dict(VERDICT))
        with mock.patch.object(research, "verify", fake):
            outcome = self.run_executor(record_egress=False)
        self.assertEqual(fake.call_count, 1)
        self.assertFalse(self.egress.exists())
        self.assertEqual(outcome.block["supported"][0]["value"], 100.0)


class FailureIsRecordedTests(_Base):
    def test_exception_still_writes_a_failed_result(self):
        def boom(*a, **k):
            raise RuntimeError("检索器炸了")

        with mock.patch.object(research, "verify", boom):
            with self.assertRaises(RuntimeError):
                self.run_executor()
        rows = self.rows()
        self.assertEqual([r["phase"] for r in rows], ["attempt", "result"])
        self.assertEqual(rows[1]["result"], "failed")
        self.assertIn("检索器炸了", rows[1]["error"])
        self.assertEqual(rows[0]["attempt_id"], rows[1]["attempt_id"])


class ToolPathTests(_Base):
    """走 `research.web` 注册表的分支：同样是先登记再执行。"""

    def _executor_with(self, registry):
        with mock.patch.object(review, "ToolRegistry", lambda **kwargs: registry):
            return self.executor(use_tool=True)

    def test_tool_execution_is_recorded_around_the_call(self):
        seen: list[list[str]] = []

        class FakeRegistry:
            def __init__(self, rows):
                self.calls = []
                self.rows = rows

            def register(self, tool):
                pass

            async def execute(self, name, data, context):
                seen.append([row["phase"] for row in self.rows()])
                self.calls.append(name)
                return ToolResult.success(dict(VERDICT)), SimpleNamespace(
                    invocation_id="inv-1")

        registry = FakeRegistry(self.rows)
        outcome = asyncio.run(self._executor_with(registry)(TEXT))
        self.assertEqual(seen, [["attempt"]])
        self.assertEqual(registry.calls, ["research.web"])
        rows = self.rows()
        self.assertEqual([r["phase"] for r in rows], ["attempt", "result"])
        self.assertEqual(rows[1]["result"], "ok")
        self.assertEqual(outcome.block["supported"][0]["value"], 100.0)

    def test_tool_failure_keeps_both_records(self):
        class FailingRegistry:
            def register(self, tool):
                pass

            async def execute(self, name, data, context):
                return (ToolResult.failure("检索超时"), SimpleNamespace(
                    invocation_id="inv-2"))

        outcome = asyncio.run(self._executor_with(FailingRegistry())(TEXT))
        self.assertTrue(any("检索超时" in e for e in outcome.errors),
                        outcome.errors)
        rows = self.rows()
        self.assertEqual(rows[1]["result"], "failed")
        self.assertIn("检索超时", rows[1]["error"])


if __name__ == "__main__":
    unittest.main()
