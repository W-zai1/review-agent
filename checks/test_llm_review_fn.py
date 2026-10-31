#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_llm_review_fn.py —— 真实模型调用的接线（不发请求）。

语义审的执行器早就有了，差的只是「一次模型调用」怎么接到 `llm_review.review_dimension`。
这里把 `load_config` / `LLMClient` / `review_dimension` 全部替换成假对象，
验证接线本身：

    1. 配置不可用 → 返回 (None, reason)，**不偷偷降级**
    2. 显式要求模型（use_llm=True）却不可用 → ConfigError，不产出「纯规则」结论
    3. 可用时：按维度取规则、任务书带目的、事实库进 prompt、quote 对全文校验
    4. `run_typed_pipeline(use_llm=True)` 不可用时报错；可用时把语义节点挂进 analysis
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402
from review_agent.domain import ConfigError  # noqa: E402

# 流水线级用例要在真实样本上跑，依赖本地私有资产（targets/ 样本 + agents/ Agent 定义）；
# 公开 clone 不含这两者（见 .gitignore），缺失时整类跳过，本地带资产时照常运行。
PRIVATE_OK = (ROOT / "targets" / "contract_001.md").exists() and (ROOT / "agents").is_dir()


def fake_cfg(*, available=False):
    return SimpleNamespace(
        available=available, enabled=True, model="fake-model",
        base_url="http://localhost:1/v1", source="test",
        max_input_chars=8000, chunk_chars=6000, cache=False,
    )


def fake_client():
    """满足 ModelClient 端口的最小对象（`review_dimension` 在这些用例里被替换掉）。"""
    return SimpleNamespace(model="fake-model",
                           chat=lambda messages: None)


class ReviewFnTests(unittest.TestCase):
    def test_unavailable_returns_none_with_reason(self):
        with mock.patch.object(review.llm_client, "load_config",
                               return_value=fake_cfg(available=False)):
            fn, meta = review.make_llm_review_fn([], "原文", "contract")
        self.assertIsNone(fn)
        self.assertFalse(meta["available"])
        self.assertTrue(meta["reason"])

    def test_config_error_is_reported_not_swallowed(self):
        with mock.patch.object(review.llm_client, "load_config",
                               side_effect=RuntimeError("配置文件坏了")):
            fn, meta = review.make_llm_review_fn([], "原文", "contract")
        self.assertIsNone(fn)
        self.assertIn("配置文件坏了", meta["reason"])

    def test_available_builds_a_callable_that_uses_dimension_rules(self):
        seen = {}

        def fake_review_dimension(client, dim, rules, chunk, cfg, **kwargs):
            seen.update(dim=dim, rules=rules, chunk=chunk, cfg=cfg, **kwargs)
            return {"issues": [], "dropped": [], "retracted": [], "model": "fake-model",
                    "usage": {"input": 1, "output": 1}, "cached": False,
                    "truncated": False, "output_truncated": False, "finish_reason": "stop"}

        active = [{"rule_id": "COMP-004", "dimension": "compliance"},
                  {"rule_id": "FACT-001", "dimension": "fact"}]
        with mock.patch.object(review.llm_client, "load_config",
                               return_value=fake_cfg(available=True)), \
                mock.patch.object(review.llm_client, "LLMClient",
                                  return_value=fake_client()), \
                mock.patch.object(review.llm_review, "review_dimension",
                                  side_effect=fake_review_dimension):
            fn, meta = review.make_llm_review_fn(active, "完整原文", "contract",
                                                 purpose_info=None)
            self.assertTrue(meta["available"])
            step = SimpleNamespace(dimension="compliance", text="这一块原文")
            result = fn(step)

        self.assertEqual(result["model"], "fake-model")
        self.assertEqual(seen["dim"], "compliance")
        self.assertEqual([r["rule_id"] for r in seen["rules"]], ["COMP-004"],
                         "只应把该维度的规则传给模型")
        self.assertEqual(seen["chunk"], "这一块原文")
        self.assertEqual(seen["full_text"], "完整原文",
                         "quote 必须对全文校验，不能只对块")


@unittest.skipUnless(PRIVATE_OK,
                     "私有审查资产缺失（targets/ 样本或 agents/ 定义不在公开仓库），"
                     "跳过流水线 LLM 接线用例")
class PipelineLlmTests(unittest.TestCase):
    def setUp(self):
        self.rules = review.load_rules()
        self.target = (ROOT / "targets" / "contract_001.md").resolve()

    def test_explicit_llm_without_config_raises(self):
        with mock.patch.object(review.llm_client, "load_config",
                               return_value=fake_cfg(available=False)):
            with self.assertRaises(ConfigError):
                review.run_typed_pipeline(self.target, self.rules,
                                          review_id="R-20261030-995",
                                          use_llm=True)

    def test_auto_llm_degrades_quietly_but_reports_meta(self):
        meta = {}
        with mock.patch.object(review.llm_client, "load_config",
                               return_value=fake_cfg(available=False)):
            state, outcome, _ = review.run_typed_pipeline(
                self.target, self.rules, review_id="R-20261030-996",
                use_llm=None, llm_meta_out=meta)
        self.assertEqual(outcome.status, "completed")
        self.assertFalse(meta["available"])
        self.assertIn("reason", meta)

    def test_semantic_node_joins_analysis_when_llm_available(self):
        def fake_review_dimension(client, dim, rules, chunk, cfg, **kwargs):
            return {"issues": [], "dropped": [], "retracted": [],
                    "model": "fake-model", "usage": {"input": 0, "output": 0},
                    "cached": False, "truncated": False,
                    "output_truncated": False, "finish_reason": "stop"}

        with mock.patch.object(review.llm_client, "load_config",
                               return_value=fake_cfg(available=True)), \
                mock.patch.object(review.llm_client, "LLMClient",
                                  return_value=fake_client()), \
                mock.patch.object(review.llm_review, "review_dimension",
                                  side_effect=fake_review_dimension):
            meta = {}
            state, outcome, _ = review.run_typed_pipeline(
                self.target, self.rules, review_id="R-20261030-997",
                use_llm=True, llm_meta_out=meta)
        self.assertEqual(outcome.status, "completed", outcome.errors)
        self.assertTrue(meta["available"])
        self.assertIn("semantic_stats", state.artifacts,
                      "语义审节点应参与 analysis 阶段")


if __name__ == "__main__":
    unittest.main()
