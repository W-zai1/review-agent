# -*- coding: utf-8 -*-
"""迁移期适配器：把历史报告 JSON 读成领域对象。

存在的意义有两个，缺一不可：
    1. **验收**：新领域模型必须能表达旧报告里已有的全部结论。表达不了，
       说明模型设计漏了东西，而不是「旧数据不重要」。
    2. **对照**：用新的 DecisionPolicy 重算旧报告的 verdict，对不上的就是
       「旧实现没有按自己声明的规则裁定」的证据。

映射的已知边界（都写进 docs/DOMAIN_MODEL.md 的附录）：
    · 旧报告没有 Evidence 对象 → 用 issue.quote 合成一条 QUOTE 证据，
      verification 按 S4 的规则判定（逐字命中 / 占位 / 未命中）
    · 旧报告没有 Dispute.id / 状态 → 按出现顺序编 DSP-0001…
    · 旧报告没有 Decision 对象 → 用 verdict + core_conclusion 合成一条，
      reasons 由 DecisionPolicy 复算得到（不是模型编的）
    · 旧报告顶层还有大量结构化块（law_library / research / action_plan…），
      领域层 v1 不建模，原样保留在 `extra`，避免「迁移一次丢一批数据」

不做的事：
    · 不写回、不改历史报告
    · 不猜缺失字段的语义（缺 agent 就写 unversioned，不冒充默认 Agent）
"""
from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..domain import (                                # noqa: F401 - 边界层引用领域包
    DEFAULT_POLICY,
    AdapterError,
    AuditEvent,
    AuditKind,
    ConfidenceBasis,
    Coverage,
    Decision,
    Dispute,
    DisputeKind,
    DisputeStatus,
    Evidence,
    EvidenceKind,
    Finding,
    FindingOrigin,
    FindingStatus,
    IntegrityHashes,
    Location,
    Review,
    ReviewOrigin,
    ReviewProfile,
    ReviewRun,
    ReviewStatus,
    ReviewTarget,
    RuleRef,
    RunMode,
    RunStats,
    RunStatus,
    Severity,
    Stage,
    Verdict,
    Verification,
    ids,
)
from ..profiles import resolve_profile

# 被真正消费（进入领域对象）的顶层键；其余全部原样进 extra。
CONSUMED_KEYS = frozenset({
    "review_id", "target_path", "target_type", "target_type_source",
    "review_mode", "integrity", "coverage", "stats", "issues", "suppressed",
    "disputes", "timestamp", "agent", "scenario", "format_spec",
    "verdict", "core_conclusion",
})

_SENTENCE_RE = re.compile(r"第\s*(\d+)\s*句")
_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})")


@dataclass(frozen=True, slots=True)
class AdaptedReview:
    """一份历史报告的领域视图（只读）。"""

    review: Review
    run: ReviewRun
    findings: tuple[Finding, ...]
    evidence: tuple[Evidence, ...]
    disputes: tuple[Dispute, ...]
    decisions: tuple[Decision, ...]
    audit: tuple[AuditEvent, ...]
    extra: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def verdict(self) -> Verdict | None:
        return self.decisions[0].verdict if self.decisions else None

    @property
    def stats(self) -> RunStats:
        return self.run.stats


def _ts(value: Any, where: str, warnings: list[str]) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    try:
        return datetime.datetime.fromisoformat(raw)
    except ValueError:
        m = _TIMESTAMP_RE.match(raw)
        if m:
            try:
                return datetime.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
            except ValueError:
                pass
        warnings.append(f"{where}: 时间戳无法解析：{raw[:40]!r}，按 None 处理")
        return None


def _profile(report: Mapping[str, Any], warnings: list[str]) -> ReviewProfile:
    """把旧报告的三块配置（agent / scenario / format_spec）合成 Profile 快照。

    合成规则只有一份实现：`review_agent.profiles.resolve_profile`。
    这里只负责取块、补 warning。
    """
    agent = report.get("agent") if isinstance(report.get("agent"), Mapping) else {}
    scenario = report.get("scenario") if isinstance(report.get("scenario"), Mapping) else None
    fmt = report.get("format_spec") if isinstance(report.get("format_spec"), Mapping) else None
    coverage = report.get("coverage") if isinstance(report.get("coverage"), Mapping) else {}

    if not agent:
        warnings.append(
            "报告没有 agent 块（旧版本），Profile 记为 legacy-unversioned："
            "这份报告的判定标准无法从报告自身复现")

    return resolve_profile(
        agent,
        target_type=str(report.get("target_type") or "unknown"),
        scenario=scenario,
        format_spec=fmt,
        active_rules=[
            r for r in (report.get("active_rules") or [])
            if isinstance(r, Mapping)
        ],
        fallback_dimensions=[
            str(d) for d in (coverage.get("dimensions_checked") or [])
        ],
        source="legacy:report",
    )


def _target(report: Mapping[str, Any], warnings: list[str]) -> ReviewTarget:
    integrity = report.get("integrity") if isinstance(report.get("integrity"), Mapping) else {}
    coverage = report.get("coverage") if isinstance(report.get("coverage"), Mapping) else {}
    detection = coverage.get("type_detection") if isinstance(
        coverage.get("type_detection"), Mapping) else {}
    source = str(report.get("target_type_source") or detection.get("source") or "unknown")
    if source not in ("auto", "manual"):
        source = "unknown"
    confidence = detection.get("confidence")
    try:
        conf = float(confidence) if confidence is not None else 1.0
    except (TypeError, ValueError):
        conf = 1.0
        warnings.append(f"类型判定置信度无法解析：{confidence!r}，按 1.0 处理")
    return ReviewTarget(
        path=str(report.get("target_path") or "（未记录）"),
        target_type=str(report.get("target_type") or "unknown"),
        sha256=integrity.get("target_hash"),
        type_confidence=conf,
        type_source=source,
        encoding=coverage.get("source_encoding"),
    )


def _integrity(report: Mapping[str, Any]) -> IntegrityHashes:
    inc = report.get("integrity")
    if not isinstance(inc, Mapping):
        return IntegrityHashes()
    fields = IntegrityHashes.__dataclass_fields__          # noqa: SLF001 - 值对象字段即契约
    return IntegrityHashes(**{k: inc.get(k) for k in fields if k in inc})


def _coverage(report: Mapping[str, Any]) -> Coverage | None:
    cov = report.get("coverage")
    if not isinstance(cov, Mapping):
        return None
    chars_total = int(cov.get("chars_total") or 0)
    chars_reviewed = int(cov.get("chars_reviewed") or 0)
    if chars_total == 0 and chars_reviewed > 0:      # 旧报告口径不一致时以已审为准
        chars_total = chars_reviewed
    return Coverage(
        chars_total=chars_total,
        chars_reviewed=chars_reviewed,
        complete=bool(cov.get("complete", True)),
        chunks=int(cov.get("chunks") or 1),
        chunked=bool(cov.get("chunked")),
        active_rules=int(cov.get("active_rules") or 0),
        dimensions=tuple(str(d) for d in (cov.get("dimensions_checked") or [])),
        failed_chunks=tuple(
            str((c or {}).get("label") or c) if isinstance(c, Mapping) else str(c)
            for c in (cov.get("failed_chunks") or [])),
        uncovered=tuple(str(u) for u in (cov.get("uncovered") or [])),
    )


def _location(raw: Any) -> Location:
    label = str(raw or "").strip() or "未定位"
    m = _SENTENCE_RE.search(label)
    return Location(label=label, sentence=int(m.group(1)) if m else None)


def _severity(raw: Any, finding_id: str, warnings: list[str]) -> Severity:
    try:
        return Severity(str(raw))
    except ValueError:
        warnings.append(f"{finding_id}: 未知严重度 {raw!r}，按 minor 处理")
        return Severity.MINOR


def _basis(raw: Any) -> ConfidenceBasis | None:
    if raw in (None, ""):
        return None
    try:
        return ConfidenceBasis(str(raw))
    except ValueError:
        return None


def _origin(issue: Mapping[str, Any]) -> FindingOrigin:
    if issue.get("open_discovery"):
        return FindingOrigin.OPEN_DISCOVERY
    basis = _basis(issue.get("confidence_basis"))
    return {
        ConfidenceBasis.MODEL: FindingOrigin.MODEL,
        ConfidenceBasis.HEURISTIC: FindingOrigin.HEURISTIC,
        ConfidenceBasis.RESEARCH: FindingOrigin.RESEARCH,
        ConfidenceBasis.RULE: FindingOrigin.RULE,
        None: FindingOrigin.RULE,
    }[basis]


def _quote_verification(quote: str, text: str) -> tuple[Verification, str | None]:
    if not quote:
        return Verification.NOT_CHECKED, None
    if quote.startswith("（") and quote.endswith("）"):
        return Verification.PLACEHOLDER, None
    if text and quote in text:
        return Verification.ANCHORED, "review.s4.evidence_anchor"
    if not text:
        return Verification.NOT_CHECKED, None
    return Verification.UNANCHORED, None


def _parse_issue(raw: Mapping[str, Any], run_id: str, text: str, created_at,
                 *, suppressed: bool, used_ids: set[str],
                 next_evidence: list[int], warnings: list[str],
                 ) -> tuple[Finding, Evidence | None]:
    raw_id = str(raw.get("issue_id") or "")
    try:
        ids.parse_finding_id(raw_id)
    except Exception:                                   # noqa: BLE001
        raw_id = ids.format_finding_id(len(used_ids) + 1)
        warnings.append(f"issue_id 不合法，已重编为 {raw_id}")
    while raw_id in used_ids:
        raw_id = ids.format_finding_id(len(used_ids) + 1)
        warnings.append(f"issue_id 重复，已重编为 {raw_id}")
    used_ids.add(raw_id)

    dimension = str(raw.get("dimension") or "unknown").strip().lower()
    severity = _severity(raw.get("severity"), raw_id, warnings)
    confidence = float(raw.get("confidence") or 0.0)
    quote = str(raw.get("quote") or "")
    rule_id = str(raw.get("rule_id") or "").strip()
    rule = None
    if rule_id:
        try:
            rule = RuleRef(rule_id=rule_id, dimension=dimension,
                           text=str(raw.get("rule_text") or ""),
                           declared_severity=severity)
        except Exception as e:                          # noqa: BLE001
            warnings.append(f"{raw_id}: rule_id {rule_id!r} 无法构成 RuleRef：{e}")

    note = None
    if severity in (Severity.BLOCKER, Severity.MAJOR) and confidence < 0.7:
        note = (f"历史报告未执行严重度下调：置信度 {confidence:.2f} < 0.70，"
                f"严重度仍为 {severity.value}")
        warnings.append(f"{raw_id}: {note}")

    evidence = None
    evidence_ids: tuple[str, ...] = ()
    if quote:
        verification, verified_by = _quote_verification(quote, text)
        ev_id = ids.format_evidence_id(next_evidence[0])
        next_evidence[0] += 1
        evidence = Evidence(
            id=ev_id, run_id=run_id, kind=EvidenceKind.QUOTE, quote=quote,
            location=_location(raw.get("location")),
            verification=verification, verified_by=verified_by,
            created_at=created_at, finding_ids=(raw_id,),
        )
        evidence_ids = (ev_id,)

    status = FindingStatus.SUPPRESSED if suppressed else (
        FindingStatus.NEEDS_CONFIRMATION if confidence < 0.7 else FindingStatus.PROPOSED)

    finding = Finding(
        id=raw_id, run_id=run_id, dimension=dimension, severity=severity,
        quote=quote, location=_location(raw.get("location")),
        confidence=confidence, origin=_origin(raw), rule=rule,
        statement=str(raw.get("evidence") or ""),
        confidence_basis=_basis(raw.get("confidence_basis")),
        status=status, tool=str(raw.get("tool") or ""),
        suggestion=str(raw.get("suggestion") or ""),
        evidence_ids=evidence_ids, severity_note=note, created_at=created_at,
    )
    return finding, evidence


def _disputes(report: Mapping[str, Any], run_id: str, created_at) -> tuple[Dispute, ...]:
    out = []
    for idx, raw in enumerate(report.get("disputes") or [], start=1):
        if not isinstance(raw, Mapping):
            continue
        issue_ids = tuple(str(x) for x in (raw.get("issue_ids") or []) if x)
        if not issue_ids:
            continue
        out.append(Dispute(
            id=ids.format_dispute_id(idx), run_id=run_id,
            point=str(raw.get("point") or "（未记录争议点）"),
            issue_ids=issue_ids, kind=DisputeKind.SEVERITY_CONFLICT,
            status=DisputeStatus.OPEN,
            between=tuple(str(x) for x in (raw.get("between") or [])),
            resolution_hint=str(raw.get("resolution_hint") or ""),
            created_at=created_at,
        ))
    return tuple(out)


def _audit(report: Mapping[str, Any], run_id: str,
           steps: Sequence[Mapping[str, Any]] | None,
           warnings: list[str]) -> tuple[AuditEvent, ...]:
    events: list[AuditEvent] = []
    seq = 0
    for raw in steps or ():
        if not isinstance(raw, Mapping):
            continue
        events.append(AuditEvent(
            seq=seq, run_id=run_id, kind=AuditKind.STEP,
            name=f"{raw.get('step', '')} {raw.get('name', '')}".strip() or "step",
            status=str(raw.get("status") or "success"),
            ts=_ts(raw.get("ts"), "steps.ts", warnings),
            latency_ms=int(raw.get("latency_ms") or 0),
            detail={k: v for k, v in raw.items()
                    if k not in ("step", "name", "status", "ts", "latency_ms")},
        ))
        seq += 1
    for raw in report.get("decisions") or ():
        if not isinstance(raw, Mapping):
            continue
        detail = {k: v for k, v in raw.items() if k not in ("ts", "decision", "rule_id")}
        events.append(AuditEvent(
            seq=seq, run_id=run_id, kind=AuditKind.DECISION,
            name=str(raw.get("decision") or "decision"),
            status="info",
            ts=_ts(raw.get("ts"), "decisions.ts", warnings),
            detail=detail,
            ref=str(raw.get("rule_id") or "") or None,
        ))
        seq += 1
    return tuple(events)


def adapt_report(report: Mapping[str, Any], *,
                 steps: Sequence[Mapping[str, Any]] | None = None,
                 ) -> AdaptedReview:
    """把一份历史报告映射为领域对象。

    steps 可传 logs/<review_id>/task.json 的 steps（报告 JSON 本身不含 steps）。
    """
    if not isinstance(report, Mapping):
        raise AdapterError(f"报告必须是 Mapping，实际 {type(report).__name__}")
    warnings: list[str] = []
    review_id = str(report.get("review_id") or "")
    try:
        ids.parse_review_id(review_id)
    except Exception as e:                              # noqa: BLE001
        raise AdapterError("报告缺少合法 review_id", where="report", cause=e) from e

    created_at = _ts(report.get("timestamp"), "timestamp", warnings)
    if created_at is None:
        created_at = datetime.datetime.now().astimezone()
        warnings.append("报告缺少 timestamp，使用读取时间（会破坏可复现性）")

    profile = _profile(report, warnings)
    target = _target(report, warnings)
    run_id = ids.format_run_id(review_id, 1)
    text = str(report.get("target_text") or "")

    used: set[str] = set()
    next_evidence = [1]
    findings: list[Finding] = []
    evidence: list[Evidence] = []
    for raw in report.get("issues") or ():
        if not isinstance(raw, Mapping):
            continue
        finding, ev = _parse_issue(raw, run_id, text, created_at, suppressed=False,
                                   used_ids=used, next_evidence=next_evidence,
                                   warnings=warnings)
        findings.append(finding)
        if ev:
            evidence.append(ev)
    for raw in report.get("suppressed") or ():
        if not isinstance(raw, Mapping):
            continue
        finding, ev = _parse_issue(raw, run_id, text, created_at, suppressed=True,
                                   used_ids=used, next_evidence=next_evidence,
                                   warnings=warnings)
        findings.append(finding)
        if ev:
            evidence.append(ev)

    stats = RunStats.from_mapping(report.get("stats")) if isinstance(
        report.get("stats"), Mapping) else RunStats.from_findings(findings)
    derived = RunStats.from_findings(findings, exclude_suppressed=True)
    if stats != derived:
        warnings.append(
            f"报告 stats 与 findings 复算不一致：报告 {stats}，复算 {derived}")

    coverage = _coverage(report)
    research = report.get("research") if isinstance(report.get("research"), Mapping) else {}
    external_unverified = bool(research.get("unverified") or research.get("needs_confirm"))
    try:
        verdict = Verdict(str(report.get("verdict") or "warn"))
    except ValueError:
        verdict = Verdict.WARN
        warnings.append(f"未知 verdict {report.get('verdict')!r}，按 warn 处理")

    disputes = _disputes(report, run_id, created_at)
    draft = DEFAULT_POLICY.decide(findings, disputes=disputes,
                                  external_unverified=external_unverified,
                                  coverage=coverage)
    if draft.verdict is not verdict:
        warnings.append(
            f"报告 verdict={verdict.value}，策略复算={draft.verdict.value}"
            f"（{'；'.join(draft.reasons)}）")

    decision = Decision(
        id=ids.format_decision_id(1), run_id=run_id, verdict=verdict,
        rationale=str(report.get("core_conclusion") or ""),
        risk_level=draft.risk_level, reasons=draft.reasons,
        rule_refs=draft.rule_refs, finding_ids=draft.finding_ids,
        requires_human_review=draft.requires_human_review,
        decided_at=created_at,
    )

    try:
        mode = RunMode(str(report.get("review_mode") or "rule-only"))
    except ValueError:
        mode = RunMode.RULE_ONLY
        warnings.append(f"未知 review_mode {report.get('review_mode')!r}，按 rule-only 处理")

    integrity = _integrity(report)
    run = ReviewRun(
        id=run_id, review_id=review_id, seq=1,
        status=RunStatus.COMPLETED, stage=Stage.FINALIZE, mode=mode,
        profile=profile, started_at=created_at, finished_at=created_at,
        integrity=integrity, coverage=coverage, stats=stats,
        decision_id=decision.id,
    )
    review = Review(
        id=review_id, target=target, profile_id=profile.id,
        profile_version=profile.version, created_at=created_at,
        status=ReviewStatus.COMPLETED, origin=ReviewOrigin.IMPORT,
        declared_purpose=(report.get("purpose") if isinstance(
            report.get("purpose"), str) else None) or None,
        runs=(run,),
    )

    extra = {k: v for k, v in report.items() if k not in CONSUMED_KEYS}
    audit = _audit(report, run_id, steps, warnings)
    return AdaptedReview(
        review=review, run=run, findings=tuple(findings),
        evidence=tuple(evidence), disputes=disputes,
        decisions=(decision,), audit=audit,
        extra=MappingProxyType(extra), warnings=tuple(warnings),
    )
