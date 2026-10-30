#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_semantic_ingest.py —— 语义审候选入库：去重 / 豁免 / 降级 / 编号。

模型给出的是候选，不是结论。从候选到 `Finding` 之间有四道判断题，全部可测：

    1. **重复吗**：同规则 + 引用重合 ≥0.8，或首句指纹相同 → 丢弃（不刷屏）
    2. **被规则层豁免了吗**：确定性判据优先，模型按字面再报 → 进 excluded，不静默丢
    3. **定级对吗**：blocker/major 且置信度 <0.7 → 降一级并记原因
    4. **编号接得上吗**：从调用方给的起始序号起，Run 内不撞号

外加执行器适配层：模型调用通过 `review_fn` 注入，离线可测（不联网、不出随机结果）。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.capabilities import (                 # noqa: E402
    Candidate,
    IngestContext,
    ingest_candidates,
)
from review_agent.domain import (                       # noqa: E402
    Finding,
    FindingOrigin,
    FindingStatus,
    Location,
    RuleRef,
    Severity,
)

RUN_ID = "R-20261030-001-RUN01"
TEXT = "第一句无关。截止目前，项目已完成 20 个里程碑节点。最后一句。"
QUOTE = "截止目前，项目已完成 20 个里程碑节点"


def existing(rule_id="COMP-004", quote=QUOTE, seq=1):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=RUN_ID, dimension="compliance",
        severity=Severity.MINOR, quote=quote, location=Location("第 1 句", 1),
        confidence=0.8, origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id=rule_id, dimension="compliance"),
    )


def context(*, existing_findings=(), exemptions=()):
    return IngestContext(
        run_id=RUN_ID, text=TEXT, dimension="compliance",
        existing=tuple(existing_findings), exemptions=tuple(exemptions),
        tool="llm_api(fake)",
    )


def candidate(**overrides):
    data = {"rule_id": "COMP-004", "quote": QUOTE, "evidence": "缺口径",
            "confidence": 0.8, "suggestion": "补口径", "severity": "minor"}
    data.update(overrides)
    return Candidate(**data)


class DedupeTests(unittest.TestCase):
    def test_overlapping_quote_with_same_rule_is_duplicate(self):
        result = ingest_candidates([candidate(quote=QUOTE + "，剩余 3 个待验收")],
                                   context(existing_findings=[existing()]),
                                   start_seq=2)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.duplicates, 1)

    def test_same_first_sentence_is_duplicate(self):
        result = ingest_candidates(
            [candidate(quote=QUOTE[:12])],
            context(existing_findings=[existing(quote=QUOTE)]),
            start_seq=2)
        self.assertEqual(result.duplicates, 1)

    def test_other_rule_is_not_duplicate(self):
        result = ingest_candidates([candidate(rule_id="FACT-001")],
                                   context(existing_findings=[existing()]),
                                   start_seq=2)
        self.assertEqual(len(result.findings), 1)

    def test_duplicates_against_earlier_candidates_in_same_batch(self):
        result = ingest_candidates(
            [candidate(quote=QUOTE), candidate(quote=QUOTE + "，另见")],
            context(), start_seq=1)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.duplicates, 1)


class ExemptionTests(unittest.TestCase):
    def test_exempted_candidate_is_recorded_not_dropped_silently(self):
        result = ingest_candidates(
            [candidate()],
            context(exemptions=(("COMP-004", QUOTE, "内部进度不算披露"),)),
            start_seq=1)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.exempted, 1)
        self.assertEqual(len(result.excluded), 1)
        self.assertIn("内部进度不算披露", result.excluded[0]["reason"])

    def test_exemption_of_another_rule_does_not_apply(self):
        result = ingest_candidates(
            [candidate(rule_id="FACT-001")],
            context(exemptions=(("COMP-004", QUOTE, "原因"),)),
            start_seq=1)
        self.assertEqual(len(result.findings), 1)


class SeverityTests(unittest.TestCase):
    def test_blocker_with_low_confidence_is_downgraded(self):
        result = ingest_candidates([candidate(severity="blocker", confidence=0.6)],
                                   context(), start_seq=1)
        finding = result.findings[0]
        self.assertIs(finding.severity, Severity.MAJOR)
        self.assertIn("下调", finding.severity_note or "")

    def test_major_with_low_confidence_is_downgraded(self):
        result = ingest_candidates([candidate(severity="major", confidence=0.6)],
                                   context(), start_seq=1)
        self.assertIs(result.findings[0].severity, Severity.MINOR)

    def test_high_confidence_keeps_severity(self):
        result = ingest_candidates([candidate(severity="major", confidence=0.9)],
                                   context(), start_seq=1)
        self.assertIs(result.findings[0].severity, Severity.MAJOR)
        self.assertIsNone(result.findings[0].severity_note)

    def test_unknown_severity_is_rejected(self):
        with self.assertRaises(ValueError):
            ingest_candidates([candidate(severity="critical")], context(), start_seq=1)


class FindingShapeTests(unittest.TestCase):
    def test_ids_follow_start_sequence(self):
        result = ingest_candidates(
            [candidate(quote=QUOTE), candidate(quote="最后一句", rule_id="STYLE-002")],
            context(), start_seq=7)
        self.assertEqual([f.id for f in result.findings], ["ISS-0007", "ISS-0008"])

    def test_location_and_status_come_from_text_and_confidence(self):
        result = ingest_candidates([candidate(confidence=0.6)], context(), start_seq=1)
        finding = result.findings[0]
        self.assertEqual(finding.location.sentence, 2)
        self.assertEqual(finding.location.label, "第 2 句")
        self.assertIs(finding.status, FindingStatus.NEEDS_CONFIRMATION)

    def test_open_discovery_origin(self):
        result = ingest_candidates([candidate(rule_id="OPEN-CONFLICT",
                                              open_discovery=True)],
                                   context(), start_seq=1)
        self.assertIs(result.findings[0].origin, FindingOrigin.OPEN_DISCOVERY)

    def test_model_origin_and_tool_label(self):
        result = ingest_candidates([candidate()], context(), start_seq=1)
        self.assertIs(result.findings[0].origin, FindingOrigin.MODEL)
        self.assertEqual(result.findings[0].tool, "llm_api(fake)")


class ExecutorAdapterTests(unittest.TestCase):
    """`make_semantic_executor`：模型调用注入，候选入库复用同一套规则。"""

    def setUp(self):
        import review
        from review_agent.runtime.nodes import SemanticStep
        self.review = review
        self.step = SemanticStep(dimension="compliance", chunk_index=1,
                                 start=0, end=len(TEXT), text=TEXT)
        session = review.Session("R-20261030-001", "targets/a.md", "contract")
        self.session = session

    def review_fn(self, issues):
        def fn(step):
            return {"issues": issues, "dropped": ["x"], "retracted": [],
                    "cached": True, "usage": {"input": 3, "output": 4},
                    "model": "fake", "truncated": False,
                    "output_truncated": False, "finish_reason": "stop"}
        return fn

    def test_executor_returns_findings_and_metadata(self):
        executor = self.review.make_semantic_executor(
            session=self.session, text=TEXT, run_id=RUN_ID,
            review_fn=self.review_fn([
                {"rule_id": "COMP-004", "quote": QUOTE, "evidence": "缺口径",
                 "confidence": 0.8, "suggestion": "补", "severity": "minor"},
            ]))
        outcome = executor(self.step, 3)
        self.assertEqual([f.id for f in outcome.findings], ["ISS-0003"])
        self.assertEqual(outcome.candidates, 1)
        self.assertEqual(outcome.dropped, 1)
        self.assertTrue(outcome.cached)
        self.assertEqual(outcome.usage, {"input": 3, "output": 4})

    def test_second_step_dedupes_against_first(self):
        executor = self.review.make_semantic_executor(
            session=self.session, text=TEXT, run_id=RUN_ID,
            review_fn=self.review_fn([
                {"rule_id": "COMP-004", "quote": QUOTE, "evidence": "",
                 "confidence": 0.8, "suggestion": "", "severity": "minor"},
            ]))
        first = executor(self.step, 1)
        second = executor(self.step, 1 + len(first.findings))
        self.assertEqual(len(first.findings), 1)
        self.assertEqual(second.findings, (), "同一片段第二次出现应判重")

    def test_executor_uses_rule_layer_exemptions(self):
        self.session.context_exempt.append(("COMP-004", QUOTE, "内部进度"))
        executor = self.review.make_semantic_executor(
            session=self.session, text=TEXT, run_id=RUN_ID,
            review_fn=self.review_fn([
                {"rule_id": "COMP-004", "quote": QUOTE, "evidence": "",
                 "confidence": 0.9, "suggestion": "", "severity": "minor"},
            ]))
        outcome = executor(self.step, 1)
        self.assertEqual(outcome.findings, ())


if __name__ == "__main__":
    unittest.main()
