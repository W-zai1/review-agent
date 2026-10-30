# -*- coding: utf-8 -*-
"""语义审候选入库：把模型给的候选变成 Finding（去重 / 语境豁免 / 严重度下调 / 编号）。

为什么单独成模块：
    旧实现把这段写在 `semantic_review` 的循环里，靠 `Session` 提供三样东西
    （已有结论、语境豁免表、make_issue）。这三样都能用普通数据表达，
    抽出来之后就能脱离 Session 单测——模型给什么、最后进报告的是什么，可以直接钉住。

四条规则（与旧实现逐条等价）：
    1. **去重**：同 rule_id 且（引用重合 ≥0.8 或首句指纹相同）视为重复。
       分块审查时相邻块有重叠，模型会对同一句给出长度不同的引用，句级指纹兜住它。
    2. **语境豁免**：规则层已按语境判「不违规」的片段，模型按字面再报时让位——
       确定性判据 > 模型判断。**不静默丢**：进 `excluded` 并写明豁免理由。
    3. **严重度下调**：blocker/major 且置信度 < 0.7 时降一级并记 `severity_note`。
       旧实现把说明写进 evidence 文本；这里放在领域字段里，报告渲染时再拼。
    4. **编号**：由调用方给起始序号，内部自增（Run 级编号资源的用法，见 AGENT_RUNTIME §4）。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..domain import (
    ConfidenceBasis,
    Finding,
    FindingOrigin,
    FindingStatus,
    Location,
    RuleRef,
    Severity,
    ids,
)
from ..text import quote_overlap, sentence_key, sentence_no

DUP_OVERLAP = 0.8
EXEMPT_OVERLAP = 0.8


@dataclass(frozen=True, slots=True)
class Candidate:
    """模型给出的一个候选（字段与 llm_review 的输出对齐）。"""

    rule_id: str
    quote: str
    evidence: str = ""
    confidence: float = 0.6
    suggestion: str = ""
    severity: str | None = None
    open_discovery: bool = False


@dataclass(frozen=True, slots=True)
class IngestContext:
    """入库需要的外部数据（全部由调用方给出，不读全局、不碰 Session）。"""

    run_id: str
    text: str
    dimension: str
    existing: tuple[Finding, ...] = ()
    # (rule_id, quote, reason)：规则层已判「按语境不算问题」的片段
    exemptions: tuple[tuple[str, str, str], ...] = ()
    default_severity: str = "minor"
    tool: str = "llm_api"
    confidence_threshold: float = 0.7


@dataclass(frozen=True, slots=True)
class IngestResult:
    findings: tuple[Finding, ...] = ()
    excluded: tuple[Mapping[str, Any], ...] = ()
    duplicates: int = 0
    exempted: int = 0


def _is_duplicate(candidate: Candidate, known: Sequence[Finding]) -> bool:
    key = sentence_key(candidate.quote)
    for finding in known:
        rule_id = finding.rule.rule_id if finding.rule else ""
        if rule_id != candidate.rule_id:
            continue
        if quote_overlap(finding.quote, candidate.quote) >= DUP_OVERLAP:
            return True
        if key and sentence_key(finding.quote) == key:
            return True
    return False


def _exemption_reason(candidate: Candidate,
                      exemptions: Sequence[tuple[str, str, str]]) -> str:
    for rule_id, quote, reason in exemptions:
        if rule_id == candidate.rule_id and quote_overlap(quote, candidate.quote) >= EXEMPT_OVERLAP:
            return reason
    return ""


def _severity(candidate: Candidate, context: IngestContext) -> tuple[Severity, str | None]:
    raw = candidate.severity or context.default_severity
    try:
        severity = Severity(str(raw))
    except ValueError as e:
        raise ValueError(
            f"候选 {candidate.rule_id} 的严重度 {raw!r} 不在领域枚举内") from e
    if severity in (Severity.BLOCKER, Severity.MAJOR) \
            and candidate.confidence < context.confidence_threshold:
        lowered = Severity.MAJOR if severity is Severity.BLOCKER else Severity.MINOR
        return lowered, (f"置信度 {candidate.confidence:.2f} < "
                         f"{context.confidence_threshold:.2f}，严重度自 "
                         f"{severity.value} 下调至 {lowered.value}")
    return severity, None


def _location(text: str, quote: str) -> Location:
    position = (text or "").find(quote or "")
    if position < 0:
        return Location("未定位")
    number = sentence_no(text, position)
    return Location(f"第 {number} 句", sentence=number)


def ingest_candidates(
    candidates: Sequence[Candidate],
    context: IngestContext,
    *,
    start_seq: int,
) -> IngestResult:
    """把候选变成 Finding；返回采纳的、排除的与计数。"""
    known: list[Finding] = list(context.existing)
    findings: list[Finding] = []
    excluded: list[Mapping[str, Any]] = []
    duplicates = exempted = 0
    seq = int(start_seq)

    for candidate in candidates:
        if _is_duplicate(candidate, known):
            duplicates += 1
            continue
        reason = _exemption_reason(candidate, context.exemptions)
        if reason:
            excluded.append({
                "rule_id": candidate.rule_id,
                "dimension": context.dimension,
                "quote": candidate.quote,
                "evidence": candidate.evidence,
                "reason": f"规则层语境豁免：{reason}",
            })
            exempted += 1
            continue

        severity, note = _severity(candidate, context)
        rule = None
        if candidate.rule_id:
            rule = RuleRef(rule_id=candidate.rule_id,
                           dimension=context.dimension)
        finding = Finding(
            id=ids.format_finding_id(seq), run_id=context.run_id,
            dimension=context.dimension, severity=severity,
            quote=candidate.quote,
            location=_location(context.text, candidate.quote),
            confidence=float(candidate.confidence),
            origin=(FindingOrigin.OPEN_DISCOVERY if candidate.open_discovery
                    else FindingOrigin.MODEL),
            rule=rule, statement=candidate.evidence,
            confidence_basis=ConfidenceBasis.MODEL,
            status=(FindingStatus.NEEDS_CONFIRMATION
                    if candidate.confidence < context.confidence_threshold
                    else FindingStatus.PROPOSED),
            tool=context.tool, suggestion=candidate.suggestion,
            severity_note=note,
        )
        findings.append(finding)
        known.append(finding)
        seq += 1

    return IngestResult(findings=tuple(findings), excluded=tuple(excluded),
                        duplicates=duplicates, exempted=exempted)
