#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_domain_v1.py —— Architecture v1 领域层的公开不变量。

守的是什么：
    · 六个核心对象的结构自洽（硬不变量在构造时就拦住脏数据）
    · 裁定策略与旧实现等价（blocker→fail、major→warn、库外未核完→warn）
    · 状态机不允许跳级 / 回退 / 从终态复活
    · 阶段推进必须有产物（防止「成功了但什么都没产出」）

为什么放 checks/：
    tests/ 被 .gitignore 排除，远程 clone 与 CI 里没有。这里的用例不含对抗样本，
    可以公开，也是 CI 唯一会跑的新架构测试。

运行：
    python -m unittest discover -s checks -v
"""
import datetime
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (          # noqa: E402
    DEFAULT_POLICY,
    AuditEvent,
    AuditKind,
    Evidence,
    EvidenceKind,
    Finding,
    FindingOrigin,
    FindingStatus,
    InvalidTransition,
    InvariantViolation,
    Location,
    Review,
    ReviewProfile,
    ReviewRun,
    ReviewStatus,
    ReviewTarget,
    RuleRef,
    RunMode,
    RunStats,
    RunStatus,
    Severity,
    Stage,
    Verdict,
    Verification,
    consistency_issues,
    ensure_finding_transition,
    ensure_run_transition,
    ensure_stage_advance,
    finding_violations,
    next_stage,
)
from review_agent.domain import ids as id_tools   # noqa: E402
from review_agent.runtime import NodeResult, RunState, Workflow, advance  # noqa: E402

TZ = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime(2026, 10, 30, 13, 0, 0, tzinfo=TZ)


def make_profile(**overrides) -> ReviewProfile:
    data = dict(id="doc-review", name="文档审查 Agent", version="1.4.0",
                dimensions=("fact", "compliance"), rule_ids=("FACT-001", "COMP-001"))
    data.update(overrides)
    return ReviewProfile(**data)


def make_finding(**overrides) -> Finding:
    data = dict(
        id="ISS-0001", run_id="R-20261030-001-RUN01", dimension="fact",
        severity=Severity.BLOCKER, quote="豆包 App 月活用户突破 10 亿",
        location=Location("第 2 句", sentence=2), confidence=0.95,
        origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id="FACT-001", dimension="fact", declared_severity=Severity.BLOCKER),
        statement="公开口径约 1 亿量级",
    )
    data.update(overrides)
    return Finding(**data)


def make_evidence(**overrides) -> Evidence:
    data = dict(id="EV-0001", run_id="R-20261030-001-RUN01", kind=EvidenceKind.QUOTE,
                quote="豆包 App 月活用户突破 10 亿", location=Location("第 2 句"))
    data.update(overrides)
    return Evidence(**data)


def make_run(**overrides) -> ReviewRun:
    data = dict(
        id="R-20261030-001-RUN01", review_id="R-20261030-001", seq=1,
        status=RunStatus.COMPLETED, stage=Stage.FINALIZE, mode=RunMode.RULE_ONLY,
        profile=make_profile(), started_at=NOW, finished_at=NOW,
        decision_id="DEC-0001",
    )
    data.update(overrides)
    return ReviewRun(**data)


def make_review(**overrides) -> Review:
    data = dict(
        id="R-20261030-001",
        target=ReviewTarget(path="targets/sample.txt", target_type="text",
                            sha256="a" * 64),
        profile_id="doc-review", profile_version="1.4.0", created_at=NOW,
        status=ReviewStatus.COMPLETED, runs=(make_run(),),
    )
    data.update(overrides)
    return Review(**data)


class IdentifierTests(unittest.TestCase):
    def test_review_id_roundtrip(self):
        value = id_tools.format_review_id(datetime.date(2026, 10, 30), 7)
        self.assertEqual(value, "R-20261030-007")
        day, seq = id_tools.parse_review_id(value)
        self.assertEqual((day, seq), (datetime.date(2026, 10, 30), 7))

    def test_next_review_seq_ignores_dirty_ids(self):
        self.assertEqual(id_tools.next_review_seq(["R-20261030-003", "garbage", ""]), 4)

    def test_invalid_ids_are_rejected(self):
        for bad in ("R-2026-001", "R-20261030-000", ""):
            with self.assertRaises(Exception):
                id_tools.parse_review_id(bad)
        for bad in ("ISS-1", "iss-0001", ""):
            with self.assertRaises(Exception):
                id_tools.parse_finding_id(bad)

    def test_run_id_must_belong_to_review(self):
        self.assertEqual(id_tools.format_run_id("R-20261030-001", 2),
                         "R-20261030-001-RUN02")
        with self.assertRaises(InvariantViolation):
            make_run(id="R-20261030-002-RUN01", review_id="R-20261030-001")


class ModelInvariantTests(unittest.TestCase):
    def test_finding_rejects_confidence_out_of_range(self):
        with self.assertRaises(InvariantViolation):
            make_finding(confidence=1.5)

    def test_finding_requires_something_to_cite(self):
        with self.assertRaises(InvariantViolation):
            make_finding(quote="", statement="", severity=Severity.MINOR)

    def test_anchored_evidence_requires_verifier(self):
        with self.assertRaises(InvariantViolation):
            make_evidence(verification=Verification.ANCHORED, verified_by=None)
        ev = make_evidence(verification=Verification.ANCHORED,
                           verified_by="review.s4.evidence_anchor")
        self.assertEqual(ev.verification, Verification.ANCHORED)

    def test_location_span_is_ordered(self):
        with self.assertRaises(InvariantViolation):
            Location("第 1 句", char_start=10, char_end=3)

    def test_completed_run_requires_decision_and_finalize(self):
        with self.assertRaises(InvariantViolation):
            make_run(decision_id=None)
        with self.assertRaises(InvariantViolation):
            make_run(stage=Stage.DECISION)

    def test_unfinished_run_cannot_have_finished_at(self):
        with self.assertRaises(InvariantViolation):
            make_run(status=RunStatus.RUNNING, finished_at=NOW, decision_id=None)

    def test_failed_run_requires_reason(self):
        with self.assertRaises(InvariantViolation):
            make_run(status=RunStatus.FAILED, finished_at=NOW,
                     decision_id=None, error="")
        run = make_run(status=RunStatus.FAILED, finished_at=NOW,
                       decision_id=None, error="LLM 连续超时")
        self.assertEqual(run.error, "LLM 连续超时")

    def test_review_add_run_is_immutable_and_sequential(self):
        review = make_review()
        second = make_run(id="R-20261030-001-RUN02", seq=2)
        updated = review.add_run(second)
        self.assertEqual(len(review.runs), 1, "原对象不能被就地修改")
        self.assertEqual(len(updated.runs), 2)
        self.assertEqual(updated.current_run.id, "R-20261030-001-RUN02")
        with self.assertRaises(InvariantViolation):
            review.add_run(make_run(id="R-20261030-001-RUN03", seq=3))

    def test_stats_exclude_suppressed_on_demand(self):
        findings = [
            make_finding(),
            make_finding(id="ISS-0002", severity=Severity.MAJOR, confidence=0.9),
            make_finding(id="ISS-0003", severity=Severity.MINOR,
                         status=FindingStatus.SUPPRESSED),
        ]
        self.assertEqual(RunStats.from_findings(findings).total, 3)
        self.assertEqual(RunStats.from_findings(findings, exclude_suppressed=True).total, 2)

    def test_profile_fingerprint_tracks_rules(self):
        a = make_profile()
        b = make_profile(rule_ids=("FACT-001",))
        self.assertNotEqual(a.fingerprint(), b.fingerprint())
        self.assertEqual(a.fingerprint(), make_profile().fingerprint())

    def test_audit_detail_is_read_only(self):
        event = AuditEvent(seq=0, run_id="R-20261030-001-RUN01",
                           kind=AuditKind.STEP, name="S1 接收解析",
                           detail={"chars": 44})
        with self.assertRaises(TypeError):
            event.detail["chars"] = 1          # type: ignore[index]


class TransitionTests(unittest.TestCase):
    def test_stage_cannot_skip_or_rewind(self):
        with self.assertRaises(InvalidTransition):
            ensure_stage_advance(Stage.INTAKE, Stage.ANALYSIS)
        with self.assertRaises(InvalidTransition):
            ensure_stage_advance(Stage.ANALYSIS, Stage.PLANNING)
        ensure_stage_advance(Stage.INTAKE, Stage.INGESTION)
        ensure_stage_advance(Stage.ANALYSIS, Stage.ANALYSIS)   # 原地重试允许

    def test_last_stage_has_no_successor(self):
        with self.assertRaises(InvalidTransition):
            next_stage(Stage.FINALIZE)
        self.assertEqual(next_stage(Stage.CROSS_REVIEW), Stage.DECISION)

    def test_terminal_run_statuses_are_closed(self):
        with self.assertRaises(InvalidTransition):
            ensure_run_transition(RunStatus.COMPLETED, RunStatus.RUNNING)
        with self.assertRaises(InvalidTransition):
            ensure_run_transition(RunStatus.CREATED, RunStatus.COMPLETED)
        ensure_run_transition(RunStatus.RUNNING, RunStatus.AWAITING_DECISION)

    def test_finding_terminal_states_are_closed(self):
        ensure_finding_transition(FindingStatus.PROPOSED, FindingStatus.CONFIRMED)
        with self.assertRaises(InvalidTransition):
            ensure_finding_transition(FindingStatus.CONFIRMED, FindingStatus.PROPOSED)


class PolicyTests(unittest.TestCase):
    def test_blocker_means_fail(self):
        draft = DEFAULT_POLICY.decide([make_finding()])
        self.assertIs(draft.verdict, Verdict.FAIL)
        self.assertTrue(draft.requires_human_review)
        self.assertIn("ISS-0001", draft.finding_ids)

    def test_major_means_warn(self):
        draft = DEFAULT_POLICY.decide([
            make_finding(severity=Severity.MAJOR, confidence=0.9)])
        self.assertIs(draft.verdict, Verdict.WARN)

    def test_clean_report_passes(self):
        draft = DEFAULT_POLICY.decide([])
        self.assertIs(draft.verdict, Verdict.PASS)
        self.assertFalse(draft.requires_human_review)

    def test_unverified_research_never_passes(self):
        draft = DEFAULT_POLICY.decide([], external_unverified=True)
        self.assertIs(draft.verdict, Verdict.WARN)
        self.assertIn("库外事实", "".join(draft.reasons))

    def test_low_confidence_forces_human_review(self):
        draft = DEFAULT_POLICY.decide([
            make_finding(severity=Severity.MINOR, confidence=0.5)])
        self.assertTrue(draft.requires_human_review)
        self.assertTrue(any("置信度" in r for r in draft.reasons))

    def test_counts_entry_matches_findings_entry(self):
        a = DEFAULT_POLICY.decide([make_finding()])
        b = DEFAULT_POLICY.decide_from_counts(blocker=1, major=0, total=1)
        self.assertEqual(a.verdict, b.verdict)
        self.assertEqual(a.risk_level, b.risk_level)

    def test_counts_pass_reason_reports_total(self):
        draft = DEFAULT_POLICY.decide_from_counts(blocker=0, major=0, total=7)
        self.assertIs(draft.verdict, Verdict.PASS)
        self.assertIn("7", draft.reasons[0])

    def test_consistency_check_catches_wrong_verdict(self):
        findings = [make_finding()]
        self.assertEqual(consistency_issues(findings, Verdict.FAIL), ())
        problems = consistency_issues(findings, Verdict.PASS)
        self.assertTrue(problems and "策略复算" in problems[0])

    def test_finding_violations_flag_high_severity_low_confidence(self):
        problems = finding_violations([
            make_finding(severity=Severity.MAJOR, confidence=0.6)])
        self.assertTrue(problems)
        self.assertIn("置信度", problems[0])
        noted = make_finding(severity=Severity.MAJOR, confidence=0.6,
                             severity_note="已下调")
        self.assertEqual(finding_violations([noted]), ())


class RuntimeStateTests(unittest.TestCase):
    def state(self, **overrides) -> RunState:
        data = dict(review_id="R-20261030-001", run_id="R-20261030-001-RUN01",
                    stage=Stage.INTAKE, profile=make_profile())
        data.update(overrides)
        return RunState(**data)

    def test_stage_is_derived_from_completed_stages(self):
        with self.assertRaises(InvariantViolation):
            self.state(completed_stages=(Stage.INTAKE,), stage=Stage.INTAKE)

    def test_advance_requires_declared_artifacts(self):
        state = self.state()
        with self.assertRaises(InvariantViolation):
            advance(state, Stage.INTAKE, NodeResult.ok())
        advanced = advance(state, Stage.INTAKE,
                           NodeResult.ok(output={"target_hash": "a" * 64}))
        self.assertEqual(advanced.stage, Stage.INGESTION)
        self.assertEqual(advanced.completed_stages, (Stage.INTAKE,))

    def test_advance_rejects_wrong_stage_and_failure(self):
        state = self.state()
        with self.assertRaises(InvalidTransition):
            advance(state, Stage.INGESTION, NodeResult.ok(output={"target_text": "x",
                                                                  "target_meta": {}}))
        with self.assertRaises(InvalidTransition):
            advance(state, Stage.INTAKE, NodeResult.fail("解析失败"))

    def test_advance_rejects_unregistered_artifact(self):
        with self.assertRaises(InvariantViolation):
            advance(self.state(), Stage.INTAKE,
                    NodeResult.ok(output={"target_hash": "a" * 64, "secret": 1}))

    def test_artifacts_are_read_only(self):
        advanced = advance(self.state(), Stage.INTAKE,
                           NodeResult.ok(output={"target_hash": "a" * 64}))
        with self.assertRaises(TypeError):
            advanced.artifacts["target_hash"] = "b" * 64   # type: ignore[index]

    def test_workflow_allows_many_nodes_per_stage_but_rejects_wrong_order(self):
        class FakeNode:
            produces = frozenset({"plan"})

            def __init__(self, name, stage):
                self.name, self.stage = name, stage

            async def run(self, ctx):                    # pragma: no cover - 仅占位
                return NodeResult.ok()

        # 同一阶段可以挂多个节点（analysis = 规则审 + 语义审），按声明顺序执行
        wf = Workflow((FakeNode("a", Stage.PLANNING), FakeNode("b", Stage.PLANNING)))
        self.assertEqual([n.name for n in wf.nodes_for(Stage.PLANNING)], ["a", "b"])
        self.assertEqual(wf.stages, (Stage.PLANNING,))

        with self.assertRaises(InvariantViolation):
            Workflow((FakeNode("a", Stage.ANALYSIS), FakeNode("b", Stage.INTAKE)))
        wf = Workflow((FakeNode("a", Stage.INTAKE), FakeNode("b", Stage.INGESTION)))
        self.assertEqual(wf.stages, (Stage.INTAKE, Stage.INGESTION))


if __name__ == "__main__":
    unittest.main()
