# -*- coding: utf-8 -*-
"""决策节点（Decision Engine）。

职责：把「已定型的发现 + 争议 + 覆盖边界」变成一条可复算的 `Decision`。
不调模型、不读文件、不看时间（落盘时间戳除外）——裁定必须能被任何人拿同样输入算回来。

为什么它先于其它节点接管
------------------------
它是六个节点里最纯的一个：
    · 输入输出都是领域对象，没有解析、没有网络、没有 prompt；
    · 规则只有一份实现（`review_agent.domain.policy`），旧流程 S6 调的也是它；
    · 因此可以先在旧管线里「用节点算裁定」而不改变任何可观察行为，
      再用双跑证明新链路同结论——这是 REFACTOR_PLAN 阶段 2 要求的第一块。

与 docs/AGENT_RUNTIME.md §3 的对应：
    Decision Engine | decision 阶段 | 输入 findings/disputes/coverage/研究兜底 | 输出 decision

配置项（`ctx.config`，不是 `ctx.state.artifacts`）
--------------------------------------------------
    external_unverified : bool   库外事实未核完（research 兜底）
    coverage_complete   : bool   本次语义审是否覆盖全文
    rationale           : str    报告正文里的裁定说明（可选，缺省用策略理由）

为什么不走 artifacts：`STAGE_ARTIFACTS[decision]` 只登记了 `decision` 一个键，
`advance()` 会拒绝任何未登记的产物。把 research 结果塞进 artifacts 需要先改阶段契约
（阶段 2 的 Evidence / Cross Review 落地时一起做），这里用运行配置承载，
并登记为 AGENT_RUNTIME 的开放问题。
"""
from __future__ import annotations

import datetime
from collections.abc import Sequence
from typing import Final

from ...domain import (
    DEFAULT_POLICY,
    Coverage,
    Decision,
    DecisionPolicy,
    Dispute,
    Finding,
    Stage,
    consistency_issues,
    ids,
)
from ..node import NodeContext, NodeResult, surviving_findings

NAME: Final = "decision-engine"


def build_decision(
    findings: Sequence[Finding],
    *,
    run_id: str,
    disputes: Sequence[Dispute] = (),
    external_unverified: bool = False,
    coverage: Coverage | None = None,
    rationale: str = "",
    policy: DecisionPolicy = DEFAULT_POLICY,
    decision_seq: int = 1,
    decided_at: datetime.datetime | None = None,
) -> Decision:
    """把发现编成裁定。纯函数式：同样输入必然同样输出（除 decided_at 默认取当前时间）。

    这是 Decision Engine 的**唯一**裁定入口。旧管线（review.py S6）、历史报告复算、
    节点执行、单测都走它——不允许任何地方再写一份「blocker→fail」的 if。
    """
    draft = policy.decide(
        findings, disputes=disputes,
        external_unverified=external_unverified, coverage=coverage,
    )
    text = str(rationale or "").strip() or "；".join(draft.reasons)
    return Decision(
        id=ids.format_decision_id(decision_seq),
        run_id=run_id,
        verdict=draft.verdict,
        rationale=text,
        risk_level=draft.risk_level,
        reasons=draft.reasons,
        rule_refs=draft.rule_refs,
        finding_ids=draft.finding_ids,
        requires_human_review=draft.requires_human_review,
        policy_id=draft.policy_id,
        decided_at=decided_at or datetime.datetime.now().astimezone(),
    )


class DecisionEngineNode:
    """decision 阶段节点：读状态，产出 Decision，落状态前自检可复算。"""

    name: Final = NAME
    stage: Final = Stage.DECISION
    produces: Final = frozenset({"decision"})

    def __init__(self, policy: DecisionPolicy = DEFAULT_POLICY) -> None:
        self.policy = policy

    def _flag(self, ctx: NodeContext, key: str) -> bool:
        return bool(ctx.config.get(key))

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.DECISION:
            # 串台是配置错误，不是输入错误：不重试，直接终止。
            return NodeResult.fail(
                f"{NAME} 只处理 decision 阶段，当前状态在 {state.stage.value}")
        missing = state.missing_artifacts()
        if missing:
            return NodeResult.fail(
                f"decision 阶段缺少上游产物 {list(missing)}；不允许带着半成品出裁定")

        coverage = None
        if "coverage_complete" in ctx.config:
            coverage = Coverage(complete=bool(ctx.config["coverage_complete"]))

        try:
            decision = build_decision(
                surviving_findings(state),
                run_id=state.run_id,
                disputes=state.disputes,
                external_unverified=self._flag(ctx, "external_unverified"),
                coverage=coverage,
                rationale=str(ctx.config.get("rationale") or ""),
                policy=self.policy,
            )
        except Exception as e:                          # noqa: BLE001 - 节点边界统一转 NodeResult
            return NodeResult.fail(f"裁定无法生成：{type(e).__name__}: {e}")

        # 自检：拿同一份输入复算一遍。算不回来的裁定不允许进入报告。
        problems = consistency_issues(
            state.findings, decision.verdict, policy=self.policy,
            external_unverified=self._flag(ctx, "external_unverified"))
        if problems:
            return NodeResult.fail(
                *problems,
                "裁定无法自复算：拒绝落状态（见 ARCHITECTURE ADR-003）",
            )

        warnings: tuple[str, ...] = ()
        if decision.requires_human_review:
            warnings = (f"裁定 {decision.verdict.value} 需人工复核："
                        f"{'；'.join(decision.reasons)}",)
        return NodeResult.ok(decision=decision, warnings=warnings)
