# -*- coding: utf-8 -*-
"""裁定策略：确定性、可复算、可解释。

这是 Decision Engine 的 v1，规则刻意与旧实现等价：

    blocker > 0                    → fail（高 / 极高风险）
    否则 major > 0                 → warn（中等风险）
    否则库外事实未核完 / 待确认     → warn（中等风险）
    否则                           → pass（低风险）
    置信度 < 0.7、存在争议、覆盖不全 → 必须人工复核（不改变 verdict）

为什么放在 domain 而不是 Agent 节点里：
    「结论怎么来的」必须是代码算出来的，不是模型说的。旧实现把这段逻辑写在
    run_one 的 S6 里（review.py:2671-2683），和统计、聚合、渲染混在一个 900 行函数里，
    没法单独测。这里抽成纯函数：输入 findings + 争议，输出 DecisionDraft，
    不读文件、不调模型、不看时间。

`consistency_issues()` 是给「能否证明最终裁定符合规则」这条验收标准用的：
拿一份报告里的 findings 重新跑策略，和报告自带的 verdict 比对，不一致就报出来。
历史 202 份报告里有 5 份对不上（research 未核完却给了 pass），属于旧版本缺陷，
已登记在 checks/test_legacy_adapter.py 的已知差异清单里。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from .enums import FindingOrigin, RiskLevel, Severity, Verdict
from .models import Coverage, Dispute, Finding, RunStats

POLICY_ID = "review-decision-v1"


@dataclass(frozen=True, slots=True)
class DecisionDraft:
    """策略输出：还没落成 Decision（缺 id / 时间 / 批准人）。"""

    verdict: Verdict
    risk_level: RiskLevel
    reasons: tuple[str, ...] = ()
    rule_refs: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    requires_human_review: bool = False
    policy_id: str = POLICY_ID


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    """策略参数。改参数 = 改口径，必须升 version 并留 ADR。"""

    confidence_threshold: float = 0.7
    fail_on_blocker: bool = True
    warn_on_major: bool = True
    warn_on_unverified_research: bool = True
    review_on_disputes: bool = True
    review_on_incomplete_coverage: bool = False
    critical_blocker_count: int = 3
    version: str = "1"

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError("confidence_threshold 必须在 0..1")
        if self.critical_blocker_count < 1:
            raise ValueError("critical_blocker_count 至少为 1")

    def decide_from_counts(
        self,
        *,
        blocker: int,
        major: int,
        external_unverified: bool = False,
        disputes: int = 0,
        low_confidence: int = 0,
        coverage_incomplete: bool = False,
        total: int | None = None,
    ) -> DecisionDraft:
        """按计数裁定（不依赖 Finding 对象）。

        旧主流程（review.py S6）手上只有 stats 计数，没有领域对象；为了让新策略
        能接管那一步、又不强迫阶段 1 先把所有 dict 改成对象，这里提供同一套规则的
        计数入口。`decide()` 内部也走这里，保证两条入口不会算出两个 verdict。
        """
        reasons: list[str] = []
        if blocker and self.fail_on_blocker:
            verdict = Verdict.FAIL
            risk = (RiskLevel.CRITICAL if blocker >= self.critical_blocker_count
                    else RiskLevel.HIGH)
            reasons.append(f"存在 {blocker} 条 blocker（阻断级）→ 裁定 fail")
        elif major and self.warn_on_major:
            verdict = Verdict.WARN
            risk = RiskLevel.MODERATE
            reasons.append(f"无 blocker，但存在 {major} 条 major（严重级）→ 裁定 warn")
        elif external_unverified and self.warn_on_unverified_research:
            verdict = Verdict.WARN
            risk = RiskLevel.MODERATE
            reasons.append("规则层无 major，但库外事实未核完/仅单一来源 → 不得给 pass")
        else:
            verdict = Verdict.PASS
            risk = RiskLevel.LOW
            shown_total = blocker + major if total is None else total
            reasons.append(f"无 blocker/major（共 {shown_total} 条异议），裁定 pass")

        needs_human = (
            verdict is not Verdict.PASS
            or low_confidence > 0
            or (self.review_on_disputes and disputes > 0)
            or (self.review_on_incomplete_coverage and coverage_incomplete)
        )
        if low_confidence:
            reasons.append(
                f"{low_confidence} 条发现置信度 < {self.confidence_threshold:g}，需人工确认")
        if disputes:
            reasons.append(f"{disputes} 处争议未裁定，需人工复核")

        return DecisionDraft(
            verdict=verdict,
            risk_level=risk,
            reasons=tuple(reasons),
            requires_human_review=needs_human,
        )

    def decide(
        self,
        findings: Sequence[Finding],
        *,
        disputes: Sequence[Dispute] = (),
        external_unverified: bool = False,
        coverage: Coverage | None = None,
    ) -> DecisionDraft:
        stats = RunStats.from_findings(findings)
        low_confidence = [f for f in findings
                          if f.confidence < self.confidence_threshold]
        draft = self.decide_from_counts(
            blocker=stats.blocker,
            major=stats.major,
            external_unverified=external_unverified,
            disputes=len(disputes),
            low_confidence=len(low_confidence),
            coverage_incomplete=bool(coverage is not None and not coverage.complete),
            total=stats.total,
        )

        # 支撑结论的发现：fail 归因于 blocker，warn 归因于 major，
        # pass 不挂发现。与旧实现的 ids/refs 收集口径一致。
        if draft.verdict is Verdict.FAIL:
            supporting = [f for f in findings if f.severity is Severity.BLOCKER]
        elif draft.verdict is Verdict.WARN:
            supporting = [f for f in findings if f.severity is Severity.MAJOR]
        else:
            supporting = []
        return replace(
            draft,
            rule_refs=tuple(dict.fromkeys(
                f.rule.rule_id for f in supporting if f.rule)),
            finding_ids=tuple(dict.fromkeys(f.id for f in supporting)),
        )


DEFAULT_POLICY = DecisionPolicy()


def consistency_issues(findings: Iterable[Finding], verdict: Verdict,
                       *, policy: DecisionPolicy = DEFAULT_POLICY,
                       external_unverified: bool = False) -> tuple[str, ...]:
    """复算裁定，返回与给定 verdict 不一致的说明。

    这是「能否证明最终裁定符合规则」这条验收标准的实现：不信任报告里的 verdict，
    用同一套策略重算一遍。
    """
    draft = policy.decide(list(findings), external_unverified=external_unverified)
    if draft.verdict is Verdict(verdict):
        return ()
    return (f"报告裁定 {verdict}，策略复算为 {draft.verdict}"
            f"（{'；'.join(draft.reasons)}）",)


def finding_violations(findings: Iterable[Finding],
                       *, policy: DecisionPolicy = DEFAULT_POLICY) -> tuple[str, ...]:
    """发现的软不变量：不合规不阻止加载，但要能被报告出来。

    历史报告里有 19 条「blocker/major 但置信度 0.6」——早期版本没做严重度下调。
    领域层若在构造函数里抛异常，历史数据就永远读不进来；这里改成显式清单，
    由适配器写进 warnings，由阶段 3 的 Decision Engine 决定怎么处置。
    """
    out: list[str] = []
    for f in findings:
        if (f.severity in (Severity.BLOCKER, Severity.MAJOR)
                and f.confidence < policy.confidence_threshold
                and not f.severity_note):
            out.append(
                f"{f.id} 严重度 {f.severity} 但置信度 {f.confidence:.2f} "
                f"< {policy.confidence_threshold:g}，且未记录下调理由")
        if f.origin is FindingOrigin.RULE and f.rule is None:
            out.append(f"{f.id} 声称来自规则，但没有挂 rule_id")
        if f.status.is_terminal and not f.evidence_ids:
            out.append(f"{f.id} 已定稿（{f.status}）但没有任何证据引用")
    return tuple(out)
