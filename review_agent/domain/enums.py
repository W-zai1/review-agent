# -*- coding: utf-8 -*-
"""领域枚举：系统里所有「状态 / 分类」的唯一取值来源。

为什么单独一个文件：
    旧实现里 severity / verdict / status 都是裸字符串，散落在 review.py、server.py、
    web/index.html 三处。加一个 severity 档位要改三个地方，漏掉一处就出现
    「前端不认识后端返回的档位」。这里用 StrEnum（Python 3.11+）固定取值，
    序列化时就是字符串，旧 JSON 报告可以原样喂进来。

约定：
    · 枚举值是**持久化契约**。改名 = 改所有历史报告，必须走 ADR。
    · 中文展示名不放进来（放 `label` 属性），避免「界面上改文案」变成「数据迁移」。
"""
from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    """发现（Finding）的严重度。顺序即排序权重，与 review.py 的 SEV_ORDER 对齐。"""

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @property
    def label(self) -> str:
        return _SEVERITY_LABEL[self]

    @classmethod
    def ordered(cls) -> tuple["Severity", ...]:
        return (cls.BLOCKER, cls.MAJOR, cls.MINOR, cls.NIT)


_SEVERITY_RANK = {
    Severity.BLOCKER: 0,
    Severity.MAJOR: 1,
    Severity.MINOR: 2,
    Severity.NIT: 3,
}
_SEVERITY_LABEL = {
    Severity.BLOCKER: "阻断",
    Severity.MAJOR: "严重",
    Severity.MINOR: "一般",
    Severity.NIT: "建议",
}


class Verdict(StrEnum):
    """最终裁定。只允许三档，和报告里的 verdict_scale 一致。"""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"

    @property
    def label(self) -> str:
        return {"pass": "通过", "warn": "警告", "fail": "不通过"}[self.value]


class RiskLevel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def label(self) -> str:
        return {"low": "低", "moderate": "中", "high": "高", "critical": "极高"}[self.value]


class ReviewStatus(StrEnum):
    """审查任务（Review）的生命周期。"""

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class RunStatus(StrEnum):
    """一次执行（ReviewRun）的状态。

    AWAITING_DECISION 是「分析做完了、等裁定/等人工复核」的显式停点：
    旧实现把「跑完」和「裁定完」混在一个状态里，中断恢复时无法判断该不该重跑分析。
    """

    CREATED = "created"
    RUNNING = "running"
    AWAITING_DECISION = "awaiting_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ABORTED)


class Stage(StrEnum):
    """Review Workflow v1 的八个阶段（见 docs/AGENT_RUNTIME.md）。

    旧 S1~S6 的映射：S1→INTAKE+INGESTION，S2→PLANNING，S3→ANALYSIS，
    S4→EVIDENCE，S5→CROSS_REVIEW，S6→DECISION+FINALIZE；
    S5.5（目的比对）/ S5.6（库外事实核验）是 ANALYSIS 的补算子阶段。
    """

    INTAKE = "intake"
    INGESTION = "ingestion"
    PLANNING = "planning"
    ANALYSIS = "analysis"
    EVIDENCE = "evidence"
    CROSS_REVIEW = "cross_review"
    DECISION = "decision"
    FINALIZE = "finalize"

    @property
    def order(self) -> int:
        return _STAGE_ORDER.index(self)

    @property
    def label(self) -> str:
        return _STAGE_LABEL[self]


_STAGE_ORDER = (
    Stage.INTAKE, Stage.INGESTION, Stage.PLANNING, Stage.ANALYSIS,
    Stage.EVIDENCE, Stage.CROSS_REVIEW, Stage.DECISION, Stage.FINALIZE,
)
_STAGE_LABEL = {
    Stage.INTAKE: "接收",
    Stage.INGESTION: "摄取",
    Stage.PLANNING: "规划",
    Stage.ANALYSIS: "分析",
    Stage.EVIDENCE: "证据",
    Stage.CROSS_REVIEW: "交叉复核",
    Stage.DECISION: "裁定",
    Stage.FINALIZE: "归档",
}


class FindingStatus(StrEnum):
    """发现的处置状态。

    PROPOSED 之后必须由「规则 + 证据 + 人」之一推进，不允许模型自证为 CONFIRMED。
    """

    PROPOSED = "proposed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    SUPPRESSED = "suppressed"

    @property
    def is_terminal(self) -> bool:
        return self in (
            FindingStatus.CONFIRMED,
            FindingStatus.REJECTED,
            FindingStatus.SUPPRESSED,
        )


class FindingOrigin(StrEnum):
    """发现从哪来。审计时用来区分「规则判的」和「模型说的」。"""

    RULE = "rule"
    MODEL = "model"
    HEURISTIC = "heuristic"
    RESEARCH = "research"
    OPEN_DISCOVERY = "open_discovery"


class ConfidenceBasis(StrEnum):
    """置信度的依据类型（历史报告里的 confidence_basis 取值）。"""

    RULE = "rule"
    HEURISTIC = "heuristic"
    MODEL = "model"
    RESEARCH = "research"


class EvidenceKind(StrEnum):
    QUOTE = "quote"
    EXTERNAL_SOURCE = "external_source"
    RULE_REFERENCE = "rule_reference"
    KNOWLEDGE_ENTRY = "knowledge_entry"
    TOOL_OUTPUT = "tool_output"


class Verification(StrEnum):
    """证据校验结果。

    ANCHORED = quote 逐字命中原文（review.py S4 已实现的硬校验）；
    PLACEHOLDER = 「（文档首段）」这类无原文可回查的占位引用（缺失型问题）；
    UNANCHORED = 声称引了原文但回查不到（S4 会丢弃，理论上不应出现在报告里）。
    """

    ANCHORED = "anchored"
    PLACEHOLDER = "placeholder"
    UNANCHORED = "unanchored"
    NOT_CHECKED = "not_checked"


class DisputeKind(StrEnum):
    SEVERITY_CONFLICT = "severity_conflict"
    DUPLICATE_FINDING = "duplicate_finding"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    CONTRADICTION = "contradiction"
    OTHER = "other"


class DisputeStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


class RunMode(StrEnum):
    """执行模式。值必须与历史报告的 review_mode 完全一致，否则适配器要写映射表。"""

    RULE_ONLY = "rule-only"
    RULE_LLM = "rule+llm"


class ReviewOrigin(StrEnum):
    WEB = "web"
    API = "api"
    CLI = "cli"
    IMPORT = "import"


class AuditKind(StrEnum):
    """审计事件类型。对应「审计中心」要展示的几类轨迹。"""

    STEP = "step"
    TOOL = "tool"
    DECISION = "decision"
    MODEL = "model"
    STATE = "state"
    HUMAN = "human"
