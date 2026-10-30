# -*- coding: utf-8 -*-
"""核心领域模型：六个业务对象 + 值对象。

    六个对象（对应 docs/DOMAIN_MODEL.md）
        Review        审查任务：审什么、按哪套标准、谁发起
        ReviewRun     一次执行：状态、阶段、覆盖、完整性
        Finding       发现：一条可举证的异议
        Evidence      证据：quote / 来源 / 校验结果
        Dispute       争议：两个结论指向同一处但互相矛盾
        Decision      裁定：最终 verdict + 依据 + 是否需人工复核

    为什么这些对象住在 domain 而不是 review.py
        review.py 现在用 `Session` 装所有状态、用 dict 装所有结论，
        任何一段代码都能改任何字段（s.issues / s.conflicts / s.decisions 全局可写）。
        领域对象是 frozen 的：状态变化只能通过新对象或状态机产生，
        「谁改了什么」才有答案。

    不变量分两层（这是刻意的）
        · 硬不变量（这里 __post_init__ 直接抛）：结构自洽 —— 置信度在 0..1、
          ID 合法、终态必须有结束时间。违反 = 数据坏了，不能降级运行。
        · 业务规则（policy.py 里校验并返回违规清单）：例如「blocker/major 的
          置信度不应低于 0.7」。历史报告里有 19 条不符（早期版本没做严重度下调），
          领域层若直接抛异常，历史数据就再也读不进来 —— 业务规则要能复盘，
          不能当解析器用。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from . import ids
from .enums import (
    AuditKind,
    ConfidenceBasis,
    DisputeKind,
    DisputeStatus,
    EvidenceKind,
    FindingOrigin,
    FindingStatus,
    RiskLevel,
    ReviewOrigin,
    ReviewStatus,
    RunMode,
    RunStatus,
    Severity,
    Stage,
    Verdict,
    Verification,
)
from .errors import InvariantViolation

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_DIM_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
_RULE_RE = re.compile(r"^[A-Z][A-Z0-9_-]{2,40}$")
_STATUS_RE = re.compile(r"^[a-z][a-z0-9_]{1,15}$")


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise InvariantViolation(message)


def _text(value: Any, field_name: str = "", *, allow_empty: bool = True) -> str:
    out = str(value).strip() if value is not None else ""
    if not allow_empty:
        _require(bool(out), f"{field_name or '字段'} 不能为空")
    return out


def _hash(value: Any, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    out = str(value).strip().lower()
    _require(bool(_HASH_RE.match(out)), f"{field_name} 必须是 64 位十六进制 sha256，实际 {value!r}")
    return out


def _frozen(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping or {}))


def _enum(enum_cls, value, field_name: str):
    """把外部字符串转成枚举；失败时给出字段名和合法取值。"""
    try:
        return enum_cls(value)
    except ValueError as e:
        allowed = "、".join(x.value for x in enum_cls)
        raise InvariantViolation(
            f"{field_name} 取值不合法：{value!r}（合法值：{allowed}）") from e


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Location:
    """原文位置。

    旧实现把位置塞进一个字符串（"第 5 句"、"文末（最后一条之后，应在此处补）"）。
    这里保留 label（向后兼容、给人看），同时允许结构化字段（句号 / 字符区间），
    证据定位要能算、能比对，不能只靠中文描述。
    """

    label: str
    sentence: int | None = None
    char_start: int | None = None
    char_end: int | None = None

    def __post_init__(self) -> None:
        _require(bool(_text(self.label)), "location.label 不能为空")
        if self.sentence is not None:
            _require(self.sentence >= 1, "location.sentence 从 1 开始")
        for name, value in (("char_start", self.char_start), ("char_end", self.char_end)):
            if value is not None:
                _require(value >= 0, f"location.{name} 不能为负")
        if self.char_start is not None and self.char_end is not None:
            _require(self.char_start <= self.char_end,
                     f"location 区间反了：{self.char_start}..{self.char_end}")


@dataclass(frozen=True, slots=True)
class RuleRef:
    """规则引用：Finding 必须能回答「违的是哪一条」。"""

    rule_id: str
    dimension: str
    text: str = ""
    declared_severity: Severity | None = None

    def __post_init__(self) -> None:
        _require(bool(_RULE_RE.match(self.rule_id)),
                 f"rule_id 格式不合法：{self.rule_id!r}")
        _require(bool(_DIM_RE.match(self.dimension)),
                 f"dimension 格式不合法：{self.dimension!r}")


@dataclass(frozen=True, slots=True)
class RunStats:
    """按严重度计数。"""

    blocker: int = 0
    major: int = 0
    minor: int = 0
    nit: int = 0

    def __post_init__(self) -> None:
        for name in ("blocker", "major", "minor", "nit"):
            value = getattr(self, name)
            _require(isinstance(value, int) and value >= 0,
                     f"stats.{name} 必须是非负整数，实际 {value!r}")

    @property
    def total(self) -> int:
        return self.blocker + self.major + self.minor + self.nit

    @property
    def by_severity(self) -> Mapping[Severity, int]:
        return MappingProxyType({
            Severity.BLOCKER: self.blocker,
            Severity.MAJOR: self.major,
            Severity.MINOR: self.minor,
            Severity.NIT: self.nit,
        })

    def count(self, severity: Severity) -> int:
        return self.by_severity[Severity(severity)]

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "RunStats":
        data = data or {}
        return cls(
            blocker=int(data.get("blocker") or 0),
            major=int(data.get("major") or 0),
            minor=int(data.get("minor") or 0),
            nit=int(data.get("nit") or 0),
        )

    @classmethod
    def from_findings(cls, findings: Iterable["Finding"],
                      *, exclude_suppressed: bool = False) -> "RunStats":
        counts = {s: 0 for s in Severity}
        for f in findings:
            if exclude_suppressed and f.status is FindingStatus.SUPPRESSED:
                continue
            counts[f.severity] += 1
        return cls(blocker=counts[Severity.BLOCKER], major=counts[Severity.MAJOR],
                   minor=counts[Severity.MINOR], nit=counts[Severity.NIT])


@dataclass(frozen=True, slots=True)
class Coverage:
    """审查覆盖率：审了多少字、哪些维度跑了、哪些没跑。

    只报问题数不报覆盖率，用户会默认「全文都审过了」。这个对象存在的目的
    就是让「没审」和「没问题」在数据上分开。
    """

    chars_total: int = 0
    chars_reviewed: int = 0
    complete: bool = True
    chunks: int = 1
    chunked: bool = False
    active_rules: int = 0
    dimensions: tuple[str, ...] = ()
    failed_chunks: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(self.chars_total >= 0, "coverage.chars_total 不能为负")
        _require(self.chars_reviewed >= 0, "coverage.chars_reviewed 不能为负")
        _require(self.chars_reviewed <= self.chars_total or self.chars_total == 0,
                 f"覆盖字数超过总字数：{self.chars_reviewed}/{self.chars_total}")
        _require(self.chunks >= 1, "coverage.chunks 至少为 1")

    @property
    def text_coverage_pct(self) -> int:
        if self.chars_total <= 0:
            return 0
        return round(self.chars_reviewed / self.chars_total * 100)


@dataclass(frozen=True, slots=True)
class IntegrityHashes:
    """完整性指纹：报告 + 规则集 + 知识库 + Agent 判定标准的哈希链。

    字段与历史报告 integrity 块一一对应，允许 None（早期报告没有 agent_dir_hash）。
    """

    target_hash: str | None = None
    ruleset_hash: str | None = None
    rules_dir_hash: str | None = None
    rules_version: str | None = None
    agent_dir_hash: str | None = None
    agent_version: str | None = None
    knowledge_hash: str | None = None
    knowledge_version: str | None = None
    report_hash: str | None = None
    logs_hash: str | None = None
    prev_report_hash: str | None = None

    def __post_init__(self) -> None:
        for name in ("target_hash", "ruleset_hash", "rules_dir_hash", "agent_dir_hash",
                     "knowledge_hash", "report_hash", "logs_hash", "prev_report_hash"):
            object.__setattr__(self, name, _hash(getattr(self, name), f"integrity.{name}"))

    @property
    def verified(self) -> bool:
        """报告自身的哈希是否已算出（不代表 ledger 链通过）。"""
        return self.report_hash is not None


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    """待审对象：路径 + 哈希 + 类型判定。"""

    path: str
    target_type: str
    sha256: str | None = None
    type_confidence: float = 1.0
    type_source: str = "auto"
    encoding: str | None = None

    def __post_init__(self) -> None:
        _require(bool(_text(self.path)), "target.path 不能为空")
        _require(bool(_text(self.target_type)), "target.target_type 不能为空")
        _require(0.0 <= float(self.type_confidence) <= 1.0,
                 f"type_confidence 必须在 0..1，实际 {self.type_confidence}")
        _require(self.type_source in ("auto", "manual", "unknown"),
                 f"type_source 只能是 auto/manual/unknown，实际 {self.type_source!r}")
        object.__setattr__(self, "sha256", _hash(self.sha256, "target.sha256"))


@dataclass(frozen=True, slots=True)
class ReviewProfile:
    """审查 Profile：同一套引擎的不同配置（文档 / 合规 / 安全 / 代码）。

    与旧结构的关系：
        旧实现把「审什么」拆成四处 —— agents/<id>/manifest.json（维度/规则来源）、
        config/scenarios.yaml（关哪些规则）、config/format_specs.yaml（版式标准）、
        config/charter.json（任务书）。四者叠加才是一次审查的完整口径。
        ReviewProfile 是这四者的**解析结果**，随 Run 落盘，保证「半年后能复现当时按什么标准判」。
    """

    id: str
    name: str
    version: str
    description: str = ""
    target_types: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    confidence_threshold: float = 0.7
    fail_on_blocker: bool = True
    require_human_review: bool = False
    source: str = ""
    rationale: str = ""

    def __post_init__(self) -> None:
        for name in ("id", "name", "version"):
            _require(bool(_text(getattr(self, name), f"profile.{name}")),
                     f"profile.{name} 不能为空")
        _require(0.0 <= float(self.confidence_threshold) <= 1.0,
                 f"confidence_threshold 必须在 0..1，实际 {self.confidence_threshold}")
        for dim in self.dimensions:
            _require(bool(_DIM_RE.match(dim)), f"profile.dimensions 含非法维度：{dim!r}")
        for rid in self.rule_ids:
            _require(bool(_RULE_RE.match(rid)), f"profile.rule_ids 含非法规则号：{rid!r}")

    def fingerprint(self) -> str:
        """口径指纹：同 id 不同 fingerprint = 判定标准被改过（用于报告校验）。"""
        payload = {
            "id": self.id, "version": self.version,
            "target_types": list(self.target_types),
            "dimensions": list(self.dimensions),
            "rule_ids": sorted(self.rule_ids),
            "confidence_threshold": self.confidence_threshold,
            "fail_on_blocker": self.fail_on_blocker,
            "require_human_review": self.require_human_review,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 证据 / 发现
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Evidence:
    """证据：支撑一条 Finding 的最小可校验单元。

    旧报告只有 issue.quote + issue.evidence 两个字符串，无法表达
    「引的是原文 / 还是外部来源 / 校验过没有」。Evidence 把这三件事拆开。
    """

    id: str
    run_id: str
    kind: EvidenceKind = EvidenceKind.QUOTE
    quote: str = ""
    location: Location = field(default_factory=lambda: Location("未定位"))
    source_ref: str | None = None
    verification: Verification = Verification.NOT_CHECKED
    verified_by: str | None = None
    note: str | None = None
    created_at: datetime.datetime | None = None
    # 这条证据支撑哪些发现。为什么证据自己也要记：
    # 证据是**分析之后**才产出的（S4 锚定），而 Finding 不可变、构造时就得写
    # evidence_ids——晚到的证据没法再回填。同一条原文片段也常同时支撑多条发现
    # （一句话命中三条规则），一对多的关系天然属于证据这一侧。
    # Finding.evidence_ids 保留为「我引用了哪些证据」的反向索引，两者由
    # runtime.advance() 校验不出现悬空指向。
    finding_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        ids.parse_evidence_id(self.id)
        _require(bool(_text(self.run_id)), "evidence.run_id 不能为空")
        if self.kind is EvidenceKind.QUOTE:
            _require(bool(_text(self.quote)), "quote 类证据必须有原文片段")
        if self.kind is EvidenceKind.EXTERNAL_SOURCE:
            _require(bool(_text(self.source_ref)), "外部来源证据必须有 source_ref")
        if self.verification is Verification.ANCHORED:
            _require(bool(_text(self.verified_by)),
                     "已锚定的证据必须记录 verified_by（谁校验的）")
        for fid in self.finding_ids:
            ids.parse_finding_id(fid)


@dataclass(frozen=True, slots=True)
class Finding:
    """一条发现（历史报告里的 issue）。

    不可变：修改结论 = 新建 Finding 并留下审计事件，而不是就地改字段。
    旧实现 `s.add_issue` / 交叉复核里直接 `i["confidence"] -= 0.05`，
    改完只剩最终值，无法回答「为什么从 0.75 变成 0.70」。
    """

    id: str
    run_id: str
    dimension: str
    severity: Severity
    quote: str
    location: Location
    confidence: float
    origin: FindingOrigin
    rule: RuleRef | None = None
    statement: str = ""
    confidence_basis: ConfidenceBasis | None = None
    status: FindingStatus = FindingStatus.PROPOSED
    tool: str = ""
    suggestion: str = ""
    evidence_ids: tuple[str, ...] = ()
    severity_note: str | None = None
    created_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        ids.parse_finding_id(self.id)
        _require(bool(_text(self.run_id)), "finding.run_id 不能为空")
        _require(bool(_DIM_RE.match(self.dimension)),
                 f"finding.dimension 非法：{self.dimension!r}")
        _require(0.0 <= float(self.confidence) <= 1.0,
                 f"finding.confidence 必须在 0..1，实际 {self.confidence}")
        _require(bool(_text(self.quote)) or bool(_text(self.statement)),
                 f"finding {self.id} 既没有原文引用也没有说明，无法举证")
        for eid in self.evidence_ids:
            ids.parse_evidence_id(eid)

    @property
    def is_blocking(self) -> bool:
        return self.severity in (Severity.BLOCKER, Severity.MAJOR)


@dataclass(frozen=True, slots=True)
class Dispute:
    """争议：同一处原文被两条结论同时命中且互相矛盾，交人工裁定。

    旧报告的 disputes[] 没有 id、没有状态，「谁处理过、处理结果」无处记录。
    """

    id: str
    run_id: str
    point: str
    issue_ids: tuple[str, ...] = ()
    kind: DisputeKind = DisputeKind.OTHER
    status: DisputeStatus = DisputeStatus.OPEN
    between: tuple[str, ...] = ()
    resolution_hint: str = ""
    resolution: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        ids.parse_dispute_id(self.id)
        _require(bool(_text(self.point)), "dispute.point 不能为空")
        _require(len(self.issue_ids) >= 1, "dispute 至少要指向一条 finding")
        for iid in self.issue_ids:
            ids.parse_finding_id(iid)
        for name in self.between:
            _require(bool(_text(name)), "dispute.between 含空值")
        if self.status is DisputeStatus.RESOLVED:
            _require(bool(_text(self.resolution)), "已解决的争议必须写 resolution")
            _require(bool(_text(self.resolved_by)), "已解决的争议必须记录 resolved_by")


@dataclass(frozen=True, slots=True)
class Decision:
    """最终裁定：verdict + 依据 + 风险等级 + 是否需人工复核。

    关键约束：裁定必须能由 policy.py 复算。Decision 只存结论与依据，
    不存推理过程；推理过程是 AuditEvent（可回放）。
    """

    id: str
    run_id: str
    verdict: Verdict
    rationale: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    reasons: tuple[str, ...] = ()
    rule_refs: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    requires_human_review: bool = False
    policy_id: str = "review-decision-v1"
    approved_by: str | None = None
    approved_at: datetime.datetime | None = None
    decided_at: datetime.datetime | None = None

    def __post_init__(self) -> None:
        ids.parse_decision_id(self.id)
        _require(bool(_text(self.run_id)), "decision.run_id 不能为空")
        _require(bool(_text(self.rationale)) or bool(self.reasons),
                 "裁定必须给出理由（rationale 或 reasons 至少一项）")
        for iid in self.finding_ids:
            ids.parse_finding_id(iid)
        for rid in self.rule_refs:
            _require(bool(_RULE_RE.match(rid)), f"decision.rule_refs 含非法规则号：{rid!r}")
        if self.approved_by is not None:
            _require(bool(_text(self.approved_by)), "approved_by 不能是空字符串")
            _require(self.approved_at is not None, "人工批准必须记录 approved_at")

    @property
    def is_final(self) -> bool:
        """是否已可对外交付：不需要人工复核，或已有人批准。"""
        return (not self.requires_human_review) or self.approved_by is not None


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """可恢复执行点：阶段 + 序号 + 状态哈希。

    有它才能回答「中断后从哪继续」。旧实现没有 checkpoint，server 侧只能整单重跑。
    """

    run_id: str
    stage: Stage
    sequence: int
    state_hash: str
    created_at: datetime.datetime
    summary: str = ""

    def __post_init__(self) -> None:
        _require(bool(_text(self.run_id)), "checkpoint.run_id 不能为空")
        _require(self.sequence >= 0, "checkpoint.sequence 不能为负")
        _require(bool(_HASH_RE.match(self.state_hash.lower())),
                 "checkpoint.state_hash 必须是 64 位十六进制")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """审计事件：步骤 / 工具调用 / 决策 / 模型调用 / 状态迁移 / 人工动作。

    旧实现分三处记录（steps 数组、decisions.jsonl、tools.jsonl），字段各不相同，
    「审计中心」只能按文件分别展示。统一成一条事件流后才能按时间轴回放。
    """

    seq: int
    run_id: str
    kind: AuditKind
    name: str
    status: str = "success"
    ts: datetime.datetime | None = None
    latency_ms: int = 0
    detail: Mapping[str, Any] = field(default_factory=dict)
    ref: str | None = None

    _STATUSES = frozenset({"success", "failed", "skipped", "aborted", "info"})

    def __post_init__(self) -> None:
        _require(self.seq >= 0, "audit.seq 不能为负")
        _require(bool(_text(self.run_id)), "audit.run_id 不能为空")
        _require(bool(_text(self.name)), "audit.name 不能为空")
        _require(self.status in self._STATUSES,
                 f"audit.status 非法：{self.status!r}")
        _require(self.latency_ms >= 0, "audit.latency_ms 不能为负")
        object.__setattr__(self, "detail", _frozen(self.detail))


# ---------------------------------------------------------------------------
# 聚合：Run / Review
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ReviewRun:
    """一次执行。"""

    id: str
    review_id: str
    seq: int
    status: RunStatus
    stage: Stage
    mode: RunMode
    profile: ReviewProfile
    started_at: datetime.datetime
    finished_at: datetime.datetime | None = None
    integrity: IntegrityHashes = field(default_factory=IntegrityHashes)
    coverage: Coverage | None = None
    stats: RunStats = field(default_factory=RunStats)
    decision_id: str | None = None
    error: str | None = None
    checkpoint: Checkpoint | None = None

    def __post_init__(self) -> None:
        review_id, seq = ids.parse_run_id(self.id)
        _require(review_id == self.review_id,
                 f"run_id {self.id} 与 review_id {self.review_id} 不匹配")
        _require(seq == self.seq, f"run_id 序号 {seq} 与 seq 字段 {self.seq} 不一致")
        if self.decision_id is not None:
            ids.parse_decision_id(self.decision_id)
        if self.status.is_terminal:
            _require(self.finished_at is not None,
                     f"Run {self.id} 已到终态 {self.status}，必须记录 finished_at")
        else:
            _require(self.finished_at is None,
                     f"Run {self.id} 未结束（{self.status}），不应有 finished_at")
        if self.status is RunStatus.COMPLETED:
            _require(self.decision_id is not None,
                     "完成的 Run 必须关联一条 Decision")
            _require(self.stage is Stage.FINALIZE,
                     "完成的 Run 必须走完 FINALIZE")
            _require(not self.error, "完成的 Run 不应有 error")
        if self.status in (RunStatus.FAILED, RunStatus.ABORTED):
            _require(bool(_text(self.error)),
                     f"Run {self.id} 状态为 {self.status}，必须记录 error")
        if self.finished_at is not None and self.started_at is not None:
            _require(self.finished_at >= self.started_at,
                     "finished_at 早于 started_at")


@dataclass(frozen=True, slots=True)
class Review:
    """审查任务：身份 + 目标 + Profile + 历次执行。"""

    id: str
    target: ReviewTarget
    profile_id: str
    profile_version: str
    created_at: datetime.datetime
    status: ReviewStatus = ReviewStatus.CREATED
    origin: ReviewOrigin = ReviewOrigin.WEB
    title: str = ""
    declared_purpose: str | None = None
    runs: tuple[ReviewRun, ...] = ()

    def __post_init__(self) -> None:
        ids.parse_review_id(self.id)
        _require(bool(_text(self.profile_id)), "review.profile_id 不能为空")
        _require(bool(_text(self.profile_version)), "review.profile_version 不能为空")
        for run in self.runs:
            _require(run.review_id == self.id,
                     f"Run {run.id} 不属于 Review {self.id}")
        if self.runs:
            seqs = [r.seq for r in self.runs]
            _require(seqs == list(range(1, len(seqs) + 1)),
                     f"Run 序号必须从 1 连续递增，实际 {seqs}")
        if self.status in (ReviewStatus.IN_PROGRESS, ReviewStatus.COMPLETED,
                           ReviewStatus.ARCHIVED):
            _require(bool(self.runs), f"状态 {self.status} 的 Review 必须至少有一次 Run")
        if self.status is ReviewStatus.COMPLETED:
            _require(self.runs[-1].status is RunStatus.COMPLETED,
                     "Review 标记完成时，最后一次 Run 必须已完成")

    @property
    def current_run(self) -> ReviewRun | None:
        return self.runs[-1] if self.runs else None

    def add_run(self, run: ReviewRun) -> "Review":
        """返回追加 Run 后的新 Review（不就地修改，保持不可变）。"""
        _require(run.review_id == self.id, f"Run {run.id} 不属于 Review {self.id}")
        _require(run.seq == len(self.runs) + 1,
                 f"Run 序号应为 {len(self.runs) + 1}，实际 {run.seq}")
        return Review(
            id=self.id, target=self.target, profile_id=self.profile_id,
            profile_version=self.profile_version, created_at=self.created_at,
            status=ReviewStatus.IN_PROGRESS, origin=self.origin, title=self.title,
            declared_purpose=self.declared_purpose, runs=self.runs + (run,),
        )
