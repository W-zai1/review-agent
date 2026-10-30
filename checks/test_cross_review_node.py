#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_cross_review_node.py —— Cross Reviewer 节点的独立验收。

交叉复核是「不让互相矛盾的结论蒙混过关」的那道门，它做两件事：
    1. 同片段跨维度冲突 → `Dispute`（交人工裁定，不自动取舍）
    2. 冲突双方各降 0.05 置信度（下限 0.5）——通过 `revisions` 显式修订

第二件事是这次接管最容易出偏差的地方：旧实现在 S5 里直接改 dict，
同一对发现反复命中时会连续降；新实现必须逐字复现这个口径，否则
`to_confirm`（置信度 < 0.7）的名单会变、报告结论会跟着动。

三层验收：
    1. 规则：相似度阈值、严重度门槛、降权步长与下限、重复命中
    2. 运行时：revisions 按 id 生效、顺序不变、悬空/重复修订被拒
    3. 历史差分：207 份报告，节点结果与旧实现逐字段一致

历史语料只在本地有（outputs/ 被 .gitignore 排除）；CI 跑前两层。
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (                       # noqa: E402
    STAGE_ORDER,
    DisputeStatus,
    Finding,
    FindingOrigin,
    InvariantViolation,
    Location,
    ReviewProfile,
    RuleRef,
    Severity,
    Stage,
)
from review_agent.runtime import NodeContext, NodeResult, RunState, advance  # noqa: E402
from review_agent.runtime.nodes import (                # noqa: E402
    CrossReviewerNode,
    dispute_to_report_dict,
    review_findings,
)
from review_agent.runtime.nodes.cross_review import CROSS_PAIRS  # noqa: E402

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"
SENTENCE = "截止目前，项目已顺利完成 20 个里程碑节点，剩余 3 个待验收。"


def finding(seq, dimension, quote=SENTENCE, *, severity=Severity.MAJOR,
            confidence=0.8, rule_id="COMP-004", run_id=RUN_ID):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=run_id, dimension=dimension,
        severity=severity, quote=quote, location=Location("第 1 句", 1),
        confidence=confidence, origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id=rule_id, dimension=dimension, text="规则原文"),
    )


class CrossRuleTests(unittest.TestCase):
    def test_conflict_between_configured_pairs(self):
        result = review_findings(
            [finding(1, "fact", severity=Severity.MAJOR),
             finding(2, "safety", severity=Severity.MINOR)], run_id=RUN_ID)
        self.assertEqual(len(result.disputes), 1)
        dispute = result.disputes[0]
        self.assertEqual(dispute.issue_ids, ("ISS-0001", "ISS-0002"))
        self.assertEqual(dispute.between, ("fact-reviewer", "safety-reviewer"))
        self.assertIs(dispute.status, DisputeStatus.OPEN)

    def test_both_minor_is_not_a_redline_conflict(self):
        result = review_findings(
            [finding(1, "fact", severity=Severity.MINOR),
             finding(2, "safety", severity=Severity.NIT)], run_id=RUN_ID)
        self.assertEqual(result.disputes, ())
        self.assertEqual(result.revisions, ())

    def test_unconfigured_dimension_pair_is_ignored(self):
        result = review_findings(
            [finding(1, "fact", severity=Severity.BLOCKER),
             finding(2, "compliance", severity=Severity.BLOCKER)], run_id=RUN_ID)
        self.assertEqual(result.disputes, ())

    def test_different_quotes_do_not_conflict(self):
        result = review_findings(
            [finding(1, "fact", quote=SENTENCE, severity=Severity.MAJOR),
             finding(2, "safety", quote="完全无关的另一句话：预算超支 30%",
                     severity=Severity.MAJOR)], run_id=RUN_ID)
        self.assertEqual(result.disputes, ())

    def test_conflict_lowers_both_confidences(self):
        result = review_findings(
            [finding(1, "compliance", confidence=0.8),
             finding(2, "style", confidence=0.9, severity=Severity.MAJOR)],
            run_id=RUN_ID)
        self.assertEqual([f.confidence for f in result.revisions], [0.75, 0.85])

    def test_confidence_floor_is_half(self):
        # 0.5 已经是下限，降无可降 → 不产生修订（revision 只记真正变了的值）
        result = review_findings(
            [finding(1, "compliance", confidence=0.5),
             finding(2, "style", confidence=0.52, severity=Severity.MAJOR)],
            run_id=RUN_ID)
        self.assertEqual([f.confidence for f in result.revisions], [0.5])

    def test_repeated_hits_decrement_again_from_current_value(self):
        # 一条合规发现同时对上两条风格发现：双方各自按「当前值」继续降
        result = review_findings(
            [finding(1, "compliance", confidence=0.9),
             finding(2, "style", confidence=0.9, severity=Severity.MAJOR),
             finding(3, "style", confidence=0.9, severity=Severity.MAJOR)],
            run_id=RUN_ID)
        by_id = {f.id: f.confidence for f in result.revisions}
        self.assertEqual(by_id["ISS-0001"], 0.8)     # 降了两次
        self.assertEqual(by_id["ISS-0002"], 0.85)
        self.assertEqual(by_id["ISS-0003"], 0.85)

    def test_only_changed_findings_are_revised(self):
        result = review_findings(
            [finding(1, "compliance", confidence=0.8),
             finding(2, "style", severity=Severity.MAJOR),
             finding(3, "fact", quote="另一句无关内容", confidence=0.8)],
            run_id=RUN_ID)
        self.assertEqual({f.id for f in result.revisions}, {"ISS-0001", "ISS-0002"})

    def test_dispute_point_names_both_sides(self):
        result = review_findings(
            [finding(1, "compliance", severity=Severity.MAJOR),
             finding(2, "style", severity=Severity.MINOR, rule_id="STYLE-002")],
            run_id=RUN_ID)
        point = result.disputes[0].point
        self.assertIn("ISS-0001", point)
        self.assertIn("ISS-0002", point)
        self.assertIn(CROSS_PAIRS[1][2], point)

    def test_report_dict_keeps_legacy_key_order(self):
        result = review_findings(
            [finding(1, "compliance"), finding(2, "style")], run_id=RUN_ID)
        self.assertEqual(
            list(dispute_to_report_dict(result.disputes[0])),
            ["between", "point", "resolution_hint", "issue_ids"])


def cross_state(findings, *, stage=Stage.CROSS_REVIEW):
    done = STAGE_ORDER[:STAGE_ORDER.index(stage)]
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=stage,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
        completed_stages=done, findings=tuple(findings),
    )


class CrossNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_node_returns_disputes_and_revisions(self):
        state = cross_state([finding(1, "compliance"), finding(2, "style")])
        result = await CrossReviewerNode().run(NodeContext(state=state))
        self.assertTrue(result.success)
        self.assertEqual(len(result.disputes), 1)
        self.assertEqual(len(result.revisions), 2)
        self.assertEqual(dict(result.output), {})     # disputes 是 typed 产物
        self.assertTrue(result.warnings)

    async def test_wrong_stage_is_rejected(self):
        state = cross_state([], stage=Stage.EVIDENCE)
        result = await CrossReviewerNode().run(NodeContext(state=state))
        self.assertFalse(result.success)

    async def test_no_conflict_produces_no_warning(self):
        state = cross_state([finding(1, "compliance")])
        result = await CrossReviewerNode().run(NodeContext(state=state))
        self.assertTrue(result.success)
        self.assertEqual(result.warnings, ())


class AdvanceRevisionTests(unittest.TestCase):
    def _advance(self, state, result):
        return advance(state, Stage.CROSS_REVIEW, result)

    def test_revision_replaces_in_place_and_keeps_order(self):
        state = cross_state([finding(1, "compliance", confidence=0.9),
                             finding(2, "style", confidence=0.9)])
        result = asyncio.run(
            CrossReviewerNode().run(NodeContext(state=state)))
        nxt = self._advance(state, result)
        self.assertEqual([f.id for f in nxt.findings], ["ISS-0001", "ISS-0002"])
        self.assertEqual([f.confidence for f in nxt.findings], [0.85, 0.85])
        self.assertEqual(len(nxt.disputes), 1)
        self.assertIs(nxt.stage, Stage.DECISION)

    def test_unknown_revision_is_rejected(self):
        state = cross_state([finding(1, "compliance")])
        bogus = finding(9, "compliance")
        with self.assertRaises(InvariantViolation):
            self._advance(state, NodeResult.ok(revisions=(bogus,)))

    def test_revision_of_new_finding_in_same_stage_is_rejected(self):
        state = cross_state([])
        fresh = finding(1, "fact")
        with self.assertRaises(InvariantViolation):
            self._advance(state, NodeResult.ok(findings=(fresh,),
                                               revisions=(fresh,)))


def historical_reports():
    outputs = ROOT / "outputs"
    if not outputs.is_dir():
        return []
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
class HistoricalDifferentialTests(unittest.TestCase):
    """差分：同一批 issue，节点与旧实现必须给出同样的争议与同样的置信度。"""

    def test_node_matches_legacy_cross_review(self):
        import review as legacy_review
        from review_agent.adapters.issue_codec import encode_issues

        checked = 0
        for report in historical_reports():
            issues = report.get("issues") or []
            if not issues:
                continue
            session = legacy_review.Session(report["review_id"], "t", "text")
            session.issues = [dict(i) for i in issues]
            legacy_review.cross_review(session)

            findings = [e.finding for e in encode_issues(issues, run_id=RUN_ID)]
            result = review_findings(findings, run_id=RUN_ID)
            # 节点不就地改发现（不可变）：最终置信度 = 修订后的值
            revised = {f.id: f.confidence for f in result.revisions}

            self.assertEqual(
                [dispute_to_report_dict(d) for d in result.disputes],
                session.disputes,
                f"{report['review_id']}：争议回写形状不一致")
            self.assertEqual(
                [revised.get(f.id, f.confidence) for f in findings],
                [i["confidence"] for i in session.issues],
                f"{report['review_id']}：降权后的置信度不一致")
            checked += 1
        self.assertGreater(checked, 0, "历史语料里没有带 issue 的报告")


if __name__ == "__main__":
    unittest.main()
