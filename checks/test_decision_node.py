#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_decision_node.py —— Decision Engine 节点的独立验收。

REFACTOR_PLAN 阶段 2 的验收标准是「每个节点都能单独测试」。这里把 Decision Engine
当成一个独立被测对象，不启动旧主流程、不读配置、不调模型：

    给定 findings + disputes + 研究兜底 → 必然得到同一个 Decision

三条硬要求：
    1. **可复算**：节点算出的裁定，用同一策略再算一遍必须一致（ADR-003）。
    2. **归属正确**：decision.run_id 必须属于本次 run；带着别的 run 的结论不许推进。
    3. **历史一致**：拿 207 份历史报告的 findings 重放，裁定必须与报告一致；
       不一致只允许是已登记的那 5 份（旧版本在「库外事实未核完」时错给了 pass）。

历史语料只在本地有（outputs/ 被 .gitignore 排除）；CI 跑前两组。
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (                      # noqa: E402
    STAGE_ORDER,
    ConfidenceBasis,
    Finding,
    FindingOrigin,
    InvalidTransition,
    InvariantViolation,
    Location,
    ReviewProfile,
    RiskLevel,
    RuleRef,
    Severity,
    Stage,
    Verdict,
)
from review_agent.runtime import NodeContext, NodeResult, RunState, advance  # noqa: E402
from review_agent.runtime.nodes import DecisionEngineNode, build_decision  # noqa: E402

try:                                                    # discover -s checks 与包导入都成立
    from test_legacy_adapter import KNOWN_VERDICT_DEVIATIONS
except ImportError:                                     # pragma: no cover
    from checks.test_legacy_adapter import KNOWN_VERDICT_DEVIATIONS

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"


def finding(seq=1, *, severity=Severity.MINOR, confidence=0.9,
            rule_id="COMP-004", run_id=RUN_ID):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=run_id, dimension="compliance",
        severity=severity, quote="截止目前，项目已完成 20 个里程碑节点",
        location=Location("第 5 句", 5), confidence=confidence,
        origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id=rule_id, dimension="compliance",
                     text="披露具体数据须标注口径与来源"),
        confidence_basis=ConfidenceBasis.RULE,
    )


def decision_state(findings=(), *, run_id=RUN_ID, stage=Stage.DECISION):
    """推进到 decision 阶段的 RunState（前六个阶段视为已完成）。"""
    done = STAGE_ORDER[:STAGE_ORDER.index(stage)]
    return RunState(
        review_id=REVIEW_ID, run_id=run_id, stage=stage,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
        completed_stages=done, findings=tuple(findings),
    )


class BuildDecisionTests(unittest.TestCase):
    """纯函数：输入 → 裁定。"""

    def test_blocker_means_fail(self):
        decision = build_decision([finding(1, severity=Severity.BLOCKER)],
                                  run_id=RUN_ID)
        self.assertIs(decision.verdict, Verdict.FAIL)
        self.assertIs(decision.risk_level, RiskLevel.HIGH)
        self.assertEqual(decision.finding_ids, ("ISS-0001",))
        self.assertEqual(decision.rule_refs, ("COMP-004",))
        self.assertTrue(decision.requires_human_review)

    def test_three_blockers_escalate_to_critical(self):
        findings = [finding(i, severity=Severity.BLOCKER) for i in (1, 2, 3)]
        decision = build_decision(findings, run_id=RUN_ID)
        self.assertIs(decision.risk_level, RiskLevel.CRITICAL)

    def test_major_means_warn(self):
        decision = build_decision([finding(1, severity=Severity.MAJOR)],
                                  run_id=RUN_ID)
        self.assertIs(decision.verdict, Verdict.WARN)
        self.assertIs(decision.risk_level, RiskLevel.MODERATE)

    def test_clean_report_passes_and_needs_no_human(self):
        decision = build_decision([], run_id=RUN_ID)
        self.assertIs(decision.verdict, Verdict.PASS)
        self.assertIs(decision.risk_level, RiskLevel.LOW)
        self.assertFalse(decision.requires_human_review)
        self.assertTrue(decision.rationale, "pass 也必须写清为什么 pass")

    def test_unverified_research_never_passes(self):
        decision = build_decision([], run_id=RUN_ID, external_unverified=True)
        self.assertIs(decision.verdict, Verdict.WARN)

    def test_low_confidence_forces_human_review_without_changing_verdict(self):
        decision = build_decision([finding(1, confidence=0.6)], run_id=RUN_ID)
        self.assertIs(decision.verdict, Verdict.PASS)
        self.assertTrue(decision.requires_human_review)

    def test_rationale_falls_back_to_policy_reasons(self):
        decision = build_decision([finding(1, severity=Severity.MAJOR)],
                                  run_id=RUN_ID)
        self.assertIn("major", decision.rationale)
        self.assertEqual(decision.policy_id, "review-decision-v1")


class DecisionNodeTests(unittest.IsolatedAsyncioTestCase):
    """节点契约：阶段校验、结果形态、可复算自检。"""

    async def test_node_returns_decision_for_current_stage(self):
        state = decision_state([finding(1, severity=Severity.MAJOR)])
        node = DecisionEngineNode()
        result = await node.run(NodeContext(state=state))
        self.assertTrue(result.success)
        self.assertIsNotNone(result.decision)
        self.assertIs(result.decision.verdict, Verdict.WARN)
        self.assertEqual(result.decision.run_id, RUN_ID)
        # decision 是 typed 产物：不允许再往 output 里塞一份
        self.assertEqual(dict(result.output), {})

    async def test_node_reports_human_review_as_warning(self):
        state = decision_state([finding(1, severity=Severity.BLOCKER)])
        result = await DecisionEngineNode().run(NodeContext(state=state))
        self.assertTrue(result.warnings, "需人工复核必须显式提示，不能只藏在 Decision 里")

    async def test_wrong_stage_is_rejected(self):
        state = decision_state(stage=Stage.CROSS_REVIEW)
        result = await DecisionEngineNode().run(NodeContext(state=state))
        self.assertFalse(result.success)
        self.assertTrue(result.errors)
        self.assertFalse(result.retryable)

    async def test_research_flag_comes_from_config(self):
        state = decision_state([])
        result = await DecisionEngineNode().run(
            NodeContext(state=state, config={"external_unverified": True}))
        self.assertTrue(result.success)
        self.assertIs(result.decision.verdict, Verdict.WARN)


class AdvanceIntegrationTests(unittest.TestCase):
    """节点产物进入状态机：推进、归属校验、失败不推进。"""

    def _advance(self, state, result):
        return advance(state, Stage.DECISION, result)

    def test_decision_advances_state_to_finalize(self):
        state = decision_state([finding(1, severity=Severity.BLOCKER)])
        result = asyncio.run(DecisionEngineNode().run(NodeContext(state=state)))
        nxt = self._advance(state, result)
        self.assertIs(nxt.stage, Stage.FINALIZE)
        self.assertIn(Stage.DECISION, nxt.completed_stages)
        self.assertEqual(nxt.decision.run_id, RUN_ID)

    def test_foreign_decision_cannot_be_merged(self):
        state = decision_state([])
        foreign = build_decision([], run_id=f"{REVIEW_ID}-RUN02")
        with self.assertRaises(InvariantViolation):
            self._advance(state, NodeResult.ok(decision=foreign))

    def test_failed_result_never_advances(self):
        state = decision_state([])
        with self.assertRaises(InvalidTransition):
            self._advance(state, NodeResult.fail("模型不可用"))


def historical_reports():
    outputs = ROOT / "outputs"
    if not outputs.is_dir():
        return []
    import json
    out = []
    for path in sorted(outputs.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "review_id" in data and isinstance(
                data.get("verdict"), str):
            out.append(data)
    return out


@unittest.skipUnless(historical_reports(), "outputs/ 不存在（CI 无历史语料）")
class HistoricalReplayTests(unittest.TestCase):
    """双跑：拿历史报告的 findings 重放裁定，只允许已登记的偏差。"""

    def test_replay_reproduces_historical_verdicts(self):
        from review_agent.adapters.issue_codec import encode_issues

        unexpected = []
        for report in historical_reports():
            findings = [e.finding for e in encode_issues(
                report.get("issues") or [], run_id=RUN_ID)]
            research = report.get("research") or {}
            decision = build_decision(
                findings, run_id=RUN_ID,
                external_unverified=bool(research.get("unverified")
                                         or research.get("needs_confirm")))
            if decision.verdict.value == report["verdict"]:
                continue
            if report["review_id"] in KNOWN_VERDICT_DEVIATIONS:
                continue
            unexpected.append((report["review_id"], report["verdict"],
                               decision.verdict.value))
        self.assertEqual(unexpected, [], "出现未解释的历史裁定差异")

    def test_known_deviation_list_is_not_stale(self):
        """登记清单里的每一条都必须真的还能复现差异，否则说明清单该删了。"""
        from review_agent.adapters.issue_codec import encode_issues

        still_diff = set()
        for report in historical_reports():
            findings = [e.finding for e in encode_issues(
                report.get("issues") or [], run_id=RUN_ID)]
            research = report.get("research") or {}
            decision = build_decision(
                findings, run_id=RUN_ID,
                external_unverified=bool(research.get("unverified")
                                         or research.get("needs_confirm")))
            if decision.verdict.value != report["verdict"]:
                still_diff.add(report["review_id"])
        self.assertLessEqual(KNOWN_VERDICT_DEVIATIONS, still_diff,
                             "清单里有已不再偏差的报告，请同步更新")


if __name__ == "__main__":
    unittest.main()
