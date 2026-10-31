#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_model_client.py —— 模型端口：换供应商不改审查代码。

方案里的验收问题「能否单独替换 LLM？」此前只能答「部分」：节点层有
`SemanticExecutor` 端口，但语义审内部仍直接绑 `llm_client.LLMClient` 的返回类型，
换供应商还是要动解析代码。

这里钉住端口的三件事：

    1. **契约**：`ModelClient.chat(messages) -> ModelReply`，只有这一个方法；
       不满足的客户端在**装配时**就报错，不留到跑到一半才炸
    2. **归一**：任意「带 content/model/finish_reason/usage/latency_ms 的对象」
       都能被适配成 `ModelReply`，缺字段给默认值
    3. **可替换**：两个不同供应商的假客户端，套进同一个 `make_llm_review_fn`
       都能跑，且不需要任何配置文件——这就是「换供应商不改代码」的证据

用例**不 mock** `llm_review.review_dimension`：要走真实的 prompt 构造与解析，
否则证明不了「换客户端后审查路径仍然完整」。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402
from review_agent.adapters.model import (  # noqa: E402
    ChatClientAdapter,
    ModelClientSettings,
    as_model_client,
    settings_for,
)
from review_agent.domain import ModelClient, ModelReply  # noqa: E402

TEXT = "本产品市场占有率达到 90%。"
STEP = SimpleNamespace(dimension="compliance", text=TEXT)
RULES = [{"rule_id": "COMP-001", "dimension": "compliance",
          "severity": "major", "desc": "绝对化表述需有依据"}]


class _LegacyResult:
    """模拟 `tools/llm_client.LLMResult`：不是 ModelReply，但字段齐全。"""

    def __init__(self, content, model="legacy-model"):
        self.content = content
        self.model = model
        self.finish_reason = "stop"
        self.usage = {"input": 10, "output": 20}
        self.latency_ms = 42


class _StubClient:
    """满足端口的最小实现：返回一段合法 JSON 数组。"""

    def __init__(self, model, content=None):
        self.model = model
        self.content = content if content is not None else (
            '[{"rule_id": "COMP-001", "quote": "市场占有率达到 90%",'
            ' "severity": "major", "statement": "缺少来源", "confidence": 0.9}]'
        )
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return ModelReply(content=self.content, model=self.model,
                          finish_reason="stop", usage={"input": 1, "output": 2},
                          latency_ms=7)


class AdapterTests(unittest.TestCase):
    def test_adapter_satisfies_the_port(self):
        self.assertIsInstance(
            ChatClientAdapter(_StubClient("m")), ModelClient)

    def test_legacy_result_is_normalized(self):
        client = ChatClientAdapter(SimpleNamespace(
            chat=lambda messages: _LegacyResult("[]")))
        reply = client.chat([{"role": "user", "content": "hi"}])
        self.assertIsInstance(reply, ModelReply)
        self.assertEqual(reply.content, "[]")
        self.assertEqual(reply.model, "legacy-model")
        self.assertEqual(reply.finish_reason, "stop")
        self.assertEqual(dict(reply.usage), {"input": 10, "output": 20})
        self.assertEqual(reply.latency_ms, 42)

    def test_model_reply_passes_through(self):
        original = ModelReply(content="ok", model="m")
        client = ChatClientAdapter(SimpleNamespace(chat=lambda messages: original))
        self.assertIs(client.chat([]), original)

    def test_missing_fields_fall_back_to_defaults(self):
        client = ChatClientAdapter(SimpleNamespace(
            chat=lambda messages: SimpleNamespace(content="x")))
        reply = client.chat([])
        self.assertEqual(reply.content, "x")
        self.assertEqual(reply.model, "")
        self.assertEqual(dict(reply.usage), {})
        self.assertEqual(reply.latency_ms, 0)

    def test_client_without_chat_is_rejected_at_assembly(self):
        with self.assertRaises(TypeError):
            ChatClientAdapter(object())

    def test_as_model_client_does_not_wrap_twice(self):
        adapter = ChatClientAdapter(_StubClient("m"))
        self.assertIs(as_model_client(adapter), adapter)


class SettingsTests(unittest.TestCase):
    def test_model_comes_from_the_client(self):
        client = _StubClient("vendor-x")
        self.assertEqual(settings_for(client).model, "vendor-x")

    def test_overrides_win(self):
        client = _StubClient("vendor-x")
        settings = settings_for(client, max_input_chars=1000)
        self.assertEqual(settings.max_input_chars, 1000)
        self.assertEqual(settings.model, "vendor-x")

    def test_unknown_override_is_ignored(self):
        settings = settings_for(_StubClient("m"), base_url="http://x")
        self.assertEqual(settings, ModelClientSettings(model="m"))

    def test_defaults_are_conservative(self):
        settings = ModelClientSettings()
        self.assertFalse(settings.cache, "注入客户端时默认不开缓存")
        self.assertGreater(settings.max_input_chars, 0)


class SwapProviderTests(unittest.TestCase):
    """同一段审查代码，换两个不同「供应商」，都能跑完。"""

    def _review_with(self, client):
        fn, meta = review.make_llm_review_fn(
            RULES, TEXT, "text", model_client=client)
        self.assertIsNotNone(fn, "注入客户端时不该返回 None")
        return fn, meta

    def test_injected_client_needs_no_config(self):
        fn, meta = self._review_with(_StubClient("vendor-a"))
        self.assertTrue(meta["available"])
        self.assertEqual(meta["source"], "injected")
        self.assertEqual(meta["model"], "vendor-a")

    def test_real_review_path_runs_on_injected_client(self):
        client = _StubClient("vendor-a")
        fn, _ = self._review_with(client)
        result = fn(STEP)

        self.assertEqual(client.calls, 1, "应该真的调了一次注入的客户端")
        self.assertEqual(result["model"], "vendor-a")
        self.assertFalse(result["cached"])
        self.assertGreater(result["raw_chars"], 0,
                           "模型返回的内容必须流过解析，不是空跑")
        self.assertIn("issues", result)
        self.assertIn("dropped", result)

    def test_two_providers_share_one_code_path(self):
        first, meta_a = self._review_with(_StubClient("vendor-a"))
        second, meta_b = self._review_with(_StubClient("vendor-b"))
        self.assertEqual(meta_a["model"], "vendor-a")
        self.assertEqual(meta_b["model"], "vendor-b")
        self.assertEqual(first(STEP)["model"], "vendor-a")
        self.assertEqual(second(STEP)["model"], "vendor-b")

    def test_truncated_output_is_reported(self):
        client = _StubClient("vendor-a", content="")
        client.chat = lambda messages: ModelReply(
            content="", model="vendor-a", finish_reason="length")
        fn, _ = self._review_with(client)
        result = fn(STEP)
        self.assertTrue(result["output_truncated"],
                        "finish_reason=length 必须标记为「没写完」")
        self.assertTrue(result["dropped"])


if __name__ == "__main__":
    unittest.main()
