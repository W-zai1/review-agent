# -*- coding: utf-8 -*-
"""证据评估节点（Evidence Assessor，批次 3 / B2）。

`EvidenceAnalyzerNode` 只回答「引用在不在原文里」（第一根轴）。本节点回答
第二个问题：**这条证据够不够支撑结论**（`EvidenceStrength`）。

设计约束：
    * 不调模型、不读文件、不看时间——强度必须可复算（ADR-003）；
    * `None` 表示「无法判定」，绝不能当成通过；
    * 评估节点必须先于 CrossReview / 闸门，否则下游读到的是未评估证据。

判定表（保守优先）：
    QUOTE + ANCHORED + 字符位置
        → VERIFIED（四项匹配由原文逐字命中与定位共同满足）
    QUOTE + ANCHORED 但无字符位置
        → PARTIAL（只能靠子串定位，主体/指标/时间口径没有结构化核验）
    QUOTE + PLACEHOLDER / 其它
        → UNVERIFIED（占位引用不能当已核验）
    EXTERNAL_SOURCE 有来源
        → PARTIAL（来源身份可核；主体/指标/时间为 None，不得冒充四项全过）
    EXTERNAL_SOURCE 无来源
        → UNVERIFIED
    其它证据类型
        → UNVERIFIED（首次出现不应直接当 VERIFIED）

已带 `contradicted_by` 的证据直接保持 CONTRADICTED：反证不是「更弱」，
而是必须进入冲突处理。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Final

from ...domain import (
    Evidence,
    EvidenceAssessment,
    EvidenceKind,
    EvidenceStrength,
    MatchCheck,
    Stage,
    Verification,
)
from ..node import NodeContext, NodeResult

NAME: Final = "evidence-assessor"
ASSESSOR: Final = "review.evidence.assess"


def _assessment(*checks: MatchCheck) -> EvidenceAssessment:
    return EvidenceAssessment(checks=tuple(checks), assessed_by=ASSESSOR)


def _quote_verified_checks() -> tuple[MatchCheck, ...]:
    """原文逐字命中且可定位时，四项由引用本身承载。"""
    return (
        MatchCheck("source_identity", True, "quote 逐字命中待审原文"),
        MatchCheck("subject", True, "主体由原文引用直接承载"),
        MatchCheck("metric", True, "指标由原文引用直接承载"),
        MatchCheck("period", True, "时间口径由原文引用直接承载"),
    )


def assess_evidence(evidence: Evidence) -> Evidence:
    """评估一条证据；纯函数、幂等。"""
    if evidence.contradicted_by:
        return replace(evidence, strength=EvidenceStrength.CONTRADICTED)

    if evidence.kind is EvidenceKind.QUOTE:
        if evidence.verification is Verification.ANCHORED:
            if evidence.location.char_start is not None:
                return replace(
                    evidence,
                    strength=EvidenceStrength.VERIFIED,
                    assessment=_assessment(*_quote_verified_checks()),
                )
            return replace(
                evidence,
                strength=EvidenceStrength.PARTIAL,
                assessment=_assessment(
                    MatchCheck("source_identity", True, "quote 逐字命中待审原文"),
                    MatchCheck("subject", None, "缺少结构化主体核验"),
                    MatchCheck("metric", None, "缺少结构化指标核验"),
                    MatchCheck("period", None, "缺少结构化时间核验"),
                ),
            )
        return replace(
            evidence,
            strength=EvidenceStrength.UNVERIFIED,
            assessment=_assessment(
                MatchCheck("source_identity", False,
                           f"引用校验为 {evidence.verification.value}"),
                MatchCheck("subject", None),
                MatchCheck("metric", None),
                MatchCheck("period", None),
            ),
        )

    if evidence.kind is EvidenceKind.EXTERNAL_SOURCE:
        source = evidence.source
        if source is None:
            return replace(evidence, strength=EvidenceStrength.UNVERIFIED)
        detail = (f"来源 {source.domain}"
                  + ("（官方域）" if source.official else "")
                  + ("，有正文快照" if source.is_snapshot else "，仅来源元数据"))
        return replace(
            evidence,
            strength=EvidenceStrength.PARTIAL,
            assessment=_assessment(
                MatchCheck("source_identity", True, detail),
                MatchCheck("subject", None, "外部来源未提供结构化主体匹配"),
                MatchCheck("metric", None, "外部来源未提供结构化指标匹配"),
                MatchCheck("period", None, "外部来源未提供结构化时间口径匹配"),
            ),
        )

    return replace(evidence, strength=EvidenceStrength.UNVERIFIED)


class EvidenceAssessorNode:
    """evidence 阶段第二节点：给已有证据写入强度，不新增证据。"""

    name: Final = NAME
    stage: Final = Stage.EVIDENCE
    produces: Final = frozenset({"evidence"})

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.EVIDENCE:
            return NodeResult.fail(
                f"{NAME} 只处理 evidence 阶段，当前状态在 {state.stage.value}")

        revised = tuple(assess_evidence(item) for item in state.evidence)
        changed = tuple(
            new for old, new in zip(state.evidence, revised) if new != old
        )
        if not changed:
            return NodeResult.ok()
        counts = Counter(item.strength.value for item in changed)
        summary = "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
        return NodeResult.ok(
            evidence_revisions=changed,
            warnings=(f"证据强度评估：{summary}",),
        )
