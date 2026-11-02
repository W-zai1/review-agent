#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_tool_limits.py —— 工具执行的三道护栏 + 出网权限档（批次 2 / C7）。

为什么单独立一个文件：`test_tool_registry.py` 钉的是「管线顺序与权限拒绝」，
这里钉的是**执行约束**——它们是"跑得太久 / 调得太多 / 结果太大"三类事故的
最后一道闸，而这三类事故的共同点是：**不会报错，只会让审查悄悄变慢或变糊**。

三个约束缺省全关（`None`），所以对既有调用点零影响；只在显式打开时生效。
`Permission.RESEARCH` 是另一个方向的事：出网等于把材料送给第三方，默认必须不给。
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.capabilities import (                 # noqa: E402
    BaseTool,
    Permission,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
)
from review_agent.capabilities.tools import (           # noqa: E402
    DEFAULT_POLICY,
    ToolBudgetError,
    default_permissions,
)

REVIEW_ID = "R-20261101-001"
RUN_ID = f"{REVIEW_ID}-RUN01"


def context(**kwargs):
    return ToolContext(review_id=REVIEW_ID, run_id=RUN_ID, **kwargs)


class EchoTool(BaseTool):
    """记录自己被调了几次，可配置延迟与输出大小。"""

    name = "test.echo"
    description = "回显入参（测试用）"
    permission = Permission.ANALYZE

    def __init__(self, *, delay=0.0, payload=None):
        self.delay = delay
        self.payload = payload if payload is not None else {"ok": True}
        self.calls = 0

    async def execute(self, arguments, context):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return ToolResult.success(self.payload)


class NetTool(EchoTool):
    name = "test.web"
    description = "假装要出网（测试用）"
    permission = Permission.RESEARCH


class ResearchPermissionTests(unittest.TestCase):
    def test_research_is_a_separate_permission_and_off_by_default(self):
        self.assertIn(Permission.RESEARCH, set(Permission))
        self.assertFalse(DEFAULT_POLICY[Permission.RESEARCH],
                         "出网必须显式授权：默认给等于把材料默认送出去")
        self.assertNotIn(Permission.RESEARCH, default_permissions())
        self.assertIn(Permission.READ, default_permissions())
        self.assertIn(Permission.ANALYZE, default_permissions())

    def test_research_tool_is_denied_by_default(self):
        registry = ToolRegistry()
        registry.register(NetTool())
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(registry.execute("test.web", {}, context()))
        self.assertIn("research", str(ctx.exception))

    def test_research_tool_runs_when_explicitly_granted(self):
        registry = ToolRegistry()
        registry.register(NetTool())
        granted = context(permissions=frozenset({Permission.READ, Permission.RESEARCH}))
        result, invocation = asyncio.run(registry.execute("test.web", {}, granted))
        self.assertTrue(result.ok)
        self.assertEqual(invocation.permission, Permission.RESEARCH)


class BudgetTests(unittest.TestCase):
    def test_default_budget_is_unlimited(self):
        registry = ToolRegistry()
        tool = EchoTool()
        registry.register(tool)
        for _ in range(5):
            asyncio.run(registry.execute("test.echo", {}, context()))
        self.assertEqual(tool.calls, 5)

    def test_budget_stops_runaway_dispatch(self):
        registry = ToolRegistry(max_calls=2)
        tool = EchoTool()
        registry.register(tool)
        asyncio.run(registry.execute("test.echo", {}, context()))
        asyncio.run(registry.execute("test.echo", {}, context()))
        with self.assertRaises(ToolBudgetError):
            asyncio.run(registry.execute("test.echo", {}, context()))
        self.assertEqual(tool.calls, 2, "超预算的调用不该真的执行到工具里")
        self.assertEqual(registry.calls, 2)

    def test_budget_error_is_not_a_tool_error(self):
        """配额拒绝与工具失败要能分开：调用方对两者的处理不同。"""
        self.assertTrue(issubclass(ToolBudgetError, ToolError))
        self.assertIsNot(ToolBudgetError, ToolError)


class TimeoutTests(unittest.TestCase):
    def test_timeout_becomes_failure_result_not_exception(self):
        registry = ToolRegistry(timeout=0.01)
        registry.register(EchoTool(delay=0.2))
        result, invocation = asyncio.run(
            registry.execute("test.echo", {}, context()))
        self.assertFalse(result.ok)
        self.assertTrue(any("超时" in e for e in result.errors), result.errors)
        self.assertFalse(invocation.ok)
        self.assertGreaterEqual(invocation.latency_ms, 10)

    def test_fast_tool_is_unaffected(self):
        registry = ToolRegistry(timeout=5)
        registry.register(EchoTool())
        result, _ = asyncio.run(registry.execute("test.echo", {}, context()))
        self.assertTrue(result.ok)

    def test_exception_inside_tool_is_reported_as_failure(self):
        class Boom(BaseTool):
            name = "test.boom"
            description = "必炸（测试用）"

            async def execute(self, arguments, context):
                raise RuntimeError("炸了")

        registry = ToolRegistry(timeout=5)
        registry.register(Boom())
        result, invocation = asyncio.run(
            registry.execute("test.boom", {}, context()))
        self.assertFalse(result.ok)
        self.assertTrue(any("RuntimeError" in e for e in result.errors))
        self.assertFalse(invocation.ok, "工具抛异常也要留审计记录")


class OutputSizeTests(unittest.TestCase):
    def test_oversized_output_is_rejected(self):
        registry = ToolRegistry(max_bytes=200)
        registry.register(EchoTool(payload={"blob": "x" * 5000}))
        result, _ = asyncio.run(registry.execute("test.echo", {}, context()))
        self.assertFalse(result.ok)
        self.assertTrue(any("输出过大" in e for e in result.errors), result.errors)

    def test_small_output_passes(self):
        registry = ToolRegistry(max_bytes=4096)
        registry.register(EchoTool(payload={"k": "v"}))
        result, _ = asyncio.run(registry.execute("test.echo", {}, context()))
        self.assertTrue(result.ok)
        self.assertEqual(result.data["k"], "v")


class InvocationIdTests(unittest.TestCase):
    def test_result_carries_id_matching_audit_record(self):
        seen = []
        registry = ToolRegistry(on_invoke=seen.append)
        registry.register(EchoTool())
        result, invocation = asyncio.run(
            registry.execute("test.echo", {}, context()))
        self.assertTrue(result.invocation_id)
        self.assertEqual(result.invocation_id, invocation.invocation_id)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].invocation_id, invocation.invocation_id,
                         "审计回调收到的必须是同一次调用")

    def test_ids_are_unique_across_calls(self):
        registry = ToolRegistry()
        registry.register(EchoTool())
        ids = {asyncio.run(registry.execute("test.echo", {}, context()))[0].invocation_id
               for _ in range(5)}
        self.assertEqual(len(ids), 5)

    def test_non_tool_result_still_rejected(self):
        class Bad(BaseTool):
            name = "test.bad"
            description = "返回非 ToolResult（测试用）"

            async def execute(self, arguments, context):
                return {"not": "a result"}

        registry = ToolRegistry(timeout=5)
        registry.register(Bad())
        with self.assertRaises(ToolError) as ctx:
            asyncio.run(registry.execute("test.bad", {}, context()))
        self.assertIn("必须是 ToolResult", str(ctx.exception))

    def test_describe_exposes_new_permission(self):
        registry = ToolRegistry()
        registry.register(NetTool())
        described = registry.describe()[0]
        self.assertEqual(described["permission"], "research")


class PipelineWiringTests(unittest.TestCase):
    """生产链路（`build_rule_registry`）是否真的把调用记下来了。

    这一层测的是**接线**而不是工具本身：`ToolRegistry(on_invoke=...)` 在
    `review.build_rule_registry` 里是否被传进去。它在改造前是 `ToolRegistry()`——
    于是 `ToolInvocation` 生成完就被丢弃，报告和审计里都查不到"这次跑了哪些工具"。
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import agent_spec                                  # noqa: E402
        import review                                      # noqa: E402
        self.review = review
        self.agent_spec = agent_spec
        self.session = review.Session("R-20261101-001", "x.md", "doc")
        self.run_id = "R-20261101-001-RUN01"
        self.registry = review.build_rule_registry(
            self.session, "本公司营业收入 100 亿元。", [],
            {"target_path": None, "target_type": "doc"}, self.run_id,
            agent_spec.load_agent("doc-review"))

    def _context(self):
        return self.review.ToolContext(
            review_id="R-20261101-001", run_id=self.run_id, actor="pipeline",
            permissions=frozenset({self.review.Permission.READ,
                                   self.review.Permission.ANALYZE,
                                   self.review.Permission.WRITE}))

    def test_invocation_reaches_the_session(self):
        result, invocation = asyncio.run(self.registry.execute(
            "builtin.fact", {"dimension": "fact"}, self._context()))
        self.assertTrue(result.ok)
        self.assertEqual(len(self.session.tool_invocations), 1)
        recorded = self.session.tool_invocations[0]
        self.assertEqual(recorded["tool"], "builtin.fact")
        self.assertEqual(recorded["invocation_id"], result.invocation_id)
        self.assertEqual(recorded["invocation_id"], invocation.invocation_id)
        self.assertEqual(recorded["actor"], "pipeline")

    def test_registry_only_wraps_handlers_so_permission_is_write(self):
        """现状记录：`_HandlerTool` 一律声明 WRITE（批次 2 之后再逐个降级）。

        钉住它是为了让"降级"这件事有测试可依据：哪天把纯检查类降到 ANALYZE，
        这条会失败，改的人必须解释为什么安全。
        """
        result, invocation = asyncio.run(self.registry.execute(
            "builtin.fact", {"dimension": "fact"}, self._context()))
        self.assertTrue(result.ok)
        self.assertEqual(invocation.permission, self.review.Permission.WRITE)


if __name__ == "__main__":
    unittest.main()
