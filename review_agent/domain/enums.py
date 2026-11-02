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

    QUEUED / CANCELLED 是 2026-11-01 补的（此前「已提交」与「正在跑」是同一个
    `running`：用户取消不了，也说不清「在排队还是已开始」）。

    **「可恢复」刻意没有做成状态**：它是「`running` + 有检查点 + 没有活着的执行者」
    的三元判断（见 `transitions.recoverable`）。做成状态就会出现
    「记录说可恢复、检查点已被清理」两套真相，而恢复逻辑只能信其中一套。
    """

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_DECISION = "awaiting_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.COMPLETED, RunStatus.FAILED,
                        RunStatus.ABORTED, RunStatus.CANCELLED)


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
    """证据校验结果（第一根轴：**这句话在不在原文里**）。

    ANCHORED = quote 逐字命中原文（review.py S4 已实现的硬校验）；
    PLACEHOLDER = 「（文档首段）」这类无原文可回查的占位引用（缺失型问题）；
    UNANCHORED = 声称引了原文但回查不到（S4 会丢弃，理论上不应出现在报告里）。

    **这一根轴回答不了「这句话够不够证明这条结论」**——那是 `EvidenceStrength` 的事。
    两者正交、不得合并：把「引用命中」当成「证据成立」，正是历史报告里
    `verification=anchored` 被读成「证据充分」的那个坑。
    """

    ANCHORED = "anchored"
    PLACEHOLDER = "placeholder"
    UNANCHORED = "unanchored"
    NOT_CHECKED = "not_checked"


class EvidenceStrength(StrEnum):
    """证据强度（第二根轴）：**这条证据够不够支撑结论**。

    与 `Verification` 正交：
        Verification      说「引用在不在原文里」（锚定）
        EvidenceStrength  说「这条证据能不能当依据」（强度）
    一条 `verification=anchored` 的证据可以是 `PARTIAL`（引文确实在原文里，
    但它证明不了这条结论），也可以是 `CONTRADICTED`（原文这么写，外部来源给出相反数值）。

    取值语义（与 V1 方案 §3.2 一致）：

        UNASSESSED    未评估（**缺省值**：历史报告导入后即此值）
        UNVERIFIED    声称有来源但无从核验（无快照 / 无域名 / 抓取失败）
        PARTIAL       只核验了部分（如仅主体匹配、时间口径不明）
        VERIFIED      四项匹配全过且无相反证据
        CONTRADICTED  存在相反证据（必须进冲突处理，不能靠降权了事）

    为什么缺省是 `UNASSESSED` 而不是 `UNVERIFIED`：历史报告没有这根轴，
    标成 `UNVERIFIED` 等于**凭空断言它们不可靠**；标成 `UNASSESSED` 才是「没评估过」。
    闸门（批次 3）对两者都压裁定强度，但报告措辞与复算口径必须能区分
    「没评」与「评了但不足」。
    """

    UNASSESSED = "unassessed"
    UNVERIFIED = "unverified"
    PARTIAL = "partial"
    VERIFIED = "verified"
    CONTRADICTED = "contradicted"

    @property
    def rank(self) -> int:
        """强度序（越大越强），供「最弱一环」规则使用。

        `CONTRADICTED` 是 -1：它不是「弱」，而是**反证**——任何一条反证都要让整条
        结论进冲突处理，不能靠比大小把它盖过去（见批次 3 的 G3）。所以先判反证再比大小。
        """
        return _STRENGTH_RANK[self]

    @property
    def label(self) -> str:
        return _STRENGTH_LABEL[self]

    @classmethod
    def ordered(cls) -> tuple["EvidenceStrength", ...]:
        """弱 → 强（不含 `CONTRADICTED`：它不是同一根轴上的刻度）。"""
        return (cls.UNASSESSED, cls.UNVERIFIED, cls.PARTIAL, cls.VERIFIED)


_STRENGTH_RANK = {
    EvidenceStrength.CONTRADICTED: -1,
    EvidenceStrength.UNASSESSED: 0,
    EvidenceStrength.UNVERIFIED: 1,
    EvidenceStrength.PARTIAL: 2,
    EvidenceStrength.VERIFIED: 3,
}
_STRENGTH_LABEL = {
    EvidenceStrength.UNASSESSED: "未评估",
    EvidenceStrength.UNVERIFIED: "未核验",
    EvidenceStrength.PARTIAL: "部分核验",
    EvidenceStrength.VERIFIED: "已核验",
    EvidenceStrength.CONTRADICTED: "被反证",
}


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
