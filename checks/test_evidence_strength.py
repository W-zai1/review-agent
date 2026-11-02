#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_evidence_strength.py —— 证据第二根轴（批次 3 / B1）的验收。

《审查意见》举的例子是这段：材料写「营收 100 亿」，quote 校验成功 → `anchored`
→ 报告算「可回溯」，外部来源显示 80 亿时，**没有任何字段能表达「这条证据被反证了」**。

B1 补的就是这根轴：`EvidenceStrength`。它与 `verification` **正交**，
并且用三条硬不变量让它「写不出来」——想标 `VERIFIED` 就必须四项匹配全过；
想标 `CONTRADICTED` 就必须指出反证；标了 `UNASSESSED` 就不许带评估结果。

同时验收 B1 范围内的两个断点：
    N1 双向索引：`Finding.evidence_ids` 在 v1 上恒为空 → evidence 阶段回填，
        `advance()` 拒绝悬空指向
    N2 原文快照：报告带 `snapshot_ref` + `snapshot_hash` + `target_text`，
        报告离开本机也能自证「quote 真的来自原文」

不联网、不跑真实审查：全部是纯对象与节点级用例（端到端在 test_research_v1）。
"""
import asyncio
import datetime
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from review_agent.application import build_shadow_report, render_shadow_md  # noqa: E402
from review_agent.domain import (                       # noqa: E402
    MATCH_KINDS,
    STAGE_ORDER,
    Decision,
    Evidence,
    EvidenceAssessment,
    EvidenceKind,
    EvidenceStrength,
    Finding,
    FindingOrigin,
    InvariantViolation,
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
from review_agent.runtime import NodeResult, RunState, advance, state_from_dict, state_to_dict  # noqa: E402
from review_agent.runtime.node import NodeContext        # noqa: E402
from review_agent.runtime.nodes import EvidenceAnalyzerNode, backfill_evidence_ids  # noqa: E402

REVIEW_ID = "R-20261101-092"
RUN_ID = f"{REVIEW_ID}-RUN01"
TEXT = "小米 2024 年全球智能手机出货量为 5 亿台，位居全球第三。"


def all_pass(**override):
    checks = []
    for kind in MATCH_KINDS:
        checks.append(MatchCheck(kind=kind, ok=override.get(kind, True),
                                 detail=override.get(f"{kind}_detail", "")))
    return EvidenceAssessment(checks=tuple(checks), assessed_by="evidence-assessor")


def evidence(**over):
    base = dict(id="EV-0001", run_id=RUN_ID, kind=EvidenceKind.QUOTE,
                quote="原文一句话", location=Location("第 1 句", 1),
                verification=Verification.ANCHORED,
                verified_by="review.s4.evidence_anchor")
    base.update(over)
    return Evidence(**base)


def external(**over):
    base = dict(id="EV-0002", run_id=RUN_ID, kind=EvidenceKind.EXTERNAL_SOURCE,
                quote="来源页面片段", source_ref="https://www.gov.cn/a",
                source=SourceRef(url="https://www.gov.cn/a", domain="www.gov.cn",
                                 official=True,
                                 snapshot_hash="b" * 64))
    base.update(over)
    return Evidence(**base)


def finding(seq=1, **over):
    base = dict(id=f"ISS-{seq:04d}", run_id=RUN_ID, dimension="fact",
                severity=Severity.MAJOR, quote=TEXT,
                location=Location("第 1 句", 1), confidence=0.8,
                origin=FindingOrigin.RULE,
                rule=RuleRef(rule_id="FACT-001", dimension="fact"))
    base.update(over)
    return Finding(**base)


# ---------------------------------------------------------------------------
# 枚举：两根轴必须能分开说
# ---------------------------------------------------------------------------
class EvidenceStrengthEnumTests(unittest.TestCase):
    def test_values_are_the_agreed_five(self):
        self.assertEqual([s.value for s in EvidenceStrength],
                         ["unassessed", "unverified", "partial", "verified",
                          "contradicted"])

    def test_default_is_unassessed_not_unverified(self):
        self.assertIs(evidence().strength, EvidenceStrength.UNASSESSED)

    def test_ordering_is_the_weakest_link_order(self):
        self.assertEqual(EvidenceStrength.ordered(),
                         (EvidenceStrength.UNASSESSED, EvidenceStrength.UNVERIFIED,
                          EvidenceStrength.PARTIAL, EvidenceStrength.VERIFIED))
        ranks = [s.rank for s in EvidenceStrength.ordered()]
        self.assertEqual(ranks, sorted(ranks))
        # 反证不是"更弱"，它不在同一根轴上的刻度里
        self.assertNotIn(EvidenceStrength.CONTRADICTED,
                         EvidenceStrength.ordered())
        self.assertLess(EvidenceStrength.CONTRADICTED.rank,
                        EvidenceStrength.UNASSESSED.rank)

    def test_strength_and_verification_are_orthogonal(self):
        """同一条 anchored 证据，强度可以是 PARTIAL——这正是旧模型缺的那一维。"""
        ev = evidence(verification=Verification.ANCHORED,
                      strength=EvidenceStrength.PARTIAL,
                      assessment=EvidenceAssessment(
                          checks=(MatchCheck("subject", True),
                                  MatchCheck("period", None, "原文未给年份")),
                          assessed_by="evidence-assessor"))
        self.assertIs(ev.verification, Verification.ANCHORED)
        self.assertIs(ev.strength, EvidenceStrength.PARTIAL)

    def test_labels_are_chinese(self):
        self.assertEqual(EvidenceStrength.VERIFIED.label, "已核验")
        self.assertEqual(EvidenceStrength.PARTIAL.label, "部分核验")


# ---------------------------------------------------------------------------
# 值对象
# ---------------------------------------------------------------------------
class SourceRefTests(unittest.TestCase):
    def test_domain_is_derived_from_url_when_missing(self):
        ref = SourceRef(url="https://WWW.Gov.CN/a/b")
        self.assertEqual(ref.domain, "www.gov.cn")
        self.assertFalse(ref.is_snapshot)

    def test_url_must_be_http(self):
        for bad in ("ftp://x/a", "javascript:alert(1)", "", "example.com/a"):
            with self.assertRaises(InvariantViolation, msg=bad):
                SourceRef(url=bad)

    def test_snapshot_hash_marks_it_snapshot(self):
        self.assertTrue(SourceRef(url="https://a.com/x",
                                  snapshot_hash="c" * 64).is_snapshot)


class MatchCheckTests(unittest.TestCase):
    def test_kind_is_closed_set(self):
        with self.assertRaises(InvariantViolation):
            MatchCheck(kind="vibes", ok=True)

    def test_unknown_is_not_a_pass(self):
        self.assertFalse(MatchCheck(kind="subject", ok=None).passed)
        self.assertFalse(MatchCheck(kind="subject", ok=False).passed)
        self.assertTrue(MatchCheck(kind="subject", ok=True).passed)

    def test_ok_must_be_tristate(self):
        with self.assertRaises(InvariantViolation):
            MatchCheck(kind="subject", ok="true")


class AssessmentTests(unittest.TestCase):
    def test_four_kinds_are_required_for_all_passed(self):
        partial = EvidenceAssessment(
            checks=(MatchCheck("subject", True), MatchCheck("metric", True)),
            assessed_by="x")
        self.assertFalse(partial.covers_all)
        self.assertFalse(partial.all_passed)
        self.assertTrue(all_pass().covers_all)
        self.assertTrue(all_pass().all_passed)

    def test_unknown_check_blocks_all_passed(self):
        assessment = all_pass(period=None)
        self.assertTrue(assessment.covers_all)
        self.assertFalse(assessment.all_passed)

    def test_checks_need_an_assessor(self):
        with self.assertRaises(InvariantViolation):
            EvidenceAssessment(checks=(MatchCheck("subject", True),))

    def test_duplicate_kind_is_rejected(self):
        with self.assertRaises(InvariantViolation):
            EvidenceAssessment(checks=(MatchCheck("subject", True),
                                       MatchCheck("subject", True)),
                               assessed_by="x")

    def test_failed_lists_only_explicit_failures(self):
        assessment = all_pass(period=False, metric=None)
        self.assertEqual(assessment.failed, ("时间口径",))


# ---------------------------------------------------------------------------
# 三条硬不变量：让"强度"写不出来
# ---------------------------------------------------------------------------
class InvariantTests(unittest.TestCase):
    def test_verified_requires_four_passing_checks(self):
        with self.assertRaises(InvariantViolation):
            evidence(strength=EvidenceStrength.VERIFIED, verified_by="x")
        with self.assertRaises(InvariantViolation):
            evidence(strength=EvidenceStrength.VERIFIED,
                     assessment=EvidenceAssessment(
                         checks=(MatchCheck("subject", True),), assessed_by="x"),
                     verified_by="x")
        with self.assertRaises(InvariantViolation):
            evidence(strength=EvidenceStrength.VERIFIED,
                     assessment=all_pass(period=None), verified_by="x")
        ok = evidence(strength=EvidenceStrength.VERIFIED, assessment=all_pass())
        self.assertTrue(ok.is_sufficient)

    def test_verified_cannot_carry_counter_evidence(self):
        with self.assertRaises(InvariantViolation):
            evidence(strength=EvidenceStrength.VERIFIED, assessment=all_pass(),
                     contradicted_by=("EV-0009",))

    def test_external_verified_needs_page_snapshot(self):
        """只有搜索摘要、没有正文快照的外部来源，最多只能到 PARTIAL。"""
        with self.assertRaises(InvariantViolation):
            external(strength=EvidenceStrength.VERIFIED, assessment=all_pass(),
                     source=SourceRef(url="https://www.gov.cn/a",
                                      domain="www.gov.cn", official=True))
        ok = external(strength=EvidenceStrength.VERIFIED, assessment=all_pass())
        self.assertTrue(ok.is_sufficient)

    def test_contradicted_must_point_at_counter_evidence(self):
        with self.assertRaises(InvariantViolation):
            evidence(strength=EvidenceStrength.CONTRADICTED)
        ev = evidence(strength=EvidenceStrength.CONTRADICTED,
                      contradicted_by=("EV-0002",))
        self.assertFalse(ev.is_sufficient)

    def test_unassessed_must_not_carry_an_assessment(self):
        with self.assertRaises(InvariantViolation):
            evidence(strength=EvidenceStrength.UNASSESSED, assessment=all_pass())
        self.assertIsNone(evidence().assessment)

    def test_partial_must_say_what_is_partial(self):
        with self.assertRaises(InvariantViolation):
            evidence(strength=EvidenceStrength.PARTIAL)
        ok = evidence(strength=EvidenceStrength.PARTIAL, assessment=all_pass())
        self.assertIs(ok.strength, EvidenceStrength.PARTIAL)

    def test_external_source_requires_source_object(self):
        with self.assertRaises(InvariantViolation):
            Evidence(id="EV-0003", run_id=RUN_ID, kind=EvidenceKind.EXTERNAL_SOURCE,
                     quote="x", source_ref="https://a.com/x")

    def test_contradicted_by_ids_must_be_evidence_ids(self):
        with self.assertRaises(Exception):
            evidence(strength=EvidenceStrength.CONTRADICTED,
                     contradicted_by=("not-an-id",))


# ---------------------------------------------------------------------------
# 向后兼容：历史证据/旧检查点原样读回
# ---------------------------------------------------------------------------
class BackwardCompatibilityTests(unittest.TestCase):
    def test_old_evidence_dict_reads_as_unassessed(self):
        old = {"id": "EV-0001", "run_id": RUN_ID, "kind": "quote",
               "quote": "原文一句话", "location": {"label": "第 1 句", "sentence": 1},
               "verification": "anchored", "verified_by": "review.s4.evidence_anchor",
               "finding_ids": ["ISS-0001"]}
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.EVIDENCE,
            profile=ReviewProfile(id="doc-review", name="审查", version="1"),
            completed_stages=STAGE_ORDER[:4], evidence=(), artifacts={})
        restored = state_from_dict({
            **state_to_dict(state),
            "evidence": [old],
        })
        ev = restored.evidence[0]
        self.assertIs(ev.strength, EvidenceStrength.UNASSESSED)
        self.assertIsNone(ev.assessment)
        self.assertEqual(ev.contradicted_by, ())
        self.assertIsNone(ev.source)

    def test_roundtrip_keeps_the_second_axis(self):
        ev = external(strength=EvidenceStrength.PARTIAL,
                      assessment=EvidenceAssessment(
                          checks=(MatchCheck("source_identity", True),
                                  MatchCheck("subject", True),
                                  MatchCheck("metric", None, "指标口径不明")),
                          assessed_by="evidence-assessor",
                          assessed_at=datetime.datetime(2026, 11, 1, 12, 0)),
                      contradicted_by=("EV-0007",))
        data = state_to_dict(RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.EVIDENCE,
            profile=ReviewProfile(id="doc-review", name="审查", version="1"),
            completed_stages=STAGE_ORDER[:4], evidence=(ev,), artifacts={}))
        restored = state_from_dict(data)
        again = restored.evidence[0]
        self.assertIs(again.strength, EvidenceStrength.PARTIAL)
        self.assertEqual(again.contradicted_by, ("EV-0007",))
        self.assertEqual(again.source.domain, "www.gov.cn")
        self.assertEqual(again.assessment.assessed_by, "evidence-assessor")
        self.assertEqual([c.kind for c in again.assessment.checks],
                         ["source_identity", "subject", "metric"])


# ---------------------------------------------------------------------------
# N1 双向索引
# ---------------------------------------------------------------------------
def evidence_state(findings=(), *, evidence=(), text=TEXT):
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.EVIDENCE,
        profile=ReviewProfile(id="doc-review", name="审查", version="1"),
        completed_stages=STAGE_ORDER[:4], findings=tuple(findings),
        evidence=tuple(evidence), artifacts={"target_text": text})


class BidirectionalIndexTests(unittest.TestCase):
    def test_backfill_adds_evidence_ids_without_touching_others(self):
        f1, f2 = finding(1), finding(2, quote="另一句不在原文里")
        ev = evidence(finding_ids=("ISS-0001",))
        revisions = backfill_evidence_ids([f1, f2], [ev])
        self.assertEqual([r.id for r in revisions], ["ISS-0001"])
        self.assertEqual(revisions[0].evidence_ids, ("EV-0001",))
        self.assertEqual(f1.evidence_ids, (), "上游对象不许被就地改")

    def test_backfill_merges_instead_of_overwriting(self):
        """库里核验阶段已经挂过证据的发现，不能被锚定阶段覆盖掉。"""
        f = finding(1, evidence_ids=("EV-0009",))
        revisions = backfill_evidence_ids([f], [evidence(finding_ids=("ISS-0001",))])
        self.assertEqual(revisions[0].evidence_ids, ("EV-0009", "EV-0001"))

    def test_backfill_is_idempotent(self):
        f = finding(1, evidence_ids=("EV-0001",))
        self.assertEqual(backfill_evidence_ids([f], [evidence()]), ())

    def test_node_revises_findings_with_their_evidence(self):
        state = evidence_state([finding(1)])
        result = asyncio.run(EvidenceAnalyzerNode().run(NodeContext(state=state)))
        self.assertTrue(result.success, result.errors)
        self.assertEqual(len(result.revisions), 1)
        self.assertEqual(result.revisions[0].evidence_ids, ("EV-0001",))

        nxt = advance(state, Stage.EVIDENCE, result)
        stored = {f.id: f for f in nxt.findings}[result.revisions[0].id]
        self.assertEqual(stored.evidence_ids, ("EV-0001",))
        # 双向都能查到对方
        self.assertEqual(nxt.evidence[0].finding_ids, ("ISS-0001",))

    def test_advance_rejects_dangling_evidence_ids(self):
        state = evidence_state([finding(1)])
        with self.assertRaises(InvariantViolation):
            advance(state, Stage.EVIDENCE, NodeResult.ok(
                revisions=(replace(finding(1), evidence_ids=("EV-0999",)),),
                output={"evidence_dropped": ()}))

    def test_advance_accepts_evidence_produced_in_the_same_stage(self):
        state = evidence_state([finding(1)])
        result = NodeResult.ok(
            evidence=(evidence(finding_ids=("ISS-0001",)),),
            revisions=(replace(finding(1), evidence_ids=("EV-0001",)),),
            output={"evidence_dropped": ()})
        nxt = advance(state, Stage.EVIDENCE, result)
        self.assertEqual(nxt.findings[0].evidence_ids, ("EV-0001",))


# ---------------------------------------------------------------------------
# N2 原文快照与报告自证
# ---------------------------------------------------------------------------
def final_state(*, snapshot=None, research=None):
    artifacts = {"target_text": TEXT, "target_hash": "a" * 64}
    if snapshot:
        artifacts["target_snapshot"] = snapshot
    if research:
        artifacts["research"] = research
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.FINALIZE,
        profile=ReviewProfile(id="doc-review", name="审查", version="1"),
        completed_stages=STAGE_ORDER,
        findings=(finding(1, evidence_ids=("EV-0001",)),),
        evidence=(evidence(strength=EvidenceStrength.VERIFIED,
                           assessment=all_pass(), finding_ids=("ISS-0001",)),),
        decision=Decision(id="DEC-0001", run_id=RUN_ID, verdict=Verdict.WARN,
                          rationale="1 条 major"),
        artifacts=artifacts)


class SnapshotTests(unittest.TestCase):
    def test_report_carries_snapshot_and_text(self):
        report = build_shadow_report(
            final_state(),
            target_snapshot={"ref": "state/text/" + "a" * 64 + ".txt",
                             "hash": "b" * 64},
            logs_hash="c" * 64)
        self.assertEqual(report["target"]["snapshot_ref"],
                         "state/text/" + "a" * 64 + ".txt")
        self.assertEqual(report["target"]["snapshot_hash"], "b" * 64)
        # 与旧报告同名的顶层键：前端高亮/定位靠它
        self.assertEqual(report["target_text"], TEXT)

    def test_absent_snapshot_is_declared(self):
        report = build_shadow_report(final_state(), logs_hash=None)
        self.assertIsNone(report["target"]["snapshot_ref"])
        self.assertTrue(any("未附原文快照" in item for item in report["limitations"]))

    def test_markdown_shows_snapshot(self):
        report = build_shadow_report(
            final_state(),
            target_snapshot={"ref": "state/text/abc.txt", "hash": "d" * 64},
            logs_hash="c" * 64)
        text = render_shadow_md(report)
        self.assertIn("原文快照", text)
        self.assertIn("state/text/abc.txt", text)

    def test_evidence_rows_carry_strength(self):
        report = build_shadow_report(final_state(), logs_hash="c" * 64)
        row = report["evidence"][0]
        self.assertEqual(row["strength"], "verified")
        self.assertEqual(row["contradicted_by"], [])
        text = render_shadow_md(report)
        self.assertIn("强度 verified", text)

    def test_unassessed_evidence_is_not_displayed_as_strength(self):
        """缺省强度不往 Markdown 里刷「强度 unassessed」——它只是"没评"。"""
        state = final_state()
        plain = RunState(
            review_id=state.review_id, run_id=state.run_id, stage=state.stage,
            profile=state.profile, completed_stages=state.completed_stages,
            findings=state.findings,
            evidence=(evidence(finding_ids=("ISS-0001",)),),
            decision=state.decision, artifacts=state.artifacts)
        text = render_shadow_md(build_shadow_report(plain, logs_hash="c" * 64))
        self.assertNotIn("强度 unassessed", text)


class SnapshotWiringTests(unittest.TestCase):
    """端到端：v1 真的写出 `state/text/<sha256>.txt`，且报告指得回去。"""

    def setUp(self):
        import review
        self.review = review
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.names = ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")
        self.saved = {name: getattr(review, name) for name in self.names}
        self.addCleanup(self._restore)
        for name in ("outputs", "logs", "ledger", "state"):
            (self.base / name).mkdir()
        review.OUTPUTS = self.base / "outputs"
        review.LOGS = self.base / "logs"
        review.LEDGER = self.base / "ledger"
        review.STATE = self.base / "state"
        review.GAP_LOG = self.base / "state" / "gap_log.jsonl"

    def _restore(self):
        for name, value in self.saved.items():
            setattr(self.review, name, value)
        self.tmp.cleanup()

    def test_snapshot_is_written_and_hashes_to_the_text(self):
        import hashlib
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.FINALIZE,
            profile=ReviewProfile(id="doc-review", name="审查", version="1"),
            completed_stages=STAGE_ORDER, artifacts={"target_text": TEXT})
        snapshot = self.review._write_target_snapshot(state)
        self.assertIsNotNone(snapshot)
        path = Path(snapshot["ref"])
        self.assertTrue(path.is_file(), path)
        self.assertEqual(hashlib.sha256(TEXT.encode("utf-8")).hexdigest(),
                         snapshot["hash"])
        self.assertEqual(path.read_text(encoding="utf-8"), TEXT)
        # 幂等：再写一次不换文件、不换哈希
        self.assertEqual(self.review._write_target_snapshot(state), snapshot)

    def test_missing_text_returns_no_snapshot(self):
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.FINALIZE,
            profile=ReviewProfile(id="doc-review", name="审查", version="1"),
            completed_stages=STAGE_ORDER, artifacts={})
        self.assertIsNone(self.review._write_target_snapshot(state))

    def test_retention_plan_covers_the_snapshot_dir(self):
        from review_agent.infrastructure import retention as ret
        snap = self.base / "state" / "text" / ("e" * 64 + ".txt")
        snap.parent.mkdir(parents=True, exist_ok=True)
        snap.write_text(TEXT, encoding="utf-8")
        old = datetime.datetime.now() - datetime.timedelta(days=120)
        import os
        os.utime(snap, (old.timestamp(), old.timestamp()))
        plan = ret.plan(self.base, config={"text": 90})
        self.assertEqual([p.category for p in plan.items], ["text"])
        # 台账与任务记录照旧不进计划
        self.assertNotIn("ledger", {p.category for p in plan.items})


if __name__ == "__main__":
    unittest.main()
