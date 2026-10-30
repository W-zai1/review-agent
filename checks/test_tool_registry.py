#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_tool_registry.py —— 工具协议与执行管线的公开不变量。

守的是什么：
    · 同名工具不能重复注册（否则「调到的不是想调的那个」）
    · 权限必须先于执行：READ/ANALYZE 默认放行，WRITE 默认受控，APPROVE 只能由人持有
    · 参数不合法要在执行前被拦住，且错误信息能定位到字段
    · 审计记录必须每条调用都产生，字段足够回答「谁、在哪个 run、调了什么、多久」

运行：
    python -m unittest discover -s checks -v
"""
import asyncio
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.capabilities import (      # noqa: E402
    BaseTool,
    DuplicateToolError,
    Permission,
    ToolArgumentError,
    ToolContext,
    ToolError,
    ToolPermissionError,
    ToolRegistry,
    ToolResult,
    default_permissions,
)

REVIEW_ID = "R-20261030-001"
RUN_ID = "R-20261030-001-RUN01"


@dataclass(frozen=True)
class MatchArgs:
    pattern: str
    text: str


class MatchTool(BaseTool):
    name = "regex.match"
    description = "在文本里匹配正则（示例工具，仅测试用）"
    permission = Permission.ANALYZE
    arguments_type = MatchArgs

    async def execute(self, arguments, context):
        import re
        hit = re.search(arguments.pattern, arguments.text) is not None
        return ToolResult.success({"match": hit, "dry_run": context.dry_run},
                                  untrusted=True)


class WriteTool(BaseTool):
    name = "state.write"
    description = "写审查数据（示例工具，仅测试用）"
    permission = Permission.WRITE

    async def execute(self, arguments, context):
        return ToolResult.success({"written": True})


class BrokenTool(BaseTool):
    name = "broken.result"
    description = "返回错误类型的工具（用于验证管线兜底）"
    permission = Permission.READ

    async def execute(self, arguments, context):
        return "not a ToolResult"


def context(**overrides) -> ToolContext:
    data = dict(review_id=REVIEW_ID, run_id=RUN_ID)
    data.update(overrides)
    return ToolContext(**data)


class RegistryTests(unittest.TestCase):
    def test_register_and_describe(self):
        registry = ToolRegistry()
        registry.register(MatchTool())
        self.assertEqual(registry.names(), ("regex.match",))
        described = registry.describe()[0]
        self.assertEqual(described["permission"], "analyze")
        self.assertEqual(described["arguments"], ["pattern", "text"])

    def test_duplicate_name_is_rejected(self):
        registry = ToolRegistry()
        registry.register(MatchTool())
        with self.assertRaises(DuplicateToolError):
            registry.register(MatchTool())

    def test_bad_tool_metadata_is_rejected(self):
        class NoDescription(BaseTool):
            name = "node.desc"
            permission = Permission.READ

            async def execute(self, arguments, co_context):
                return ToolResult.success()

        with self.assertRaises(ToolError):
            ToolRegistry().register(NoDescription())
        with self.assertRaises(ToolError):
            ToolRegistry().get("missing.tool")

    def test_execute_success_records_invocation(self):
        seen = []
        registry = ToolRegistry(on_invoke=seen.append)
        registry.register(MatchTool())
        result, invocation = asyncio.run(registry.execute(
            "regex.match", {"pattern": "10 亿", "text": "月活 10 亿"},
            context()))
        self.assertTrue(result.ok)
        self.assertTrue(result.untrusted, "文档派生内容必须标记为不可信输入")
        self.assertEqual(invocation.tool, "regex.match")
        self.assertEqual(invocation.actor, "system")
        self.assertEqual(invocation.argument_keys, ("pattern", "text"))
        self.assertEqual(len(seen), 1)

    def test_argument_validation_happens_before_execution(self):
        registry = ToolRegistry()
        registry.register(MatchTool())
        with self.assertRaises(ToolArgumentError) as ctx:
            asyncio.run(registry.execute("regex.match", {"pattern": "x"}, context()))
        self.assertIn("text", str(ctx.exception))

    def test_write_requires_explicit_permission(self):
        registry = ToolRegistry()
        registry.register(WriteTool())
        with self.assertRaises(ToolPermissionError):
            asyncio.run(registry.execute("state.write", {}, context()))
        allowed = context(permissions=default_permissions() | {Permission.WRITE},
                          actor="user:alice")
        result, _ = asyncio.run(registry.execute("state.write", {}, allowed))
        self.assertTrue(result.ok)

    def test_approve_cannot_be_granted_to_system(self):
        with self.assertRaises(ToolPermissionError):
            context(permissions=frozenset({Permission.APPROVE}), actor="system")
        human = context(permissions=frozenset({Permission.APPROVE}), actor="user:alice")
        self.assertTrue(human.allows(Permission.APPROVE))

    def test_invalid_tool_result_is_rejected(self):
        registry = ToolRegistry()
        registry.register(BrokenTool())
        with self.assertRaises(ToolError):
            asyncio.run(registry.execute("broken.result", {}, context()))

    def test_failed_result_must_explain(self):
        with self.assertRaises(ToolError):
            ToolResult(ok=False)
        with self.assertRaises(ToolError):
            ToolResult(ok=True, errors=("boom",))

    def test_dry_run_is_visible_to_tools(self):
        registry = ToolRegistry()
        registry.register(MatchTool())
        result, invocation = asyncio.run(registry.execute(
            "regex.match", {"pattern": "x", "text": "x"}, context(dry_run=True)))
        self.assertTrue(result.data["dry_run"])
        self.assertTrue(invocation.dry_run)


if __name__ == "__main__":
    unittest.main()
