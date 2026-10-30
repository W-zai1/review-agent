#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_legacy_adapter.py —— 历史报告 → 领域对象的迁移验收。

两条底线：
    1. **不丢数据**：报告顶层每个键，要么被领域对象消费，要么原样进 extra。
       迁移一次丢一批字段，是这类重构最常见的事故。
    2. **能证伪**：用 DecisionPolicy 复算旧报告的 verdict。复算不出来的报告
       不是「策略错了」，而是「旧实现没按自己声明的规则裁定」——这类差异必须
       被登记，不能靠人肉记忆。

历史全量用例只在本地跑（outputs/ 被 .gitignore 排除，CI 里没有）；
CI 里跑的是 checks/fixtures/legacy_report_min.json 这份最小样本。

运行：
    python -m unittest discover -s checks -v
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (                      # noqa: E402
    FindingStatus,
    Severity,
    Verdict,
    Verification,
    consistency_issues,
)
from review_agent.adapters import CONSUMED_KEYS, adapt_report  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy_report_min.json"
OUTPUTS = ROOT / "outputs"

# 已核对的历史偏差：这 5 份报告在「库外事实未核完」时仍给了 pass。
# 旧版本的 verdict 计算发生在这段兜底逻辑上线之前；新策略会判 warn。
# 留成显式清单而不是 skip：一旦历史报告被重生成，这里会失败并提醒复核。
KNOWN_VERDICT_DEVIATIONS = {
    "R-20261028-045", "R-20261028-046", "R-20261028-047",
    "R-20261028-055", "R-20261028-071",
}


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class FixtureAdaptationTests(unittest.TestCase):
    def setUp(self):
        self.raw = load_fixture()
        self.adapted = adapt_report(self.raw)

    def test_identity_and_status(self):
        self.assertEqual(self.adapted.review.id, "R-20261030-001")
        self.assertEqual(self.adapted.run.id, "R-20261030-001-RUN01")
        self.assertEqual(self.adapted.review.status.value, "completed")
        self.assertEqual(self.adapted.run.status.value, "completed")
        self.assertEqual(self.adapted.run.stage.value, "finalize")

    def test_findings_include_suppressed_and_keep_ids(self):
        ids = [f.id for f in self.adapted.findings]
        self.assertEqual(ids, ["ISS-0001", "ISS-0002", "ISS-0003"])
        suppressed = [f for f in self.adapted.findings
                      if f.status is FindingStatus.SUPPRESSED]
        self.assertEqual([f.id for f in suppressed], ["ISS-0003"])

    def test_low_confidence_major_keeps_downgrade_note(self):
        major = next(f for f in self.adapted.findings if f.id == "ISS-0002")
        self.assertIs(major.severity, Severity.MAJOR)
        self.assertEqual(major.confidence, 0.6)
        self.assertIn("未执行严重度下调", major.severity_note or "")
        self.assertTrue(any("ISS-0002" in w for w in self.adapted.warnings))

    def test_evidence_is_anchored_against_target_text(self):
        self.assertEqual(len(self.adapted.evidence), 3)
        for ev in self.adapted.evidence:
            self.assertIs(ev.verification, Verification.ANCHORED)
            self.assertEqual(ev.verified_by, "review.s4.evidence_anchor")

    def test_dispute_and_decision_are_materialised(self):
        self.assertEqual(len(self.adapted.disputes), 1)
        dispute = self.adapted.disputes[0]
        self.assertEqual(dispute.id, "DSP-0001")
        self.assertEqual(dispute.issue_ids, ("ISS-0001", "ISS-0002"))
        decision = self.adapted.decisions[0]
        self.assertIs(decision.verdict, Verdict.FAIL)
        self.assertEqual(decision.finding_ids, ("ISS-0001",))

    def test_stats_match_report(self):
        self.assertEqual(self.adapted.stats.blocker, 1)
        self.assertEqual(self.adapted.stats.major, 1)
        self.assertEqual(self.adapted.stats.minor, 1)
        self.assertEqual(self.adapted.stats.total, 3)

    def test_every_top_level_key_is_consumed_or_preserved(self):
        self.assertLessEqual(CONSUMED_KEYS, set(self.raw),
                             "CONSUMED_KEYS 里出现了样本中不存在的键：口径漂了")
        self.assertEqual(set(self.adapted.extra), set(self.raw) - CONSUMED_KEYS)
        for key, value in self.adapted.extra.items():
            self.assertEqual(value, self.raw[key], f"extra[{key!r}] 被改写了")

    def test_verdict_reproduced_by_policy(self):
        adapted = self.adapted
        problems = consistency_issues(
            adapted.findings, adapted.verdict,
            external_unverified=bool(
                (adapted.extra.get("research") or {}).get("unverified")))
        self.assertEqual(problems, ())

    def test_audit_events_carry_steps_and_decisions(self):
        steps = [{"step": "S1", "name": "接收解析", "status": "success",
                  "ts": "2026-10-30T13:00:00+08:00", "latency_ms": 3,
                  "detail": {"字符数": "44"}}]
        adapted = adapt_report(self.raw, steps=steps)
        kinds = [e.kind.value for e in adapted.audit]
        self.assertEqual(kinds.count("step"), 1)
        self.assertEqual(kinds.count("decision"), 1)


@unittest.skipUnless(OUTPUTS.is_dir() and list(OUTPUTS.glob("R-*.json")),
                     "outputs/ 不在当前 checkout（CI 只跑最小样本）")
class HistoricalCorpusTests(unittest.TestCase):
    """全量历史报告回归：202 份报告必须都能被领域模型表达。"""

    @classmethod
    def setUpClass(cls):
        cls.reports = []
        for path in sorted(OUTPUTS.glob("R-*.json")):
            cls.reports.append((path.stem, json.loads(path.read_text(encoding="utf-8"))))

    def test_corpus_is_present(self):
        self.assertGreaterEqual(len(self.reports), 100)

    def test_all_reports_adapt_without_error(self):
        for name, raw in self.reports:
            with self.subTest(report=name):
                adapt_report(raw)

    def test_verdict_deviations_are_exactly_the_known_ones(self):
        deviations = set()
        for name, raw in self.reports:
            adapted = adapt_report(raw)
            if any("策略复算" in w for w in adapted.warnings):
                deviations.add(name)
        self.assertEqual(deviations, KNOWN_VERDICT_DEVIATIONS,
                         "历史报告的裁定偏差集合变了：要么新增了不符合策略的报告，"
                         "要么已知问题被修复（两种情况都要更新清单）")

    def test_finding_counts_and_stats_match_every_report(self):
        for name, raw in self.reports:
            with self.subTest(report=name):
                adapted = adapt_report(raw)
                expected = len(raw.get("issues") or []) + len(raw.get("suppressed") or [])
                self.assertEqual(len(adapted.findings), expected)
                stats = raw.get("stats") or {}
                self.assertEqual(adapted.stats.total,
                                 sum(int(stats.get(k) or 0) for k in
                                     ("blocker", "major", "minor", "nit")))
                ids = [f.id for f in adapted.findings]
                self.assertEqual(len(ids), len(set(ids)), "finding id 重复")

    def test_high_severity_low_confidence_findings_are_documented(self):
        for name, raw in self.reports:
            adapted = adapt_report(raw)
            for f in adapted.findings:
                if (f.severity in (Severity.BLOCKER, Severity.MAJOR)
                        and f.confidence < 0.7):
                    self.assertIsNotNone(
                        f.severity_note, f"{name}/{f.id} 低置信高严重度但没有说明")

    def test_no_report_data_is_dropped(self):
        for name, raw in self.reports:
            adapted = adapt_report(raw)
            with self.subTest(report=name):
                self.assertEqual(set(adapted.extra), set(raw) - CONSUMED_KEYS)


if __name__ == "__main__":
    unittest.main()
