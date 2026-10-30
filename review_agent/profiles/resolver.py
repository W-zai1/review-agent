# -*- coding: utf-8 -*-
"""Profile 解析：把「按哪套标准审」的四处配置合成一个快照。

旧实现的判定口径散在四个地方，缺一不可：

    agents/<id>/manifest.json       维度、规则来源、类型路由、任务书默认值
    config/scenarios.yaml           本次关掉哪些规则（用途不同，尺度不同）
    config/format_specs.yaml        版式按哪套数值标准判（仅 docx）
    config/charter.json             任务书（目的、受众、后果、严格度）

四者叠加才是「这次按什么标准审」。问题是：叠加发生在 run_one 里，靠局部变量传递，
落盘时又被拆回 report 的三个块（agent / scenario / format_spec），
半年后想回答「当时到底按哪套标准判」要把三块重新脑内合并。

`resolve_profile()` 是那个合并动作的**唯一实现**：输入是已解析好的配置（不读文件、
不调模型），输出一个 `ReviewProfile` 快照。它同时被两处使用：

    · 历史报告适配器（review_agent/adapters/legacy_report.py）——把旧报告的三块合成快照
    · 阶段 2 的 Application ——把本次真实生效的配置合成快照，随 Run 落盘

为什么不用 Pydantic：见 docs/ARCHITECTURE.md ADR-001（运行时只依赖 PyYAML）。
为什么接受 Mapping 而不是具体类型：manifest / scenario / format_spec 的解析器还在
`tools/` 里，阶段 3 再迁；这里只约定「解析后的形状」，不复制它们的校验逻辑。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..domain import ReviewProfile

LEGACY_AGENT_ID = "legacy-unversioned"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str_list(value: Any) -> tuple[str, ...]:
    return tuple(str(x) for x in (value or ()) if str(x).strip())


def resolve_profile(
    agent: Mapping[str, Any] | None,
    *,
    target_type: str,
    scenario: Mapping[str, Any] | None = None,
    format_spec: Mapping[str, Any] | None = None,
    active_rules: Sequence[Mapping[str, Any]] = (),
    fallback_dimensions: Sequence[str] = (),
    source: str = "manifest",
) -> ReviewProfile:
    """合成一次审查的 Profile 快照。

    agent 允许为空（历史报告可能没有 agent 块），此时 id 记为 `legacy-unversioned`，
    语义是「这份材料按什么标准审的，报告自身说不清」——不冒充默认 Agent。
    """
    agent = _mapping(agent)
    scenario = _mapping(scenario) or None
    format_spec = _mapping(format_spec) or None

    agent_id = str(agent.get("id") or LEGACY_AGENT_ID)
    version_bits = [str(agent.get("version") or "unversioned")]
    rationale: list[str] = []

    if scenario:
        version_bits.append(f"scenario:{scenario.get('id')}@{scenario.get('version')}")
        rationale.append(f"情境 {scenario.get('name') or scenario.get('id')}")
    if format_spec:
        version_bits.append(f"format:{format_spec.get('id')}@{format_spec.get('version')}")
        rationale.append(f"格式规格 {format_spec.get('name') or format_spec.get('id')}")

    # 维度：manifest 优先按对象类型路由；没有路由信息时退回声明清单，
    # 再退回调用方给的 fallback（历史报告只能用 coverage.dimensions_checked）。
    by_type = _mapping(agent.get("dims_by_type"))
    dims: tuple[str, ...] = ()
    if by_type:
        dims = _str_list(by_type.get(target_type))
        if not dims:
            dims = _str_list(agent.get("default_dims"))
    if not dims:
        dims = tuple(str(d.get("id")) for d in (agent.get("dimensions") or ())
                     if isinstance(d, Mapping) and d.get("id"))
    if not dims:
        dims = _str_list(fallback_dimensions)

    rule_ids = tuple(
        str(r.get("rule_id")) for r in active_rules
        if isinstance(r, Mapping) and r.get("rule_id")
    )

    target_types = tuple(str(k) for k in by_type) if by_type else (
        str(target_type) if target_type else ())

    return ReviewProfile(
        id=agent_id,
        name=str(agent.get("name") or agent_id),
        version=";".join(version_bits),
        description=str(agent.get("description") or ""),
        target_types=target_types,
        dimensions=dims,
        rule_ids=rule_ids,
        source=source,
        rationale="；".join(rationale),
    )
