#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_evidence_assessor.py —— B2 证据评估节点。"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (  # noqa: E402
    STAGE_ORDER,
    Evidence,
    EvidenceKind,
    EvidenceStrength,
    Location,
    ReviewProfile,
    SourceRef,
    Stage,
    Verification,
)
from review_agent.runtime import NodeContext, RunState, advance  # noqa: E402
from review_agent.runtime.nodes import EvidenceAssessorNode, assess_evidence  # noqa: E402

REVIEW_ID = "R-20261102-101"
RUN_ID = f"{REVIEW_ID}-RUN01"
PROFILE = ReviewProfile(id="p", name="p", version="1")


def quote(**over):
    base = dict(
        id="EV-0001", run_id=RUN_ID, kind=EvidenceKind.QUOTE,
        quote="原文一句话", location=Location("第 1 句", 1, 0, 5),
        verification=Verification.ANCHORED,
        verified_by="review.s4.evidence_anchor",
    )
    base.update(over)
    return Evidence(**base)


def external(**over):
    base = dict(
        id="EV-0002", run_id=RUN_ID, kind=EvidenceKind.EXTERNAL_SOURCE,
        quote="来源页面片段", source_ref="https://www.gov.cn/a",
        source=SourceRef(url="https://www.gov.cn/a", domain="www.gov.cn",
                         official=True, snapshot_hash="b" * 64),
    )
    base.update(over)
    return Evidence(**base)


class AssessRuleTests(unittest.TestCase):
    def test_quote_with_position_is_verified(self):
        result = assess_evidence(quote())
        self.assertIs(result.strength, EvidenceStrength.VERIFIED)
        self.assertTrue(result.assessment.all_passed)

    def test_quote_without_position_is_partial(self):
        result = assess_evidence(quote(location=Location("第 1 句", 1)))
        self.assertIs(result.strength, EvidenceStrength.PARTIAL)
        self.assertFalse(result.assessment.all_passed)

    def test_placeholder_is_unverified(self):
        result = assess_evidence(quote(verification=Verification.PLACEHOLDER))
        self.assertIs(result.strength, EvidenceStrength.UNVERIFIED)

    def test_external_source_is_partial_not_fake_verified(self):
        result = assess_evidence(external())
        self.assertIs(result.strength, EvidenceStrength.PARTIAL)
        self.assertFalse(result.assessment.all_passed)

    def test_contradicted_is_preserved(self):
        result = assess_evidence(quote(contradicted_by=("EV-0002",)))
        self.assertIs(result.strength, EvidenceStrength.CONTRADICTED)

    def test_idempotent(self):
        first = assess_evidence(quote())
        self.assertEqual(assess_evidence(first), first)


class NodeTests(unittest.TestCase):
    def test_node_returns_revisions_and_advance_replaces_in_place(self):
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.EVIDENCE,
            profile=PROFILE, completed_stages=STAGE_ORDER[:4],
            evidence=(quote(), external()),
        )
        result = asyncio.run(
            EvidenceAssessorNode().run(NodeContext(state=state)))
        self.assertTrue(result.success, result.errors)
        self.assertEqual(len(result.evidence_revisions), 2)
        self.assertEqual([e.id for e in result.evidence_revisions],
                         ["EV-0001", "EV-0002"])

        nxt = advance(state, Stage.EVIDENCE, result, required=frozenset())
        self.assertEqual([e.strength for e in nxt.evidence],
                         [EvidenceStrength.VERIFIED, EvidenceStrength.PARTIAL])


if __name__ == "__main__":
    unittest.main()
