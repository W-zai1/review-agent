# -*- coding: utf-8 -*-
"""交叉复核节点（Cross Reviewer）。

职责：同一片段被两个不同维度分别标记、且至少一方是 blocker/major 时，
判为**跨维度冲突**，产出 `Dispute` 交人工裁定，并把双方置信度各降 0.05（下限 0.5）。

为什么降权：两条结论指向同一句话，说明两边的判定至少有一边在看错重点；
降权不等于否定，但足以让它们在 `to_confirm`（置信度 < 0.7）里被人工看一眼。

规则与旧实现（`review.py::cross_review`）逐条等价：
    · 只查 `CROSS_PAIRS` 指定的两对视角（fact×safety、compliance×style）
    · quote 相似度 ≥ 0.6 才算「同一片段」
    · 两边都是 minor/nit 不算红线冲突（skip）
    · 每命中一次，双方各 -0.05，四舍五入到 2 位，下限 0.5
    · 同一对发现反复命中时，后续从**已降过的值**继续降（旧实现就地改 dict 的口径）

与运行时的关系：
    发现不可变，所以「降权」通过 `NodeResult.revisions` 表达（显式修订，
    由 `advance()` 按 id 替换）。不允许节点自己改上游对象——那正是旧实现
    「S5 里直接 i['confidence'] -= 0.05、改完只剩最终值」调不出来的原因。

本节点不做的两件事（旧实现在 S6 做，属于「折叠/聚合」，另立节点处理）：
    · `aggregate_repeats`（长文同类聚合）
    · `drop_covered_comp004` / `collapse_rule_overlap`（同片段归口折叠）
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

from ...domain import (
    Dispute,
    DisputeKind,
    DisputeStatus,
    Finding,
    Severity,
    Stage,
    ids,
)
from ..node import NodeContext, NodeResult, surviving_findings
from .text import quote_overlap

NAME: Final = "cross-reviewer"

# 两对「看的是同一件事、但结论可能打架」的视角。点出冲突原因，便于人工裁定。
CROSS_PAIRS: Final[tuple[tuple[str, str, str], ...]] = (
    ("fact", "safety", "事实审与安全审对同一片段结论冲突，需确认是否涉及未公开敏感信息"),
    ("compliance", "style", "合规审已就该片段提出要件/披露问题，风格审可能低估红线"),
)

CONFIDENCE_STEP: Final = 0.05
CONFIDENCE_FLOOR: Final = 0.5
OVERLAP_THRESHOLD: Final = 0.6
# 只有「至少一方到 major」才算红线冲突；两条 minor 打架不算
BLOCKING_RANK: Final = 1
_SEV_RANK: Final[Mapping[Severity, int]] = {
    Severity.BLOCKER: 0, Severity.MAJOR: 1, Severity.MINOR: 2, Severity.NIT: 3,
}


@dataclass(frozen=True, slots=True)
class CrossReviewResult:
    disputes: tuple[Dispute, ...] = ()
    revisions: tuple[Finding, ...] = ()

    @property
    def conflicted_ids(self) -> tuple[str, ...]:
        return tuple(sorted({iid for d in self.disputes for iid in d.issue_ids}))


def _point(a: Finding, b: Finding, why: str) -> str:
    def label(f: Finding) -> str:
        rule = f.rule.rule_id if f.rule else ""
        return f"{f.id}({rule}/{f.severity.value})"

    return (f"{label(a)} 与 {label(b)} 指向同一片段："
            f"{a.quote[:40]}… — {why}")


def review_findings(
    findings: Sequence[Finding],
    *,
    run_id: str,
    first_dispute_seq: int = 1,
) -> CrossReviewResult:
    """交叉复核（纯函数）：找出跨维度冲突，产出争议与降权后的发现。"""
    working = {f.id: f.confidence for f in findings}
    disputes: list[Dispute] = []
    seq = int(first_dispute_seq)
    touched: list[str] = []

    for dim_a, dim_b, why in CROSS_PAIRS:
        left = [f for f in findings if f.dimension == dim_a]
        right = [f for f in findings if f.dimension == dim_b]
        for a in left:
            for b in right:
                if quote_overlap(a.quote, b.quote) < OVERLAP_THRESHOLD:
                    continue
                if (_SEV_RANK[a.severity] > BLOCKING_RANK
                        and _SEV_RANK[b.severity] > BLOCKING_RANK):
                    continue                      # 两边都是 minor/nit
                disputes.append(Dispute(
                    id=ids.format_dispute_id(seq), run_id=run_id,
                    point=_point(a, b, why),
                    issue_ids=(a.id, b.id),
                    kind=DisputeKind.SEVERITY_CONFLICT,
                    status=DisputeStatus.OPEN,
                    between=(f"{dim_a}-reviewer", f"{dim_b}-reviewer"),
                    resolution_hint="交人工裁定：确认严重度是否被高估/低估，以及是否漏判",
                ))
                seq += 1
                for f in (a, b):
                    working[f.id] = round(
                        max(CONFIDENCE_FLOOR, working[f.id] - CONFIDENCE_STEP), 2)
                    if f.id not in touched:
                        touched.append(f.id)

    revisions = tuple(
        replace(f, confidence=working[f.id])
        for f in findings
        if f.id in touched and working[f.id] != f.confidence
    )
    return CrossReviewResult(disputes=tuple(disputes), revisions=revisions)


def dispute_to_report_dict(dispute: Dispute) -> dict[str, Any]:
    """回写旧报告 `disputes[]` 的形状。

    键序固定为 between / point / resolution_hint / issue_ids：报告 JSON 与旧版
    逐字节可比，才谈得上「历史报告并排比对」。这也是报告层唯一的转换实现。
    """
    return {
        "between": list(dispute.between),
        "point": dispute.point,
        "resolution_hint": dispute.resolution_hint,
        "issue_ids": list(dispute.issue_ids),
    }


class CrossReviewerNode:
    """cross_review 阶段节点：跨维度冲突 → disputes + 降权修订。"""

    name: Final = NAME
    stage: Final = Stage.CROSS_REVIEW
    produces: Final = frozenset({"disputes"})

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.CROSS_REVIEW:
            return NodeResult.fail(
                f"{NAME} 只处理 cross_review 阶段，当前状态在 {state.stage.value}")
        result = review_findings(
            surviving_findings(state), run_id=state.run_id,
            first_dispute_seq=int(ctx.config.get("first_dispute_seq", 1)),
        )
        warnings: tuple[str, ...] = ()
        if result.disputes:
            warnings = (f"{len(result.disputes)} 处跨维度冲突待人工裁定："
                        f"{'、'.join(result.conflicted_ids[:6])}",)
        return NodeResult.ok(
            disputes=result.disputes,
            revisions=result.revisions,
            warnings=warnings,
        )
