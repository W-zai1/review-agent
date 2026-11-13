#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_evidence_contradiction.py —— 反证在真实链路里可达（决策书 4.4 / A）。

《审查意见》的原始例子：材料写「2025 年营收 100 亿」，quote 在原文里逐字命中，
外部来源却说 80 亿。旧链路里这条引用证据直接是 VERIFIED，`CONTRADICTED`
只在序列化层出现过——读者看到的结论是「已核验」，与事实相反。

现在 `EvidenceAssessorNode` 评估前先跑 `link_contradictions()`：同一条发现身上
既有 `EXTERNAL_SOURCE` 证据（全仓只有 research 节点产，且只在主张被反驳时产）
又有 `QUOTE` 证据时，把原文引用那条标成反证，并用 `contradicted_by` 指名
外部证据。此处验收：
    1. 纯函数：只连该连的，别的证据一个字段都不动；
    2. 节点：反证进入 revisions，落盘状态里是 CONTRADICTED；
    3. 未被反驳的引用仍然照旧 VERIFIED（不误伤）。
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from review_agent.domain import (                       # noqa: E402
    STAGE_ORDER,
    Evidence,
    EvidenceKind,
    EvidenceStrength,
    Finding,
    FindingOrigin,
    RuleRef,
    Severity,
    Location,
    ReviewProfile,
    SourceRef,
    Stage,
    Verification,
)
from review_agent.runtime import RunState, advance        # noqa: E402
from review_agent.runtime.node import NodeContext        # noqa: E402
from review_agent.runtime.nodes.evidence_assess import (  # noqa: E402
    EvidenceAssessorNode,
    link_contradictions,
)

REVIEW_ID = "R-20261112-900"
RUN_ID = f"{REVIEW_ID}-RUN01"
TEXT = "本公司 2025 年营业收入 100 亿元。"


def quote(seq=1, finding_ids=("ISS-0001",), **over):
    base = dict(
        id=f"EV-{seq:04d}", run_id=RUN_ID, kind=EvidenceKind.QUOTE,
        quote=TEXT, location=Location("第 1 句", 1, 0, len(TEXT)),
        verification=Verification.ANCHORED,
        verified_by="review.s4.evidence_anchor",
        finding_ids=finding_ids,
    )
    base.update(over)
    return Evidence(**base)


def external(seq=2, finding_ids=("ISS-0001",), **over):
    base = dict(
        id=f"EV-{seq:04d}", run_id=RUN_ID, kind=EvidenceKind.EXTERNAL_SOURCE,
        quote="据财报，该公司 2025 年营业收入为 80 亿元。",
        source_ref="https://www.example.com/report",
        source=SourceRef(url="https://www.example.com/report",
                         domain="www.example.com"),
        verification=Verification.NOT_CHECKED,
        verified_by="research",
        finding_ids=finding_ids,
    )
    base.update(over)
    return Evidence(**base)


def finding(seq=1, **over):
    base = dict(
        id=f"ISS-{seq:04d}", run_id=RUN_ID, dimension="fact",
        severity=Severity.MAJOR, quote=TEXT, location=Location("第 1 句", 1),
        confidence=0.8, origin=FindingOrigin.RESEARCH,
        rule=RuleRef(rule_id="FACT-001", dimension="fact"),
    )
    base.update(over)
    return Finding(**base)


def state(evidence, findings=(finding(),)):
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.EVIDENCE,
        profile=ReviewProfile(id="doc-review", name="审查", version="1"),
        completed_stages=STAGE_ORDER[:4],
        findings=tuple(findings), evidence=tuple(evidence),
        artifacts={"target_text": TEXT, "evidence_dropped": []},
    )


class LinkContradictionsTests(unittest.TestCase):
    """纯函数：只按「同一条发现」连反证，别的一律不动。"""

    def test_refuted_quote_points_at_the_external_evidence(self):
        linked = link_contradictions([quote(), external()])
        self.assertEqual(linked[0].contradicted_by, ("EV-0002",))
        self.assertEqual(linked[1].contradicted_by, ())

    def test_quote_without_external_source_is_untouched(self):
        linked = link_contradictions([quote()])
        self.assertEqual(linked[0].contradicted_by, ())
        self.assertIs(linked[0].kind, EvidenceKind.QUOTE)

    def test_only_the_refuted_finding_is_linked(self):
        linked = link_contradictions([
            quote(1, ("ISS-0001",)), external(2, ("ISS-0001",)),
            quote(3, ("ISS-0002",)),
        ])
        self.assertEqual(linked[0].contradicted_by, ("EV-0002",))
        self.assertEqual(linked[2].contradicted_by, ())

    def test_external_evidence_not_attached_to_a_finding_links_nothing(self):
        linked = link_contradictions([quote(), external(finding_ids=())])
        self.assertEqual(linked[0].contradicted_by, ())

    def test_existing_contradiction_is_not_overwritten(self):
        linked = link_contradictions([
            quote(contradicted_by=("EV-0009",)), external(),
        ])
        self.assertEqual(linked[0].contradicted_by, ("EV-0009",))

    def test_multiple_sources_are_numbered_in_evidence_order(self):
        linked = link_contradictions([
            quote(), external(2), external(3),
        ])
        self.assertEqual(linked[0].contradicted_by, ("EV-0002", "EV-0003"))

    def test_same_input_gives_same_output(self):
        rows = [quote(), external()]
        self.assertEqual(link_contradictions(rows), link_contradictions(rows))


class AssessorNodeTests(unittest.TestCase):
    """节点：反证要进 revisions、要落盘，且不误伤未被反驳的引用。"""

    def _run(self, *evidence):
        return asyncio.run(EvidenceAssessorNode().run(
            NodeContext(state=state(evidence))))

    def test_refuted_quote_becomes_contradicted(self):
        result = self._run(quote(), external())
        self.assertTrue(result.success, result.errors)
        stored = {item.id: item for item in result.evidence_revisions}
        self.assertIs(stored["EV-0001"].strength,
                      EvidenceStrength.CONTRADICTED)
        self.assertEqual(stored["EV-0001"].contradicted_by, ("EV-0002",))
        # 外部证据本身照旧按判定表评：有来源、无四项匹配 → PARTIAL
        self.assertIs(stored["EV-0002"].strength, EvidenceStrength.PARTIAL)

    def test_contradicted_revision_lands_in_state(self):
        nxt = advance(state([quote(), external()]), Stage.EVIDENCE,
                      self._run(quote(), external()))
        stored = {item.id: item for item in nxt.evidence}
        self.assertIs(stored["EV-0001"].strength,
                      EvidenceStrength.CONTRADICTED)
        self.assertEqual(stored["EV-0001"].verification,
                         Verification.ANCHORED)

    def test_unrefuted_quote_stays_verified(self):
        result = self._run(quote())
        self.assertTrue(result.success, result.errors)
        self.assertEqual(len(result.evidence_revisions), 1)
        self.assertIs(result.evidence_revisions[0].strength,
                      EvidenceStrength.VERIFIED)

    def test_two_findings_one_refuted_one_verified(self):
        result = self._run(
            quote(1, ("ISS-0001",)), external(2, ("ISS-0001",)),
            quote(3, ("ISS-0002",)),
        )
        self.assertTrue(result.success, result.errors)
        stored = {item.id: item for item in result.evidence_revisions}
        self.assertIs(stored["EV-0001"].strength,
                      EvidenceStrength.CONTRADICTED)
        self.assertIs(stored["EV-0003"].strength, EvidenceStrength.VERIFIED)


if __name__ == "__main__":
    unittest.main()
