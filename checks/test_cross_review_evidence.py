#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_cross_review_evidence.py —— B4：Cross Review 读取证据。"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (  # noqa: E402
    MATCH_KINDS,
    STAGE_ORDER,
    DisputeKind,
    Evidence,
    EvidenceAssessment,
    EvidenceKind,
    EvidenceStrength,
    Finding,
    FindingOrigin,
    Location,
    MatchCheck,
    ReviewProfile,
    RuleRef,
    Severity,
    Stage,
)
from review_agent.runtime import NodeContext, RunState  # noqa: E402
from review_agent.runtime.nodes import CrossReviewerNode, review_findings  # noqa: E402

REVIEW_ID = "R-20261102-103"
RUN_ID = f"{REVIEW_ID}-RUN01"


def finding(seq=1, **over):
    base = dict(
        id=f"ISS-{seq:04d}", run_id=RUN_ID, dimension="fact",
        severity=Severity.MAJOR, quote="原文一句话",
        location=Location("第 1 句", 1), confidence=0.8,
        origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id="FACT-001", dimension="fact"),
    )
    base.update(over)
    return Finding(**base)


def assessment(*, passed=True):
    checks = tuple(
        MatchCheck(kind, True if passed else None) for kind in MATCH_KINDS)
    return EvidenceAssessment(checks=checks, assessed_by="evidence-assessor")


def evidence(seq=1, **over):
    base = dict(
        id=f"EV-{seq:04d}", run_id=RUN_ID, kind=EvidenceKind.QUOTE,
        quote="原文一句话", location=Location("第 1 句", 1),
        finding_ids=("ISS-0001",),
        strength=EvidenceStrength.UNVERIFIED,
    )
    base.update(over)
    return Evidence(**base)


class EvidenceDisputeTests(unittest.TestCase):
    def test_duplicate_finding_is_reported(self):
        result = review_findings(
            [finding(1), finding(2)], run_id=RUN_ID,
            evidence=(evidence(),))
        kinds = [d.kind for d in result.disputes]
        self.assertIn(DisputeKind.DUPLICATE_FINDING, kinds)

    def test_weak_major_is_insufficient(self):
        result = review_findings(
            [finding(1)], run_id=RUN_ID, evidence=(evidence(),))
        self.assertEqual([d.kind for d in result.disputes],
                         [DisputeKind.INSUFFICIENT_EVIDENCE])

    def test_blocker_requires_verified(self):
        ev = evidence(strength=EvidenceStrength.PARTIAL,
                      assessment=assessment(passed=False))
        result = review_findings(
            [finding(1, severity=Severity.BLOCKER)], run_id=RUN_ID,
            evidence=(ev,))
        self.assertEqual([d.kind for d in result.disputes],
                         [DisputeKind.INSUFFICIENT_EVIDENCE])

    def test_major_accepts_partial(self):
        ev = evidence(strength=EvidenceStrength.PARTIAL,
                      assessment=assessment(passed=False))
        result = review_findings(
            [finding(1, severity=Severity.MAJOR)], run_id=RUN_ID,
            evidence=(ev,))
        self.assertEqual(result.disputes, ())

    def test_verified_blocker_is_not_flagged(self):
        ev = evidence(strength=EvidenceStrength.VERIFIED,
                      assessment=assessment())
        result = review_findings(
            [finding(1, severity=Severity.BLOCKER)], run_id=RUN_ID,
            evidence=(ev,))
        self.assertEqual(result.disputes, ())

    def test_without_evidence_keeps_legacy_behavior(self):
        result = review_findings([finding(1)], run_id=RUN_ID)
        self.assertEqual(result.disputes, ())

    def test_node_passes_evidence_to_cross_review(self):
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.CROSS_REVIEW,
            profile=ReviewProfile(id="p", name="p", version="1"),
            completed_stages=STAGE_ORDER[:5],
            findings=(finding(1),), evidence=(evidence(),),
        )
        result = asyncio.run(
            CrossReviewerNode().run(NodeContext(state=state)))
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.disputes[0].kind,
                         DisputeKind.INSUFFICIENT_EVIDENCE)


if __name__ == "__main__":
    unittest.main()
