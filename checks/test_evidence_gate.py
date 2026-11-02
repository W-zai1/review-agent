#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_evidence_gate.py —— B3 证据闸门。"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (  # noqa: E402
    STAGE_ORDER,
    MATCH_KINDS,
    DecisionPolicy,
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
    Verdict,
    evidence_gate,
)
from review_agent.runtime import NodeContext, RunState  # noqa: E402
from review_agent.runtime.nodes import DecisionEngineNode  # noqa: E402

REVIEW_ID = "R-20261102-102"
RUN_ID = f"{REVIEW_ID}-RUN01"
PROFILE = ReviewProfile(id="p", name="p", version="1")


def finding(seq=1, **over):
    base = dict(
        id=f"ISS-{seq:04d}", run_id=RUN_ID, dimension="fact",
        severity=Severity.MAJOR, quote="原文一句话",
        statement="规则判定说明",
        location=Location("第 1 句", 1), confidence=0.8,
        origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id="FACT-001", dimension="fact"),
    )
    base.update(over)
    return Finding(**base)


def all_pass():
    return EvidenceAssessment(
        checks=tuple(MatchCheck(kind, True) for kind in MATCH_KINDS),
        assessed_by="evidence-assessor",
    )


def partial():
    return EvidenceAssessment(
        checks=(
            MatchCheck("source_identity", True),
            MatchCheck("subject", None),
            MatchCheck("metric", None),
            MatchCheck("period", None),
        ),
        assessed_by="evidence-assessor",
    )


def evidence(seq=1, **over):
    base = dict(
        id=f"EV-{seq:04d}", run_id=RUN_ID, kind=EvidenceKind.QUOTE,
        quote="原文一句话", location=Location("第 1 句", 1),
        finding_ids=(f"ISS-{seq:04d}",),
        strength=EvidenceStrength.VERIFIED,
        assessment=all_pass(),
    )
    base.update(over)
    return Evidence(**base)


class GateRuleTests(unittest.TestCase):
    def test_disabled_is_noop(self):
        gate = evidence_gate([finding()], [evidence()], enabled=False)
        self.assertFalse(gate.enabled)
        self.assertIs(gate.cap, Verdict.PASS)
        self.assertEqual(gate.capped, ())

    def test_weak_major_caps_warn(self):
        gate = evidence_gate(
            [finding()], [evidence(strength=EvidenceStrength.UNVERIFIED)],
            enabled=True)
        self.assertIs(gate.cap, Verdict.WARN)
        self.assertEqual(gate.capped, ("ISS-0001",))
        self.assertTrue(gate.needs_human)

    def test_partial_caps_warn(self):
        gate = evidence_gate(
            [finding()],
            [evidence(strength=EvidenceStrength.PARTIAL, assessment=partial())],
            enabled=True)
        self.assertIs(gate.cap, Verdict.WARN)
        self.assertIn("ISS-0001", gate.capped)

    def test_verified_is_not_capped(self):
        gate = evidence_gate([finding()], [evidence()], enabled=True)
        self.assertIs(gate.cap, Verdict.PASS)
        self.assertEqual(gate.capped, ())

    def test_contradicted_is_flagged(self):
        gate = evidence_gate(
            [finding()],
            [evidence(strength=EvidenceStrength.CONTRADICTED,
                      contradicted_by=("EV-0002",))],
            enabled=True)
        self.assertIs(gate.cap, Verdict.WARN)
        self.assertEqual(gate.contradicted, ("ISS-0001",))
        self.assertTrue(gate.needs_human)

    def test_missing_clause_without_evidence_is_exempt(self):
        gate = evidence_gate(
            [finding(quote="", statement="原文缺失条款")], [], enabled=True)
        self.assertIs(gate.cap, Verdict.PASS)
        self.assertEqual(gate.capped, ())


class DecisionIntegrationTests(unittest.TestCase):
    def test_gate_caps_decision_and_adds_dispute(self):
        f = finding()
        ev = evidence(
            strength=EvidenceStrength.CONTRADICTED,
            contradicted_by=("EV-0002",))
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.DECISION,
            profile=PROFILE, completed_stages=STAGE_ORDER[:6],
            findings=(f,), evidence=(ev,),
        )
        result = asyncio.run(
            DecisionEngineNode(
                policy=DecisionPolicy(
                    evidence_gate_enabled=True, version="2"))
            .run(NodeContext(state=state)))
        self.assertTrue(result.success, result.errors)
        self.assertIs(result.decision.verdict, Verdict.WARN)
        self.assertTrue(result.decision.requires_human_review)
        self.assertEqual(len(result.disputes), 1)
        self.assertIs(result.disputes[0].kind, DisputeKind.CONTRADICTION)
        self.assertTrue(any("证据" in r for r in result.decision.reasons))


if __name__ == "__main__":
    unittest.main()
