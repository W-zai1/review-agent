# -*- coding: utf-8 -*-
"""证据节点（Evidence Analyzer）。

职责：把「发现里声称的原文引用」变成**可回查的证据**，或者把它判为不可回溯。
不调模型、不读文件——只有一条确定性规则（沿用旧 S4）：

    非空 quote 必须逐字出现在原文里
        命中          → ANCHORED（报告算「可回溯」）
        占位式（（文档首段））→ PLACEHOLDER（缺失型问题，保留）
        都没命中      → 移出报告（结论不可审计）
    quote 为空        → 缺失型问题，保留，但这条没有证据对象

为什么这条规则必须挪进节点：
    旧实现写在 `run_one` 的 S4 里（约 40 行），规则本身只有一次 if，但
    「哪些条目该丢、丢几条、可回溯几条」的统计散在 `task.json` 与 `limitations` 里，
    没法单独测。挪进来之后：同样的 findings + 同样的原文 → 同样的证据集合，
    可以拿历史语料回放。

证据与发现的对应关系：
    `Evidence.finding_ids`（证据指向谁）由本节点填写；`Finding.evidence_ids`
    是反向索引，只有「构造发现时证据已存在」的路径（历史报告导入）才填得进去。
    两者的一致性由 `runtime.advance()` 校验，不允许悬空引用。

配置（`ctx.config`）：
    first_evidence_seq : int   证据编号起点（默认 1）
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from ...domain import (
    Evidence,
    EvidenceKind,
    Finding,
    Stage,
    Verification,
    ids,
)
from ..node import NodeContext, NodeResult

NAME: Final = "evidence-analyzer"
# 写进 Evidence.verified_by 的工具名：审计里要能回答「谁校验的」
ANCHOR_TOOL: Final = "review.s4.evidence_anchor"
DROP_REASON: Final = "quote 无法逐字回查原文"


def _placeholder(quote: str) -> bool:
    return quote.startswith("（") and quote.endswith("）")


def verify_quote(quote: str, text: str) -> Verification:
    """一条引用的校验结论——**四类归一的唯一实现**。

    * 空引用 → `NOT_CHECKED`：缺失型问题（「原文没有这条条款」），保留但无证据对象
    * 逐字命中 → `ANCHORED`：可回溯
    * 占位式（（文档首段））→ `PLACEHOLDER`：缺失型问题，保留
    * 其余 → `UNANCHORED`：不可回溯，必须移出报告

    旧流程用 dict 工作（`review.py` S4），新节点用领域对象；两处都调这一个函数，
    规则不会出现「节点判的」和「报告判的」不一致。
    """
    quote = str(quote or "")
    if not quote:
        return Verification.NOT_CHECKED
    if quote in (text or ""):
        return Verification.ANCHORED
    if _placeholder(quote):
        return Verification.PLACEHOLDER
    return Verification.UNANCHORED


@dataclass(frozen=True, slots=True)
class AnchorResult:
    """一次锚定的结果。"""

    evidence: tuple[Evidence, ...] = ()
    dropped: tuple[Finding, ...] = ()
    missing: tuple[Finding, ...] = ()

    @property
    def dropped_ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.dropped)

    @property
    def anchored_ids(self) -> tuple[str, ...]:
        return tuple(e.finding_ids[0] for e in self.evidence
                     if e.verification is Verification.ANCHORED)

    @property
    def placeholder_ids(self) -> tuple[str, ...]:
        return tuple(e.finding_ids[0] for e in self.evidence
                     if e.verification is Verification.PLACEHOLDER)

    @property
    def traceable_count(self) -> int:
        """逐字命中原文的条数（旧 S4 的「quote 可回溯」口径）。"""
        return len(self.anchored_ids)


def anchor_findings(
    findings: Sequence[Finding],
    text: str,
    *,
    run_id: str,
    first_evidence_seq: int = 1,
) -> AnchorResult:
    """按原文校验每条发现的引用，产出证据集合与被丢弃的发现。

    纯函数：不读文件、不看时间。同样的输入永远得到同样的证据编号，
    这是「中断后重跑该阶段不产生第二份结论」的前提。
    """
    text = text or ""
    evidence: list[Evidence] = []
    dropped: list[Finding] = []
    missing: list[Finding] = []
    seq = int(first_evidence_seq)

    for finding in findings:
        quote = str(finding.quote or "")
        verdict = verify_quote(quote, text)
        if verdict is Verification.NOT_CHECKED:
            # 缺失型问题：本身就是在说「没有原文」，不能要求它逐字可回查，
            # 但也没有证据对象可产出。
            missing.append(finding)
            continue
        if verdict is Verification.UNANCHORED:
            dropped.append(finding)
            continue
        evidence.append(Evidence(
            id=ids.format_evidence_id(seq), run_id=run_id,
            kind=EvidenceKind.QUOTE, quote=quote, location=finding.location,
            verification=verdict,
            verified_by=(ANCHOR_TOOL if verdict is Verification.ANCHORED else None),
            finding_ids=(finding.id,),
        ))
        seq += 1

    return AnchorResult(evidence=tuple(evidence), dropped=tuple(dropped),
                        missing=tuple(missing))


def evidence_to_report(evidence: Sequence[Evidence]) -> list[dict[str, Any]]:
    """回写报告的 `evidence[]` 段（新增字段，不改既有格式）。

    为什么报告里要有它：`issues[].quote` 只说「我引了这句」，不回答「这句核过没有」。
    有了 `verification`，读者能区分「逐字命中原文」「占位式引用（原文确实没有）」
    与「未校验」——这正是「证据可追溯」在产品层面的落地。
    """
    return [
        {
            "evidence_id": ev.id,
            "issue_id": ev.finding_ids[0] if ev.finding_ids else None,
            "kind": ev.kind.value,
            "quote": ev.quote,
            "location": ev.location.label,
            "verification": ev.verification.value,
            "verified_by": ev.verified_by,
        }
        for ev in evidence
    ]


class EvidenceAnalyzerNode:
    """evidence 阶段节点：校验引用、产出证据，丢弃不可回溯的发现。"""

    name: Final = NAME
    stage: Final = Stage.EVIDENCE
    produces: Final = frozenset({"evidence", "evidence_dropped"})

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.EVIDENCE:
            return NodeResult.fail(
                f"{NAME} 只处理 evidence 阶段，当前状态在 {state.stage.value}")
        text = state.artifacts.get("target_text")
        if not isinstance(text, str):
            # 没有原文就无法校验引用；此时「锚定通过」会变成一句空话。
            return NodeResult.fail(
                "evidence 阶段缺少 artifacts['target_text']：没有原文无从校验引用")

        result = anchor_findings(
            state.findings, text, run_id=state.run_id,
            first_evidence_seq=int(ctx.config.get("first_evidence_seq", 1)),
        )
        dropped = tuple({
            "finding_id": f.id,
            "rule_id": f.rule.rule_id if f.rule else "",
            "quote": f.quote[:120],
            "reason": DROP_REASON,
        } for f in result.dropped)

        warnings: tuple[str, ...] = ()
        if result.dropped:
            warnings = (f"丢弃 {len(result.dropped)} 条 quote 不可回溯的发现："
                        f"{'、'.join(result.dropped_ids[:5])}",)
        return NodeResult.ok(
            evidence=result.evidence,
            output={"evidence_dropped": dropped},
            warnings=warnings,
        )
