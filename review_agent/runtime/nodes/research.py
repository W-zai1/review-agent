# -*- coding: utf-8 -*-
"""库外事实核验节点（Fact Checker）。

职责：把「材料里说的数字/日期」拿去外部来源核一遍，并把结果落成**两种东西**：

    1. 发现（Finding）：只有「被反驳」的主张才成为问题（`FACT-001`），
       severity 取决于来源够不够权威——单一来源的反驳只到 minor，
       因为搜索摘要的统计口径（累计/单季、全球/中国）经常和原文不同，
       把「可能错」写成 major 就是把推测冒充成定论。
    2. 证据（Evidence）：`kind=EXTERNAL_SOURCE` + `source_ref`（来源 URL）。
       这是全仓第一次真正产出外部来源证据——以前 `EXTERNAL_SOURCE` 只存在于
       模型输出的校验分支里，`source_ref` 从未被填过。

三态语义（沿用 `tools/research.py`）：

    supported   → 来源与原文一致：不进发现，进报告的 `research.supported`
    refuted     → 来源与原文不一致：进发现 + 证据（`needs_confirm` 见上）
    unverified  → 查过但没有可比来源：**不是失败**，也不是虚假，
                  进 `research.unverified` 并写进 limitations

为什么它和语义审节点一样只拿「执行器」
--------------------------------------
节点在 `runtime` 层，不许 import `capabilities`（依赖方向硬规则），更不许
import `tools/`。所以「怎么抽主张、怎么检索、缓存怎么用」全部由
`ResearchExecutor` 端口注入；节点只做三件确定性的事：
按判定表生成发现与证据、把块原样挂进状态、把错误与截断降级成告警。

与旧链路的一致性
----------------
报告 `research` 段由 `tools/research.build_block()` 产出——**与旧链路同一个函数**，
不是「两份看着像的代码」。条目里 `needs_confirm`、来源截断（2 条）、
「支持/反驳/未找到」的分类也都在那一处。
"""
from __future__ import annotations

import datetime
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from ...domain import (
    ConfidenceBasis,
    Evidence,
    EvidenceKind,
    Finding,
    FindingOrigin,
    FindingStatus,
    Location,
    RuleRef,
    Severity,
    SourceRef,
    Stage,
    Verification,
    ids,
)
from ..node import NodeContext, NodeResult

NAME: Final = "fact-checker"
RULE_ID: Final = "FACT-001"
DIMENSION: Final = "fact"
TOOL_NAME: Final = "research.web"
# 与 `tools/research.py` 的 authoritative 判据一致：>= 0.8 才算「够权威」
AUTHORITATIVE_CONFIDENCE: Final = 0.8
# 单一来源反驳的置信度：低于 0.7，进 to_confirm，不进整改单
WEAK_REFUTATION_CONFIDENCE: Final = 0.65


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    """一条待核验的主张（`tools/research.extract_claims()` 的领域视图）。"""

    quote: str
    entity: str = ""
    metric: str = ""
    kind: str = "number"
    value: Any = None
    unit: str = ""
    year: int | None = None
    sentence_index: int = 1
    raw: str = ""

    def __post_init__(self) -> None:
        if not str(self.quote or "").strip():
            raise ValueError("ResearchClaim.quote 不能为空：没有原文就没有可核验的主张")
        if self.sentence_index < 1:
            raise ValueError("sentence_index 从 1 开始")

    def as_dict(self) -> dict[str, Any]:
        return {
            "quote": self.quote, "entity": self.entity, "metric": self.metric,
            "kind": self.kind, "value": self.value, "unit": self.unit,
            "year": self.year, "sentence_index": self.sentence_index,
            "raw": self.raw,
        }


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    """一条主张的核验结论 + 它在报告 `research.items[]` 里的那一行。

    `item` 由 `tools/research.item_from_verdict()` 产出（与旧链路同一个函数），
    节点不重新拼一遍——重拼就会在 `needs_confirm` 这类细节上漂移。
    `value` 是**来源给出的值**（不是原文里的值）：改写建议要写它。
    """

    claim: ResearchClaim
    status: str
    confidence: float = 0.0
    method: str = "search"
    value: Any = None
    display: str = ""
    reason: str = ""
    query: str = ""
    sources: tuple[Mapping[str, Any], ...] = ()
    item: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ("supported", "refuted", "unverified"):
            raise ValueError(f"未知的核验结论：{self.status!r}")

    @property
    def refuted(self) -> bool:
        return self.status == "refuted"

    @property
    def authoritative(self) -> bool:
        """来源够不够权威：决定这条反驳是 major（可进整改单）还是 minor（待确认）。"""
        return self.refuted and self.confidence >= AUTHORITATIVE_CONFIDENCE

    @property
    def needs_confirm(self) -> bool:
        return bool(self.item.get("needs_confirm"))


@dataclass(frozen=True, slots=True)
class ResearchOutcome:
    """一次库外核验的执行结果。

    `block`：直接挂进报告 `research` 段的字典（键名/分类与旧报告一致）。
    `verdicts`：逐条结论，节点据此生成发现与证据。
    """

    enabled: bool = True
    block: Mapping[str, Any] = field(default_factory=dict)
    verdicts: tuple[ClaimVerdict, ...] = ()
    reason: str = ""
    errors: tuple[str, ...] = ()

    @property
    def checked(self) -> int:
        return len(self.verdicts)

    @property
    def refuted(self) -> tuple[ClaimVerdict, ...]:
        return tuple(v for v in self.verdicts if v.refuted)

    @property
    def stopped_early(self) -> bool:
        return bool(self.block.get("stopped_early"))


# (text) -> 这一次核验的全部结论。实现方负责抽主张、查缓存、联网、落缓存。
ResearchExecutor = Callable[[str], Awaitable[ResearchOutcome]]


def build_location(claim: ResearchClaim) -> Location:
    return Location(label=f"第 {claim.sentence_index} 句", sentence=claim.sentence_index)


def research_evidence_text(verdict: ClaimVerdict) -> str:
    """旧报告里 `FACT-001` 的「依据」文字（逐字沿用，便于并排比对）。"""
    claim = verdict.claim
    srcs = verdict.sources
    label = _label(verdict)
    return (
        f"外部检索核验：{verdict.reason}；"
        f"来源 {srcs[0].get('title') if srcs else '—'}"
        f"（{srcs[0].get('url') if srcs else '无链接'}）显示 {label}，"
        f"原文为 {claim.raw}"
        + ("。已按来源改写。" if verdict.authoritative else "。口径可能不同，未自动改写。")
    )


def research_suggestion(verdict: ClaimVerdict) -> str:
    srcs = verdict.sources
    label = _label(verdict)
    if verdict.authoritative:
        return (f"改为「{label}」并注明来源："
                f"{srcs[0].get('url') if srcs else '（检索来源见报告）'}")
    return (f"检索到不同数值（{label}），请先确认口径后再改写；"
            f"来源：{srcs[0].get('url') if srcs else '见报告'}")


def research_fix(verdict: ClaimVerdict) -> dict[str, Any]:
    """旧报告 `issue["fix"]` 的同形改写建议（改哪句 / 怎么改 / 不改会怎样）。

    为什么要有它：`remediation.build_fix()` 看到一条 `FACT-001` 但没有对应的事实库
    断言时，只能给出「补统计口径」这类**通用**建议；而外部检索已经拿到了具体数值，
    正确的建议是「把 5 亿改成 1.69 亿」。少了这一条，整改单会从「可直接改」降级成
    「需人工改写」——用户要多做一步才能得到已有答案。
    """
    claim = verdict.claim
    srcs = verdict.sources
    label = _label(verdict)
    topic = f"{claim.entity}{claim.metric}".strip()
    consequence = "对外发布构成事实性错误；被引用、被追责时无法用「笔误」解释。"
    if verdict.authoritative:
        return {
            "action": "replace", "where": f"第 {claim.sentence_index} 句",
            "from": claim.quote,
            "to": (claim.quote.replace(claim.raw, label, 1)
                   if claim.raw else None),
            "topic": topic, "consequence": consequence,
            "how": f"改为「{label}」并按来源标注统计口径与时间。",
            "has_rewrite": bool(claim.raw),
            "summary": f"把「{claim.raw or claim.quote[:30]}」改为「{label}」",
        }
    # 单一非权威来源：可以提示「检索到不同数值」，但不能替用户拍板改成哪个——
    # 口径（累计/单季、全球/中国）可能不同，硬改写就是造新错。
    return {
        "action": "manual", "where": f"第 {claim.sentence_index} 句",
        "from": claim.quote, "to": None, "topic": topic,
        "consequence": consequence,
        "how": (f"检索到 {label}（{srcs[0].get('url') if srcs else '来源见报告'}），"
                f"与原文 {claim.raw} 不一致；先确认统计口径与年份，"
                f"确认后改为 {label} 并注明来源。"),
        "has_rewrite": False,
        "summary": f"先确认口径：检索到 {label}，与原文 {claim.raw} 不一致",
    }


def _label(verdict: ClaimVerdict) -> str:
    """来源给出的值（旧报告的 `label` 口径：display 优先，其次数值 + 单位）。"""
    if verdict.display:
        return verdict.display
    got = verdict.value
    if isinstance(got, (int, float)):
        return f"{got:.6g}{verdict.claim.unit or ''}"
    return str(got if got is not None else "—")


def finding_from_verdict(verdict: ClaimVerdict, *, seq: int,
                         run_id: str) -> Finding:
    """被反驳的主张 → `FACT-001` 发现（严重度/置信度与旧链路同口径）。"""
    claim = verdict.claim
    authoritative = verdict.authoritative
    confidence = (verdict.confidence if authoritative
                  else WEAK_REFUTATION_CONFIDENCE)
    return Finding(
        id=ids.format_finding_id(seq), run_id=run_id,
        dimension=DIMENSION,
        severity=Severity.MAJOR if authoritative else Severity.MINOR,
        quote=claim.quote, location=build_location(claim),
        confidence=float(confidence),
        origin=FindingOrigin.RESEARCH,
        rule=RuleRef(rule_id=RULE_ID, dimension=DIMENSION,
                     declared_severity=Severity.MAJOR),
        statement=research_evidence_text(verdict),
        confidence_basis=ConfidenceBasis.RESEARCH,
        # 与 `adapters/issue_codec.encode_issue` 同一口径：置信度不足要标待确认
        status=(FindingStatus.NEEDS_CONFIRMATION
                if confidence < 0.7 else FindingStatus.PROPOSED),
        tool=f"research:{verdict.method}",
        suggestion=research_suggestion(verdict),
    )


def _source_ref(row: Mapping[str, Any]) -> SourceRef:
    """来源行 → `SourceRef`（域名/官方域/抓取时间/正文快照哈希）。

    为什么必须建这个对象：`kind=EXTERNAL_SOURCE` 的证据如果没有来源元数据，
    「外部核验过」就是一句无法复核的自我声明——半年后没人知道引的是哪个域、
    算不算官方、抓的是正文还是搜索摘要。`SourceRef` 让这几项随证据落盘。
    """
    url = str(row.get("url") or "")
    fetched_at = None
    raw_time = row.get("fetched_at") or row.get("retrieved_at")
    if raw_time not in (None, ""):
        try:
            fetched_at = datetime.datetime.fromisoformat(str(raw_time))
        except ValueError:
            fetched_at = None
    return SourceRef(
        url=url,
        domain=str(row.get("domain") or ""),
        official=bool(row.get("official")),
        fetched_at=fetched_at,
        snapshot_hash=(str(row["snapshot_hash"]).strip().lower()
                       if row.get("snapshot_hash") else None),
    )


def evidence_from_verdict(verdict: ClaimVerdict, *, seq: int, run_id: str,
                          finding_id: str) -> Evidence | None:
    """被反驳的主张 → 一条 `EXTERNAL_SOURCE` 证据（带来源 URL 与 `SourceRef`）。

    没有来源就返回 None：**不允许**造一条「没有出处的外部证据」。
    `verification` 保持 `NOT_CHECKED`——它校验的是「这段引用在不在待审原文里」，
    而这里引用的是**别人网页上的话**，两者不能混为一谈；
    `strength` 保持缺省 `UNASSESSED`（评估是批次 3 / B2 的评估节点的事）。
    """
    sources = verdict.sources
    if not sources:
        return None
    first = sources[0]
    url = str(first.get("url") or "")
    if not url:
        return None
    snippet = str(first.get("snippet") or "").strip()
    note = "｜".join(bit for bit in (
        f"外部检索：{verdict.status}",
        str(verdict.reason or ""),
        f"来源 {first.get('title') or '（无标题）'}（{url}）",
        f"原文为 {verdict.claim.raw}" if verdict.claim.raw else "",
    ) if bit)
    return Evidence(
        id=ids.format_evidence_id(seq), run_id=run_id,
        kind=EvidenceKind.EXTERNAL_SOURCE,
        quote=snippet or verdict.claim.quote,
        location=build_location(verdict.claim),
        source_ref=url,
        source=_source_ref(first),
        verification=Verification.NOT_CHECKED,
        verified_by=TOOL_NAME,
        note=note[:500],
        finding_ids=(finding_id,),
    )


def research_limitations(block: Mapping[str, Any] | None) -> tuple[str, ...]:
    """报告 limitations 里必须说清的边界（没做 / 做了一半 / 失败）。"""
    if not block:
        return (
            "未做库外事实核验：仅比对项目内置事实库；库外实体只判「缺可核验出处」，"
            "不判真伪。如需联网核验，用 --research 运行。",)
    if block.get("error"):
        return (f"库外事实核验失败（{block['error']}），本次结论未包含外部检索。",)
    if not block.get("enabled"):
        return (f"未做库外事实核验：{block.get('reason') or '本次未启用'}；"
                "库外实体只判「缺可核验出处」，不判真伪。",)
    limits = [
        f"库外事实核验：检索 {block.get('checked', 0)} 条主张"
        f"（支持 {len(block.get('supported') or [])} / "
        f"反驳 {len(block.get('refuted') or [])} / "
        f"未找到 {len(block.get('unverified') or [])}）。"
        "「未找到」表示公开检索没有给出可比来源，不等于该说法虚假；"
        "「反驳」也只说明检索来源与原文数值不一致，最终口径需人工确认。",
    ]
    if block.get("stopped_early"):
        limits.append(
            f"库外核验因超时提前结束：只核了 {block.get('checked', 0)} / "
            f"{block.get('claims_found', 0)} 条主张，其余未检索。")
    if block.get("errors"):
        limits.append("部分检索失败："
                      + "；".join(str(e) for e in list(block["errors"])[:3])[:300])
    if block.get("needs_confirm"):
        limits.append(
            f"{len(block['needs_confirm'])} 条反驳只有单一来源支撑，已转入待人工确认，"
            "未进入整改单——统计口径不同就可能出现这种差异。")
    return tuple(limits)


class FactCheckNode:
    """analysis 阶段（库外核验部分）节点：检索 → 发现 + 证据 + 报告块。"""

    name: Final = NAME
    stage: Final = Stage.ANALYSIS
    # `research` 是「这一阶段算出来的结论块」，随状态落盘、随报告出门。
    produces: Final = frozenset({"research"})

    def __init__(self, executor: ResearchExecutor) -> None:
        self.executor = executor

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.ANALYSIS:
            return NodeResult.fail(
                f"{NAME} 只处理 analysis 阶段，当前状态在 {state.stage.value}")
        text = state.artifacts.get("target_text")
        if not isinstance(text, str):
            return NodeResult.fail(
                "analysis 阶段缺少 artifacts['target_text']：没有原文无从抽取主张")

        dimensions = tuple(ctx.config.get("dimensions") or state.profile.dimensions)
        if DIMENSION not in dimensions:
            # 代码对象 / 未声明事实维度的 Agent：不发请求，但要如实说「没做」
            return NodeResult.ok(output={"research": {
                "enabled": False,
                "reason": f"本次维度不含 {DIMENSION}（Agent/对象类型未开启事实审）",
                "claims_found": 0, "checked": 0,
                "supported": [], "refuted": [], "unverified": [],
                "needs_confirm": [], "items": [], "errors": [],
                "cache_hits": 0, "learned_hits": 0, "local_hits": 0,
            }})

        try:
            outcome = self.executor(text)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except Exception as exc:                    # noqa: BLE001 - 边界统一转失败
            return NodeResult.fail(
                f"库外事实核验失败：{type(exc).__name__}: {exc}")
        if not isinstance(outcome, ResearchOutcome):
            return NodeResult.fail(
                f"检索执行器返回了 {type(outcome).__name__}，必须是 ResearchOutcome")
        if not outcome.enabled:
            return NodeResult.ok(output={"research": dict(outcome.block) or {
                "enabled": False, "reason": outcome.reason or "本次未启用库外核验"}})

        findings: list[Finding] = []
        evidence: list[Evidence] = []
        fixes: dict[str, Any] = {}
        seq = int(ctx.next_finding_seq)
        ev_seq = _first_evidence_seq(state, ctx)
        for verdict in outcome.refuted:
            finding = finding_from_verdict(verdict, seq=seq, run_id=state.run_id)
            findings.append(finding)
            fixes[finding.id] = research_fix(verdict)
            seq += 1
            item = evidence_from_verdict(verdict, seq=ev_seq,
                                         run_id=state.run_id, finding_id=finding.id)
            if item is not None:
                evidence.append(item)
                ev_seq += 1

        # `fixes` 是**传输层**字段：报告层把它并进 `issues[].fix` 之后就会摘掉
        # （见 `application/shadow_report.py`），所以对外的 `research` 段形状
        # 仍与旧报告一致——不在这里重新发明一套报告结构。
        block = dict(outcome.block)
        if fixes:
            block["fixes"] = fixes

        warnings: list[str] = []
        if outcome.errors:
            warnings.append(
                f"{len(outcome.errors)} 次检索失败：{outcome.errors[0][:150]}")
        if outcome.stopped_early:
            warnings.append("库外核验因超时提前结束，后续主张未检索")
        if findings:
            warnings.append(
                f"外部检索反驳 {len(findings)} 条主张："
                f"{'、'.join(f.id for f in findings[:5])}")
        return NodeResult.ok(
            findings=tuple(findings),
            evidence=tuple(evidence),
            output={"research": block},
            warnings=tuple(warnings),
        )


def _first_evidence_seq(state: Any, ctx: NodeContext) -> int:
    """本节点可用的证据起始号：已有证据之后，且不低于配置的下限。

    证据编号是 Run 级资源。ANALYSIS 阶段新产生的证据若不避开 EVIDENCE 阶段
    的起号点，两个阶段就会各发一条 `EV-0001`——报告里「这条证据指向哪个发现」
    当场变成掷硬币。
    """
    best = 0
    for item in state.evidence:
        try:
            best = max(best, ids.parse_evidence_id(item.id))
        except Exception:                          # noqa: BLE001 - 脏 id 不参与编号
            continue
    try:
        floor = int(ctx.config.get("first_evidence_seq", 1))
    except (TypeError, ValueError):
        floor = 1
    return max(best + 1, floor)
