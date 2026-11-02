#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_evidence_graph.py —— B5 Evidence Graph 投影。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.application import build_graph, build_shadow_report, render_shadow_md  # noqa: E402
from review_agent.domain import (  # noqa: E402
    MATCH_KINDS,
    STAGE_ORDER,
    Decision,
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
    SourceRef,
    Stage,
    Verdict,
    Verification,
)
from review_agent.runtime import RunState  # noqa: E402

REVIEW_ID = "R-20261102-104"
RUN_ID = f"{REVIEW_ID}-RUN01"


def _assessment():
    return EvidenceAssessment(
        checks=tuple(MatchCheck(k, True) for k in MATCH_KINDS),
        assessed_by="evidence-assessor")


def _state():
    finding = Finding(
        id="ISS-0001", run_id=RUN_ID, dimension="compliance",
        severity=Severity.MAJOR, quote="违约方应支付违约金。",
        location=Location("第 1 句", 1), confidence=0.8,
        origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id="COMP-001", dimension="compliance"),
        evidence_ids=("EV-0001", "EV-0002"),
    )
    quote = Evidence(
        id="EV-0001", run_id=RUN_ID, kind=EvidenceKind.QUOTE,
        quote="违约方应支付违约金。", location=Location("第 1 句", 1),
        verification=Verification.ANCHORED,
        verified_by="review.s4.evidence_anchor",
        finding_ids=("ISS-0001",),
        strength=EvidenceStrength.VERIFIED, assessment=_assessment(),
    )
    external = Evidence(
        id="EV-0002", run_id=RUN_ID, kind=EvidenceKind.EXTERNAL_SOURCE,
        quote="来源片段", source_ref="https://www.gov.cn/a",
        source=SourceRef(url="https://www.gov.cn/a", domain="www.gov.cn",
                         official=True, snapshot_hash="b" * 64),
        finding_ids=("ISS-0001",),
        strength=EvidenceStrength.PARTIAL, assessment=_assessment(),
    )
    decision = Decision(
        id="DEC-0001", run_id=RUN_ID, verdict=Verdict.WARN,
        rationale="1 条 major", finding_ids=("ISS-0001",),
    )
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.FINALIZE,
        profile=ReviewProfile(id="p", name="p", version="1"),
        completed_stages=STAGE_ORDER,
        findings=(finding,), evidence=(quote, external), decision=decision,
        artifacts={"target_text": "违约方应支付违约金。"},
    )


class GraphTests(unittest.TestCase):
    def test_answers_cover_six_questions(self):
        graph = build_graph(_state())
        answer = graph["answers"]["ISS-0001"]
        self.assertEqual(answer["rule"], "COMP-001")
        self.assertEqual(answer["materials"], ["EV-0001", "EV-0002"])
        self.assertEqual(answer["external"], ["EV-0002"])
        self.assertEqual(answer["produced_by"], "rule-analyzer")
        self.assertEqual(answer["reviewed_by"], "cross-reviewer")
        self.assertEqual(answer["verdict"], "warn")

    def test_edges_connect_findings_evidence_and_decision(self):
        edges = build_graph(_state())["edges"]
        relations = {(e["from"], e["to"], e["rel"]) for e in edges}
        self.assertIn(("ISS-0001", "RULE-COMP-001", "derived_from"), relations)
        self.assertIn(("ISS-0001", "EV-0001", "supported_by"), relations)
        self.assertIn(("DEC-0001", "ISS-0001", "decided_on"), relations)

    def test_report_contains_graph_and_markdown_section(self):
        report = build_shadow_report(_state())
        self.assertIn("graph", report)
        self.assertIn("证据关系", render_shadow_md(report))


if __name__ == "__main__":
    unittest.main()
