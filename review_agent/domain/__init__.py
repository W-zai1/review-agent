# -*- coding: utf-8 -*-
"""领域层公共出口。

只导出会长期存在的名字：枚举、六个核心对象、策略、状态机、端口。
适配器（adapters/）不在这里导出——它依赖历史报告格式，属于迁移期代码。
"""
from __future__ import annotations

from .enums import (
    AuditKind,
    ConfidenceBasis,
    DisputeKind,
    DisputeStatus,
    EvidenceKind,
    FindingOrigin,
    FindingStatus,
    ReviewOrigin,
    ReviewStatus,
    RiskLevel,
    RunMode,
    RunStatus,
    Severity,
    Stage,
    Verdict,
    Verification,
)
from .errors import (
    AdapterError,
    ConfigError,
    DomainError,
    IdFormatError,
    InputError,
    InvalidTransition,
    InvariantViolation,
    NotFoundError,
)
from .models import (
    AuditEvent,
    Checkpoint,
    Coverage,
    Decision,
    Dispute,
    Evidence,
    Finding,
    IntegrityHashes,
    Location,
    Review,
    ReviewProfile,
    ReviewRun,
    ReviewTarget,
    RuleRef,
    RunStats,
)
from .policy import (
    DEFAULT_POLICY,
    POLICY_ID,
    DecisionDraft,
    DecisionPolicy,
    consistency_issues,
    finding_violations,
)
from .ports import ModelClient, ModelReply
from .transitions import (
    STAGE_ARTIFACTS,
    STAGE_ORDER,
    ensure_dispute_transition,
    ensure_finding_transition,
    ensure_review_transition,
    ensure_run_transition,
    ensure_stage_advance,
    next_stage,
)

__all__ = [
    # enums
    "AuditKind", "ConfidenceBasis", "DisputeKind", "DisputeStatus", "EvidenceKind",
    "FindingOrigin", "FindingStatus", "ReviewOrigin", "ReviewStatus", "RiskLevel",
    "RunMode", "RunStatus", "Severity", "Stage", "Verdict", "Verification",
    # errors
    "AdapterError", "ConfigError", "DomainError", "IdFormatError", "InputError",
    "InvalidTransition", "InvariantViolation", "NotFoundError",
    # models
    "AuditEvent", "Checkpoint", "Coverage", "Decision", "Dispute", "Evidence",
    "Finding", "IntegrityHashes", "Location", "Review", "ReviewProfile",
    "ReviewRun", "ReviewTarget", "RuleRef", "RunStats",
    # policy
    "DEFAULT_POLICY", "POLICY_ID", "DecisionDraft", "DecisionPolicy",
    "consistency_issues", "finding_violations",
    # ports
    "ModelClient", "ModelReply",
    # transitions
    "STAGE_ARTIFACTS", "STAGE_ORDER", "ensure_dispute_transition",
    "ensure_finding_transition", "ensure_review_transition",
    "ensure_run_transition", "ensure_stage_advance", "next_stage",
]
