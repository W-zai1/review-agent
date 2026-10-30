#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_rule_dispatch.py —— 规则分发走 Tool Registry。

旧实现在 `run_agent_steps` 里直接 `resolve_handler(key)(session, ...)`：
没有统一入参校验、没有权限判定、没有「这个处理器是否注册」的单一事实源。
现在分发管线是 `registry.execute()`（存在性 → 权限 → 执行 → 结果），
处理器本身仍由 `_HandlerTool` 包着旧实现（阶段 3 再逐个纯化）。

这里钉住四件事：
    1. 处理器注册成工具，名字合法（`builtin:fact` → `builtin.fact`），权限是 WRITE
    2. 调用要带 WRITE；没有 WRITE 时拒绝执行（不是「静默跑」）
    3. 未知处理器报 Agent 配置错误（旧语义：这是配置问题，不是崩溃）
    4. 执行产出的 issue 被编码成 Finding，且 dimension 传到了处理器上下文
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402
from review_agent.capabilities import Permission, ToolContext, ToolError  # noqa: E402
from review_agent.runtime.nodes import RuleStep  # noqa: E402

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"

# 真实注册表用例要按 agent 定义解析目标，依赖本地私有资产（targets/ 样本 + agents/）；
# 公开 clone 不含这两者（见 .gitignore），缺失时跳过本用例，本地带资产时照常运行。
PRIVATE_OK = (ROOT / "targets" / "contract_001.md").exists() and (ROOT / "agents").is_dir()

ISSUE = {
    "issue_id": "ISS-0001", "dimension": "fact", "severity": "major",
    "quote": "截止目前，项目已完成 20 个里程碑节点", "location": "第 1 句",
    "rule_id": "FACT-001", "rule_text": "事实须准确",
    "evidence": "与事实库冲突", "tool": "fake", "confidence": 0.8,
    "confidence_basis": "rule", "suggestion": "改正", "open_discovery": False,
}


class FakeHandler:
    def __init__(self, issue=None):
        self.issue = issue
        self.seen_dimension = None
        self.calls = 0

    def __call__(self, session, text, active, ctx):
        self.calls += 1
        self.seen_dimension = ctx.get("dimension")
        if self.issue:
            session.issues.append(dict(self.issue))
        return session


def session():
    return review.Session(REVIEW_ID, "targets/a.md", "fact")


def context(*, permissions=None, actor="pipeline"):
    return ToolContext(
        review_id=REVIEW_ID, run_id=RUN_ID, actor=actor,
        permissions=frozenset(permissions or {Permission.WRITE}),
    )


class HandlerToolTests(unittest.TestCase):
    def make_tool(self, handler, *, run_id=RUN_ID):
        return review._HandlerTool(
            "builtin:fake", handler, session=session(), text="原文",
            active=[], legacy_ctx={}, run_id=run_id)

    def test_tool_name_is_legal_and_permission_is_write(self):
        tool = self.make_tool(FakeHandler())
        self.assertEqual(tool.name, "builtin.fake")
        self.assertIs(tool.permission, Permission.WRITE)

    def test_execute_returns_encoded_findings(self):
        handler = FakeHandler(ISSUE)
        tool = self.make_tool(handler)
        result = asyncio.run(tool.execute({"dimension": "fact"}, context()))
        self.assertTrue(result.ok)
        findings = result.data["findings"]
        self.assertEqual([f.id for f in findings], ["ISS-0001"])
        self.assertEqual(handler.seen_dimension, "fact")

    def test_dimension_absent_keeps_previous_context(self):
        handler = FakeHandler(ISSUE)
        legacy_ctx = {"dimension": "safety"}
        tool = review._HandlerTool(
            "builtin:fake", handler, session=session(), text="原文",
            active=[], legacy_ctx=legacy_ctx, run_id=RUN_ID)
        asyncio.run(tool.execute({}, context()))
        self.assertEqual(handler.seen_dimension, "safety")

    def test_unencodable_issue_is_kept_out_of_findings_with_decision(self):
        bad = dict(ISSUE, severity="critical")
        handler = FakeHandler(bad)
        session_ = session()
        tool = review._HandlerTool(
            "builtin:fake", handler, session=session_, text="原文",
            active=[], legacy_ctx={}, run_id=RUN_ID)
        result = asyncio.run(tool.execute({"dimension": "fact"}, context()))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["findings"], ())
        self.assertEqual(result.data["unencoded"], 1)
        self.assertTrue(any(d["decision"] == "规则命中未能编成领域对象"
                            for d in session_.decisions))


class RegistryExecutorTests(unittest.TestCase):
    def build(self, handler):
        registry = review.ToolRegistry()
        registry.register(review._HandlerTool(
            "builtin:fake", handler, session=session(), text="原文",
            active=[], legacy_ctx={}, run_id=RUN_ID))
        return registry

    def test_executor_runs_registered_handler(self):
        handler = FakeHandler(ISSUE)
        executor = review.rule_executor_from(self.build(handler), REVIEW_ID, RUN_ID)
        findings = asyncio.run(executor(RuleStep("builtin:fake", "fact")))
        self.assertEqual([f.id for f in findings], ["ISS-0001"])
        self.assertEqual(handler.calls, 1)

    def test_unknown_handler_is_a_configuration_error(self):
        executor = review.rule_executor_from(self.build(FakeHandler()), REVIEW_ID, RUN_ID)
        with self.assertRaises(review.agent_spec.AgentError):
            asyncio.run(executor(RuleStep("builtin:missing")))

    def test_missing_write_permission_is_rejected(self):
        registry = self.build(FakeHandler(ISSUE))
        context_without_write = context(permissions={Permission.READ})
        with self.assertRaises(ToolError):
            asyncio.run(registry.execute("builtin.fake", {}, context_without_write))

    @unittest.skipUnless(PRIVATE_OK,
                         "私有审查资产缺失（targets/ 样本或 agents/ 定义不在公开仓库），"
                         "跳过真实注册表用例")
    def test_real_registry_contains_every_builtin_handler(self):
        agent, _ = review.agent_spec.resolve(
            ROOT / "targets" / "contract_001.md", "合同正文")
        registry = review.build_rule_registry(
            session(), "原文", [], {}, RUN_ID, agent)
        names = set(registry.names())
        for key in review.BUILTIN_HANDLERS:
            self.assertIn(review._tool_name(key), names)


if __name__ == "__main__":
    unittest.main()
