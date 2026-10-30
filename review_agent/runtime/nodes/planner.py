# -*- coding: utf-8 -*-
"""规划节点（Planner）。

职责：在开跑之前把「这次按什么范围审」写成一份计划，随 Run 落盘。

为什么值得单独一个阶段：
    旧实现没有任何"计划"可言——审什么维度由 manifest 现算、跑哪些检查由循环现定，
    事后想回答「这份报告当时覆盖了哪些维度」只能去翻 coverage 块和日志。
    计划落盘之后，`plan.dimensions` 与 `plan.handlers` 就是**当时承诺审什么**，
    和最终的 `findings` / `coverage` 一对比，漏审立刻显形。

不调模型、不读文件：输入全部来自 Profile 与 manifest 投影（`ctx.config`）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from ...domain import Stage
from ..node import NodeContext, NodeResult

NAME: Final = "planner"


def build_plan(
    dimensions: Sequence[str],
    handlers: Mapping[str, str],
    checks: Sequence[str] = (),
) -> dict[str, Any]:
    """把「要审什么」写成一份可落盘的计划（纯函数）。"""
    dims = [str(d) for d in dimensions if str(d or "").strip()]
    bound = [(dim, str(handlers.get(dim) or "")) for dim in dims]
    return {
        "dimensions": dims,
        "handlers": {dim: key for dim, key in bound},
        "checks": [str(c) for c in checks if str(c or "").strip()],
        "local_checks": sum(1 for _, key in bound if key),
    }


class PlannerNode:
    """planning 阶段节点：产出本次审查的计划。"""

    name: Final = NAME
    stage: Final = Stage.PLANNING
    produces: Final = frozenset({"plan"})

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.PLANNING:
            return NodeResult.fail(
                f"{NAME} 只处理 planning 阶段，当前状态在 {state.stage.value}")
        dimensions = tuple(ctx.config.get("dimensions") or state.profile.dimensions)
        if not dimensions:
            return NodeResult.fail(
                "计划为空：Profile 未声明 dimensions，config 也没给")
        handlers = ctx.config.get("handlers") or {}
        if not isinstance(handlers, Mapping):
            return NodeResult.fail("config['handlers'] 必须是 mapping（维度 → 处理器名）")
        plan = build_plan(dimensions, handlers, ctx.config.get("checks") or ())
        warnings: tuple[str, ...] = ()
        if not plan["local_checks"] and not plan["checks"]:
            # 没有任何本地检查 = 这次只能靠语义审；纯规则模式下等于没审，
            # 必须让上层看见，而不是产出一份"干净"的空报告。
            warnings = ("计划里没有任何本地检查（无 handlers、无 checks）："
                        "本次审查只可能有语义审结论",)
        return NodeResult.ok(output={"plan": plan}, warnings=warnings)
