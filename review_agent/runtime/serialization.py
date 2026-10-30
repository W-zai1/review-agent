# -*- coding: utf-8 -*-
"""RunState 序列化：把一次执行的状态存下来，再原样读回来。

为什么需要它：
    检查点现在只有「阶段 + state_hash」——能在**手里有状态**时判断恢复是否一致，
    但进程重启后状态没了，「中断后恢复执行」这条验收就永远只能演示、不能交付。
    把状态落盘并重建，恢复才有落点：读回 RunState → Orchestrator 从它的 stage 继续。

编码约定（都写死在 `state_to_dict` 里，测试逐条钉住）：
    * 枚举存 `.value`（字符串），读回时用枚举构造——脏值立刻报错，不静默降级
    * 时间存 ISO 字符串（带时区），读回 `datetime.fromisoformat`
    * tuple 存 list（JSON 没有 tuple），读回再转 tuple
    * `artifacts` 原样 JSON 化：它必须只放可序列化的东西（文本、计划、统计、报告）。
      Path / 自定义对象进了 artifacts，说明有人把「运行时对象」当「产物」塞了，
      这里直接报错（`TypeError`）比存进去读不回来好。

不做的事：
    * 不做 schema 迁移（v1 刚起步，字段变动直接改版本号）
    * 不把 Session / 旧报告格式卷进来——这里只认领域对象
"""
from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..domain import (
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
    ReviewProfile,
    RiskLevel,
    RuleRef,
    RunStats,
    Severity,
    Stage,
    Verdict,
    Verification,
)
from .node import RunState

SCHEMA_VERSION = 1


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: Any) -> datetime.datetime | None:
    if value in (None, ""):
        return None
    return datetime.datetime.fromisoformat(str(value))


def _location_dict(location: Location) -> dict[str, Any]:
    return {"label": location.label, "sentence": location.sentence,
            "char_start": location.char_start, "char_end": location.char_end}


def _location(data: Mapping[str, Any] | None) -> Location:
    data = data or {}
    return Location(
        label=str(data.get("label") or "未定位"),
        sentence=data.get("sentence"),
        char_start=data.get("char_start"),
        char_end=data.get("char_end"),
    )


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    rule = finding.rule
    return {
        "id": finding.id,
        "run_id": finding.run_id,
        "dimension": finding.dimension,
        "severity": finding.severity.value,
        "quote": finding.quote,
        "location": _location_dict(finding.location),
        "confidence": finding.confidence,
        "origin": finding.origin.value,
        "rule": ({"rule_id": rule.rule_id, "dimension": rule.dimension,
                  "text": rule.text,
                  "declared_severity": (rule.declared_severity.value
                                        if rule.declared_severity else None)}
                 if rule else None),
        "statement": finding.statement,
        "confidence_basis": (finding.confidence_basis.value
                             if finding.confidence_basis else None),
        "status": finding.status.value,
        "tool": finding.tool,
        "suggestion": finding.suggestion,
        "evidence_ids": list(finding.evidence_ids),
        "severity_note": finding.severity_note,
        "created_at": _iso(finding.created_at),
    }


def finding_from_dict(data: Mapping[str, Any]) -> Finding:
    rule_data = data.get("rule")
    rule = RuleRef(
        rule_id=rule_data["rule_id"],
        dimension=rule_data["dimension"],
        text=rule_data.get("text") or "",
        declared_severity=(Severity(rule_data["declared_severity"])
                           if rule_data.get("declared_severity") else None),
    ) if rule_data else None
    basis = data.get("confidence_basis")
    return Finding(
        id=data["id"], run_id=data["run_id"], dimension=data["dimension"],
        severity=Severity(data["severity"]), quote=data.get("quote") or "",
        location=_location(data.get("location")),
        confidence=float(data["confidence"]),
        origin=FindingOrigin(data["origin"]),
        rule=rule, statement=data.get("statement") or "",
        confidence_basis=ConfidenceBasis(basis) if basis else None,
        status=FindingStatus(data.get("status") or "proposed"),
        tool=data.get("tool") or "", suggestion=data.get("suggestion") or "",
        evidence_ids=tuple(data.get("evidence_ids") or ()),
        severity_note=data.get("severity_note"),
        created_at=_dt(data.get("created_at")),
    )


def evidence_to_dict(evidence: Evidence) -> dict[str, Any]:
    return {
        "id": evidence.id,
        "run_id": evidence.run_id,
        "kind": evidence.kind.value,
        "quote": evidence.quote,
        "location": _location_dict(evidence.location),
        "source_ref": evidence.source_ref,
        "verification": evidence.verification.value,
        "verified_by": evidence.verified_by,
        "note": evidence.note,
        "created_at": _iso(evidence.created_at),
        "finding_ids": list(evidence.finding_ids),
    }


def evidence_from_dict(data: Mapping[str, Any]) -> Evidence:
    return Evidence(
        id=data["id"], run_id=data["run_id"],
        kind=EvidenceKind(data.get("kind") or "quote"),
        quote=data.get("quote") or "",
        location=_location(data.get("location")),
        source_ref=data.get("source_ref"),
        verification=Verification(data.get("verification") or "not_checked"),
        verified_by=data.get("verified_by"), note=data.get("note"),
        created_at=_dt(data.get("created_at")),
        finding_ids=tuple(data.get("finding_ids") or ()),
    )


def dispute_to_dict(dispute: Dispute) -> dict[str, Any]:
    return {
        "id": dispute.id,
        "run_id": dispute.run_id,
        "point": dispute.point,
        "issue_ids": list(dispute.issue_ids),
        "kind": dispute.kind.value,
        "status": dispute.status.value,
        "between": list(dispute.between),
        "resolution_hint": dispute.resolution_hint,
        "resolution": dispute.resolution,
        "resolved_by": dispute.resolved_by,
        "resolved_at": _iso(dispute.resolved_at),
        "created_at": _iso(dispute.created_at),
    }


def dispute_from_dict(data: Mapping[str, Any]) -> Dispute:
    return Dispute(
        id=data["id"], run_id=data["run_id"], point=data["point"],
        issue_ids=tuple(data.get("issue_ids") or ()),
        kind=DisputeKind(data.get("kind") or "other"),
        status=DisputeStatus(data.get("status") or "open"),
        between=tuple(data.get("between") or ()),
        resolution_hint=data.get("resolution_hint") or "",
        resolution=data.get("resolution"),
        resolved_by=data.get("resolved_by"),
        resolved_at=_dt(data.get("resolved_at")),
        created_at=_dt(data.get("created_at")),
    )


def decision_to_dict(decision: Decision) -> dict[str, Any]:
    return {
        "id": decision.id,
        "run_id": decision.run_id,
        "verdict": decision.verdict.value,
        "rationale": decision.rationale,
        "risk_level": decision.risk_level.value,
        "reasons": list(decision.reasons),
        "rule_refs": list(decision.rule_refs),
        "finding_ids": list(decision.finding_ids),
        "requires_human_review": decision.requires_human_review,
        "policy_id": decision.policy_id,
        "approved_by": decision.approved_by,
        "approved_at": _iso(decision.approved_at),
        "decided_at": _iso(decision.decided_at),
    }


def decision_from_dict(data: Mapping[str, Any]) -> Decision:
    return Decision(
        id=data["id"], run_id=data["run_id"], verdict=Verdict(data["verdict"]),
        rationale=data.get("rationale") or "",
        risk_level=RiskLevel(data.get("risk_level") or "low"),
        reasons=tuple(data.get("reasons") or ()),
        rule_refs=tuple(data.get("rule_refs") or ()),
        finding_ids=tuple(data.get("finding_ids") or ()),
        requires_human_review=bool(data.get("requires_human_review")),
        policy_id=data.get("policy_id") or "review-decision-v1",
        approved_by=data.get("approved_by"),
        approved_at=_dt(data.get("approved_at")),
        decided_at=_dt(data.get("decided_at")),
    )


def profile_to_dict(profile: ReviewProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "version": profile.version,
        "description": profile.description,
        "target_types": list(profile.target_types),
        "dimensions": list(profile.dimensions),
        "rule_ids": list(profile.rule_ids),
        "confidence_threshold": profile.confidence_threshold,
        "fail_on_blocker": profile.fail_on_blocker,
        "require_human_review": profile.require_human_review,
        "source": profile.source,
        "rationale": profile.rationale,
    }


def profile_from_dict(data: Mapping[str, Any]) -> ReviewProfile:
    return ReviewProfile(
        id=data["id"], name=data["name"], version=data["version"],
        description=data.get("description") or "",
        target_types=tuple(data.get("target_types") or ()),
        dimensions=tuple(data.get("dimensions") or ()),
        rule_ids=tuple(data.get("rule_ids") or ()),
        confidence_threshold=float(data.get("confidence_threshold", 0.7)),
        fail_on_blocker=bool(data.get("fail_on_blocker", True)),
        require_human_review=bool(data.get("require_human_review", False)),
        source=data.get("source") or "",
        rationale=data.get("rationale") or "",
    )


def state_to_dict(state: RunState) -> dict[str, Any]:
    """RunState → 可 JSON 化的 dict。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "review_id": state.review_id,
        "run_id": state.run_id,
        "stage": state.stage.value,
        "profile": profile_to_dict(state.profile),
        "completed_stages": [s.value for s in state.completed_stages],
        "findings": [finding_to_dict(f) for f in state.findings],
        "evidence": [evidence_to_dict(e) for e in state.evidence],
        "disputes": [dispute_to_dict(d) for d in state.disputes],
        "decision": decision_to_dict(state.decision) if state.decision else None,
        "artifacts": _jsonable_artifacts(state.artifacts),
        "warnings": list(state.warnings),
        "attempt": state.attempt,
    }


def _jsonable_artifacts(artifacts: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in artifacts.items():
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            raise TypeError(
                f"artifacts[{key!r}] 不能 JSON 序列化：{e}。"
                "artifacts 只放产物（文本/计划/统计/报告），不放运行时对象") from e
        out[key] = value
    return out


def state_from_dict(data: Mapping[str, Any]) -> RunState:
    """dict → RunState（脏数据在这里暴露，不往下游传）。"""
    version = int(data.get("schema_version") or 0)
    if version != SCHEMA_VERSION:
        raise ValueError(f"不认识的 state schema_version={version}（期望 {SCHEMA_VERSION}）")
    decision = data.get("decision")
    return RunState(
        review_id=data["review_id"],
        run_id=data["run_id"],
        stage=Stage(data["stage"]),
        profile=profile_from_dict(data["profile"]),
        completed_stages=tuple(Stage(s) for s in data.get("completed_stages") or ()),
        findings=tuple(finding_from_dict(f) for f in data.get("findings") or ()),
        evidence=tuple(evidence_from_dict(e) for e in data.get("evidence") or ()),
        disputes=tuple(dispute_from_dict(d) for d in data.get("disputes") or ()),
        decision=decision_from_dict(decision) if decision else None,
        artifacts=dict(data.get("artifacts") or {}),
        warnings=tuple(data.get("warnings") or ()),
        attempt=int(data.get("attempt") or 1),
    )


class FileRunStore:
    """按 run_id 存一份状态 JSON（进程重启后可读回继续跑）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.state.json"

    def save(self, state: RunState) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(state.run_id)
        payload = json.dumps(state_to_dict(state), ensure_ascii=False,
                             sort_keys=True, indent=2)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)                       # 原子替换：不留下半个状态文件
        return path

    def load(self, run_id: str) -> RunState | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        return state_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def exists(self, run_id: str) -> bool:
        return self._path(run_id).exists()
