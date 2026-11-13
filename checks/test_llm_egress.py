#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_llm_egress.py —— 模型外发的审计闸口（决策书 4.6 收尾）。

`tools/llm_client.chat()` 是全项目唯一的外发出口（审查、OCR、目的裁决、
Web 探针都走它）。这里验证四件事：

    1. 没设上下文时行为不变，不写日志（单元测试/bench 不受影响）；
    2. 设了上下文：attempt 先于 HTTP，结束后写配对 result；
    3. 审计写不进去 → 抛 LLMError 且**不发起请求**（审计不可用时不外发）；
    4. 日志只记元信息（字符数、主机、usage），不落 prompt 原文。

server 侧的「登记失败不起子进程」在同一文件末尾单独验证，因为它是
另一条外发路径（起 `review.py --llm` 子进程），不是 `chat()`。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import llm_client  # noqa: E402


class _Body:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _payload(content="hi"):
    return json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "model": "fake-model",
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }).encode("utf-8")


def cfg(**kwargs):
    values = dict(enabled=True, base_url="https://api.example.com/v1",
                  model="fake-model", api_key="test-key", timeout=5,
                  max_retries=0, temperature=0, cache=False, max_tokens=16,
                  max_input_chars=1000, max_issues_per_dim=2, chunking=False,
                  chunk_chars=100, chunk_overlap=0, headers={}, source="test")
    values.update(kwargs)
    return llm_client.LLMConfig(**values)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "state" / "egress.log"

    def rows(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in
                self.log.read_text(encoding="utf-8").splitlines()]

    def scope(self, **kwargs):
        scope = llm_client.EgressScope(self.log, kind="llm", **kwargs)
        token = llm_client.set_egress_scope(scope)
        self.addCleanup(llm_client.reset_egress_scope, token)
        return scope

    def chat(self, client=None, messages=None):
        client = client or llm_client.LLMClient(cfg())
        return client.chat(messages or [{"role": "user", "content": "hello"}])


class NoScopeTests(_Base):
    def test_without_scope_nothing_is_written(self):
        """没设上下文就当作「不需要审计」：单测、bench、--check 都靠这条。"""
        with mock.patch.object(llm_client.urllib.request, "urlopen",
                               lambda *a, **k: _Body(_payload())):
            self.assertEqual(self.chat().content, "hi")
        self.assertFalse(self.log.exists())
        self.assertIsNone(llm_client.current_egress_scope())


class AttemptAndResultTests(_Base):
    def test_attempt_comes_before_the_request(self):
        order = []

        def fake_urlopen(*args, **kwargs):
            order.append("http")
            # 请求发出时 attempt 行必须已经在盘上
            self.assertEqual([r["phase"] for r in self.rows()], ["attempt"])
            return _Body(_payload())

        self.scope(review_id="R-1", run_id="R-1-RUN01", target="t.md")
        with mock.patch.object(llm_client.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(self.chat().content, "hi")
        self.assertEqual(order, ["http"])

    def test_result_is_paired_with_attempt(self):
        self.scope(review_id="R-1", run_id="R-1-RUN01", target="t.md")
        with mock.patch.object(llm_client.urllib.request, "urlopen",
                               lambda *a, **k: _Body(_payload("ok"))):
            self.chat()
        attempt, result = self.rows()
        self.assertEqual((attempt["phase"], result["phase"]), ("attempt", "result"))
        self.assertEqual(attempt["attempt_id"], result["attempt_id"])
        self.assertEqual(result["result"], "ok")
        self.assertEqual(result["usage"], {"input": 3, "output": 4})
        self.assertEqual(result["review_id"], "R-1")
        self.assertEqual(result["run_id"], "R-1-RUN01")
        self.assertEqual(result["target"], "t.md")
        self.assertIsInstance(result["duration_ms"], int)

    def test_one_chat_is_one_pair_even_with_retries(self):
        """重试属于同一次外发：记多行会让「发了几次」失真。"""
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise llm_client.urllib.error.HTTPError(
                    "https://api.example.com/v1/chat/completions", 429,
                    "too many", {}, None)
            return _Body(_payload())

        self.scope()
        client = llm_client.LLMClient(cfg(max_retries=2))
        with mock.patch.object(llm_client.urllib.request, "urlopen", flaky):
            self.assertEqual(self.chat(client).content, "hi")
        rows = self.rows()
        self.assertEqual([r["phase"] for r in rows], ["attempt", "result"])
        self.assertEqual(calls["n"], 2)

    def test_prompt_text_is_not_logged(self):
        secret_text = "本公司 2025 年营业收入 100 亿元"
        self.scope()
        with mock.patch.object(llm_client.urllib.request, "urlopen",
                               lambda *a, **k: _Body(_payload())):
            self.chat(messages=[{"role": "user", "content": secret_text}])
        attempt, result = self.rows()
        self.assertEqual(attempt["prompt_chars"], len(secret_text))
        self.assertNotIn("[", attempt["host"])
        self.assertNotIn("亿元", json.dumps(attempt, ensure_ascii=False))
        self.assertNotIn("亿元", json.dumps(result, ensure_ascii=False))


class BlockingTests(_Base):
    def test_write_failure_blocks_the_call(self):
        """审计写不进去 → 不发起请求（4.6 的核心验收）。"""
        self.log = Path(self.tmp.name) / "nested" / "file" / "egress.log"
        (Path(self.tmp.name) / "nested").write_text("我是文件", encoding="utf-8")
        scope = llm_client.EgressScope(self.log)
        token = llm_client.set_egress_scope(scope)
        self.addCleanup(llm_client.reset_egress_scope, token)
        with mock.patch.object(llm_client.urllib.request, "urlopen",
                               side_effect=AssertionError("审计失败时不允许外发")):
            with self.assertRaises(llm_client.LLMError) as ctx:
                self.chat()
        self.assertIn("外发审计写入失败", str(ctx.exception))

    def test_http_failure_is_recorded_and_reraised(self):
        self.scope()

        def boom(*args, **kwargs):
            raise llm_client.LLMError("连不上")

        with mock.patch.object(llm_client.urllib.request, "urlopen", boom):
            with self.assertRaises(llm_client.LLMError):
                self.chat()
        attempt, result = self.rows()
        self.assertEqual(result["result"], "failed")
        self.assertEqual(result["attempt_id"], attempt["attempt_id"])
        self.assertIn("连不上", result["error"])


class IdentifyTests(_Base):
    def test_run_id_can_be_filled_after_scope_is_set(self):
        """review_id/run_id 在 scope 创建之后才确定（v1 核心），要能补登记。"""
        self.scope(target="t.md")
        self.assertIsNotNone(llm_client.update_egress_scope(
            review_id="R-9", run_id="R-9-RUN01"))
        with mock.patch.object(llm_client.urllib.request, "urlopen",
                               lambda *a, **k: _Body(_payload())):
            self.chat()
        attempt, _ = self.rows()
        self.assertEqual(attempt["review_id"], "R-9")
        self.assertEqual(attempt["run_id"], "R-9-RUN01")

    def test_update_without_scope_is_a_noop(self):
        """没有上下文时补登记不能抛（比如纯规则运行里调到）。"""
        self.assertIsNone(llm_client.update_egress_scope(review_id="R-1"))


class CliScopeTests(unittest.TestCase):
    def test_cli_sets_scope_and_resets_it(self):
        """CLI 入口负责设上下文：跑完之后必须复位，否则记录会串到下一个任务。"""
        import review as runtime
        from interfaces.cli import commands

        seen = {}

        def fake_body(opts, rt):
            scope = rt.llm_client.current_egress_scope()
            seen["scope"] = scope
            raise SystemExit(0)

        with mock.patch.object(commands, "_run_review_body", fake_body):
            opts = mock.Mock(target_arg="targets/sample.md")
            with self.assertRaises(SystemExit):
                commands._run_review(opts, runtime)
        self.assertIsNotNone(seen["scope"])
        self.assertEqual(seen["scope"].target,
                         str(Path(runtime.ROOT) / "targets/sample.md"))
        self.assertIsNone(runtime.llm_client.current_egress_scope())


class ServerGateTests(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = server.EGRESS_LOG
        server.EGRESS_LOG = Path(self.tmp.name) / "state" / "egress.log"
        self.addCleanup(self._restore)

    def _restore(self):
        self.server.EGRESS_LOG = self._saved

    def test_begin_and_end_pair(self):
        attempt_id = self.server._egress_begin("llm", target="targets/a.md",
                                               job_id="job1", detail="x")
        self.assertTrue(attempt_id)
        self.server._egress_end(attempt_id, job_id="job1",
                                target="targets/a.md", ok=False, error="炸了")
        rows = [json.loads(line) for line in
                self.server.EGRESS_LOG.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["phase"] for r in rows], ["attempt", "result"])
        self.assertEqual(rows[0]["attempt_id"], rows[1]["attempt_id"])
        self.assertEqual(rows[1]["result"], "failed")

    def test_begin_returns_none_when_log_is_unwritable(self):
        Path(self.tmp.name, "state").write_text("我是文件", encoding="utf-8")
        self.assertIsNone(self.server._egress_begin("llm", target="x"))

    def test_job_does_not_start_subprocess_when_audit_fails(self):
        """登记失败时任务落 FAILED，且**没有**起子进程。"""
        Path(self.tmp.name, "state").write_text("我是文件", encoding="utf-8")
        with mock.patch.object(self.server.subprocess, "Popen",
                               side_effect=AssertionError("不该起子进程")):
            outcome = self.server._run_review_job(
                "job1", "R-20261112-001", "targets/a.md", True,
                str(Path(self.tmp.name) / "p.jsonl"))
        self.assertEqual(outcome.status.value, "failed")
        self.assertIn("外发审计登记失败", outcome.error or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
