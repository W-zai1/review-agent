# -*- coding: utf-8 -*-
"""工具协议与注册表（Tool Specification v1）。

旧实现的问题不是「工具太少」，而是「工具没有边界」：
    · 42 个模块平铺在 tools/，靠 sys.path 注入互相 import，谁都能调谁
    · 维度处理器用 `builtin:` / `pack:` 字符串在 review.py 里硬编码分发
    · 没有权限概念：一个分析工具可以直接写文件、改规则、发网络请求
    · 调用记录是 `s.tool(...)` 手写的，漏写就没记录，字段也不统一

这里定四件事（对应 docs/TOOL_SPECIFICATION.md）：
    1. 工具声明：name / description / permission / arguments_type
    2. 统一执行管线：参数校验 → 权限校验 → 执行 → 结果校验 → 审计事件
    3. 权限分级：READ / ANALYZE / WRITE / APPROVE，APPROVE 只能由人持有
    4. 外部文档是**不可信输入**：从文档里抽出来的内容一律标 untrusted，
       节点不得把工具输出当指令执行

刻意没有做成插件系统：阶段 1 只需要「一张注册表 + 一条执行管线」，
多一层抽象就多一层没人维护的间接。
"""
from __future__ import annotations

import abc
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..domain import ids as id_tools

_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_.]{1,63}$")


class ToolError(Exception):
    """工具层异常基类。"""


class ToolArgumentError(ToolError):
    """入参不符合工具声明的 schema。"""


class ToolPermissionError(ToolError):
    """当前上下文没有执行该工具所需权限。"""


class DuplicateToolError(ToolError):
    """同名工具重复注册（会导致「调到的不是想调的那个」）。"""


class Permission(StrEnum):
    """工具权限等级。

    READ    读取材料          默认允许
    ANALYZE 分析材料          默认允许
    WRITE   修改审查数据      受控（需要显式授权）
    APPROVE 批准结果          只能由人持有，任何自动流程都不得授予
    """

    READ = "read"
    ANALYZE = "analyze"
    WRITE = "write"
    APPROVE = "approve"


# 默认策略：分析类工具开箱即用，写入必须显式授权，批准永远要人。
DEFAULT_POLICY: Mapping[Permission, bool] = MappingProxyType({
    Permission.READ: True,
    Permission.ANALYZE: True,
    Permission.WRITE: False,
    Permission.APPROVE: False,
})


def default_permissions() -> frozenset[Permission]:
    return frozenset(p for p, allowed in DEFAULT_POLICY.items() if allowed)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """一次工具调用的上下文（谁、在哪个 run、有什么权限、是不是演练）。"""

    review_id: str
    run_id: str
    permissions: frozenset[Permission] = field(default_factory=default_permissions)
    actor: str = "system"
    dry_run: bool = False

    def __post_init__(self) -> None:
        id_tools.parse_review_id(self.review_id)
        run_review, _ = id_tools.parse_run_id(self.run_id)
        if run_review != self.review_id:
            raise ToolError(
                f"run_id {self.run_id} 不属于 review {self.review_id}")
        if Permission.APPROVE in self.permissions and self.actor == "system":
            # 计划里写了「APPROVE 必须人工授权」。如果允许 system 拿 APPROVE，
            # 这条约束在代码层面就失效了 —— 这里直接拒绝构造。
            raise ToolPermissionError(
                "APPROVE 权限不能授予系统主体（actor=system）：批准必须由人完成")
        if not self.actor.strip():
            raise ToolError("actor 不能为空：审计要知道是谁调的工具")

    def allows(self, permission: Permission) -> bool:
        return permission in self.permissions


@dataclass(frozen=True, slots=True)
class ToolResult:
    """工具执行结果。

    untrusted=True 表示结果内容来自待审文档（外部不可信输入）：
    调用方只能把它当**数据**，不得当指令执行，也不得据此提升权限。
    """

    ok: bool
    data: Mapping[str, Any] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    untrusted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))
        if self.ok and self.errors:
            raise ToolError("成功的 ToolResult 不应带 errors")
        if not self.ok and not self.errors:
            raise ToolError("失败的 ToolResult 必须说明原因（errors 不能为空）")

    @classmethod
    def success(cls, data: Mapping[str, Any] | None = None, *,
                warnings: tuple[str, ...] = (), untrusted: bool = False) -> "ToolResult":
        return cls(ok=True, data=data or {}, warnings=warnings, untrusted=untrusted)

    @classmethod
    def failure(cls, *errors: str, untrusted: bool = False) -> "ToolResult":
        return cls(ok=False, errors=tuple(errors), untrusted=untrusted)


class BaseTool(abc.ABC):
    """所有工具的实现基类。子类只声明元数据 + 实现 execute。"""

    name: str = ""
    description: str = ""
    permission: Permission = Permission.ANALYZE
    # dataclass / TypedDict / None；None = 接受任意关键字参数（此时由工具自己校验）
    arguments_type: type | None = None

    def validate_arguments(self, raw: Mapping[str, Any]) -> Any:
        """把原始 dict 变成强类型入参。

        为什么不用 Pydantic：项目运行时的硬依赖只有 PyYAML（CI 也只装它），
        领域层与工具层引入 Pydantic 会把部署底线改掉。dataclass 构造校验
        足够拦住「少传字段 / 字段名拼错 / 类型明显不对」，且零依赖。
        需要 JSON Schema 时由应用层（FastAPI 侧）另行生成，不在工具层做。
        """
        if self.arguments_type is None:
            return dict(raw)
        try:
            return self.arguments_type(**dict(raw))       # type: ignore[operator]
        except TypeError as e:
            raise ToolArgumentError(
                f"工具 {self.name} 入参不合法：{e}；"
                f"期望字段 {getattr(self.arguments_type, '__annotations__', {})}") from e

    @abc.abstractmethod
    async def execute(self, arguments: Any, context: ToolContext) -> ToolResult:
        """执行工具。实现必须是幂等的（同一入参 + 同一材料，结果可复现）。"""


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """一条工具调用审计记录（trace 的最小单元）。"""

    tool: str
    permission: Permission
    actor: str
    review_id: str
    run_id: str
    ok: bool
    error_count: int
    latency_ms: int
    argument_keys: tuple[str, ...]
    dry_run: bool = False


class ToolRegistry:
    """工具注册表 + 统一执行管线。

    管线顺序（少一步都出过事）：
        存在性 → 权限校验 → schema 校验 → 执行 → 结果校验 → 审计
    权限放在 schema 之前：无权执行的主体不该拿到「你这个入参写错了」这种
    关于工具内部形状的反馈。反过来，同一主体的合法调用仍会在执行前被 schema 拦住
    （`checks/test_tool_registry.py::test_argument_validation_happens_before_execution`）。
    """

    def __init__(self, *, on_invoke: Callable[[ToolInvocation], None] | None = None):
        self._tools: dict[str, BaseTool] = {}
        self._on_invoke = on_invoke

    def register(self, tool: BaseTool) -> None:
        name = getattr(tool, "name", "")
        if not _TOOL_NAME_RE.match(name):
            raise ToolError(f"工具名不合法：{name!r}（要求小写字母开头，可含 . _）")
        if not tool.description.strip():
            raise ToolError(f"工具 {name} 缺少 description：模型/人无法判断何时该调它")
        if name in self._tools:
            raise DuplicateToolError(f"工具 {name} 已注册，禁止重复实现同名工具")
        self._tools[name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as e:
            raise ToolError(f"未注册的工具：{name!r}") from e

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def describe(self) -> tuple[dict[str, Any], ...]:
        """给 Agent / UI 的工具清单（不含实现）。"""
        return tuple({
            "name": t.name,
            "description": t.description,
            "permission": t.permission.value,
            "arguments": sorted(getattr(t.arguments_type, "__annotations__", {}) or {}),
        } for t in self._tools.values())

    async def execute(self, name: str, arguments: Mapping[str, Any],
                      context: ToolContext) -> tuple[ToolResult, ToolInvocation]:
        tool = self.get(name)
        if not context.allows(tool.permission):
            raise ToolPermissionError(
                f"工具 {name} 需要 {tool.permission.value} 权限，"
                f"当前主体 {context.actor} 没有该权限")
        validated = tool.validate_arguments(arguments)
        started = time.perf_counter()
        result = await tool.execute(validated, context)
        latency = int((time.perf_counter() - started) * 1000)
        if not isinstance(result, ToolResult):
            raise ToolError(f"工具 {name} 返回了 {type(result).__name__}，必须是 ToolResult")
        invocation = ToolInvocation(
            tool=name, permission=tool.permission, actor=context.actor,
            review_id=context.review_id, run_id=context.run_id, ok=result.ok,
            error_count=len(result.errors), latency_ms=latency,
            argument_keys=tuple(sorted(arguments)), dry_run=context.dry_run,
        )
        if self._on_invoke is not None:
            self._on_invoke(invocation)
        return result, invocation
