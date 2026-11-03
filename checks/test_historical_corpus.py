#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史报告评估：只从显式配置的语料目录加载。

运行：
    $env:REVIEW_AGENT_HISTORY_DIR = "D:/path/to/reports"
    python -m unittest checks.test_historical_corpus -v

未设置环境变量时，历史回放类全部跳过；公开 checks 不再隐式读取 outputs/。
设置环境变量后语料必须完整，五个已登记偏差报告缺一不可。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "checks"))

import history_corpus  # noqa: E402
from review_agent.adapters import CONSUMED_KEYS, adapt_report  # noqa: E402
from review_agent.adapters.issue_codec import (  # noqa: E402
    encode_issues,
    export_issues,
)
from review_agent.domain import Severity  # noqa: E402
from review_agent.runtime.nodes import (  # noqa: E402
    anchor_findings,
    build_decision,
    dispute_to_report_dict,
    review_findings,
)

RUN_ID = "R-20261030-001-RUN01"
HISTORY_REASON = (
    "未设置 REVIEW_AGENT_HISTORY_DIR；历史回放需要显式、完整的外部语料")


class HistoricalCorpusLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="history-corpus-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_report(self, review_id, *, verdict="warn", issues=None):
        payload = {
            "review_id": review_id,
            "verdict": verdict,
            "issues": list(issues or []),
            "suppressed": [],
        }
        (self.root / f"{review_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_no_environment_does_not_discover_runtime_reports(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(history_corpus.CORPUS_ENV, None)
            self.assertEqual(history_corpus.load_reports(), ())

    def test_configured_missing_directory_fails(self):
        missing = self.root / "missing"
        with mock.patch.dict(os.environ, {
            history_corpus.CORPUS_ENV: str(missing),
        }, clear=False):
            with self.assertRaises(history_corpus.HistoryCorpusError):
                history_corpus.load_reports()

    def test_partial_corpus_fails_known_report_check(self):
        self.write_report("R-20261101-001")
        with self.assertRaises(history_corpus.HistoryCorpusError) as ctx:
            history_corpus.load_reports(self.root, require_known=True)
        self.assertIn("缺少已登记偏差报告", str(ctx.exception))

    def test_complete_known_corpus_loads_deterministically(self):
        for review_id in history_corpus.KNOWN_VERDICT_DEVIATIONS:
            self.write_report(review_id)
        reports = history_corpus.load_reports(self.root, require_known=True)
        self.assertEqual(
            {report.review_id for report in reports},
            set(history_corpus.KNOWN_VERDICT_DEVIATIONS),
        )

    def test_bad_json_fails_instead_of_disappearing(self):
        (self.root / "R-20261101-002.json").write_text(
            "{broken", encoding="utf-8")
        with self.assertRaises(history_corpus.HistoryCorpusError):
            history_corpus.load_reports(self.root)


@unittest.skipUnless(history_corpus.configured(), HISTORY_REASON)
class DecisionHistoricalReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = history_corpus.load_reports(require_known=True)

    def test_replay_reproduces_historical_verdicts(self):
        unexpected = []
        for report in self.reports:
            raw = report.data
            findings = [e.finding for e in encode_issues(
                raw.get("issues") or [], run_id=RUN_ID)]
            research = raw.get("research") or {}
            decision = build_decision(
                findings, run_id=RUN_ID,
                external_unverified=bool(research.get("unverified")
                                         or research.get("needs_confirm")))
            if decision.verdict.value == raw["verdict"]:
                continue
            if report.review_id in history_corpus.KNOWN_VERDICT_DEVIATIONS:
                continue
            unexpected.append((report.review_id, raw["verdict"],
                               decision.verdict.value))
        self.assertEqual(unexpected, [], "出现未解释的历史裁定差异")

    def test_known_deviation_list_is_not_stale(self):
        still_diff = set()
        for report in self.reports:
            raw = report.data
            findings = [e.finding for e in encode_issues(
                raw.get("issues") or [], run_id=RUN_ID)]
            research = raw.get("research") or {}
            decision = build_decision(
                findings, run_id=RUN_ID,
                external_unverified=bool(research.get("unverified")
                                         or research.get("needs_confirm")))
            if decision.verdict.value != raw["verdict"]:
                still_diff.add(report.review_id)
        self.assertLessEqual(
            history_corpus.KNOWN_VERDICT_DEVIATIONS, still_diff,
            "清单里有已不再偏差的报告，请同步更新")


@unittest.skipUnless(history_corpus.configured(), HISTORY_REASON)
class CrossReviewHistoricalDifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = history_corpus.load_reports(require_known=True)

    def test_node_matches_legacy_cross_review(self):
        import review as legacy_review

        checked = 0
        for report in self.reports:
            raw = report.data
            issues = raw.get("issues") or []
            if not issues:
                continue
            session = legacy_review.Session(report.review_id, "t", "text")
            session.issues = [dict(i) for i in issues]
            legacy_review.cross_review(session)

            findings = [e.finding for e in encode_issues(issues, run_id=RUN_ID)]
            result = review_findings(findings, run_id=RUN_ID)
            revised = {f.id: f.confidence for f in result.revisions}

            self.assertEqual(
                [dispute_to_report_dict(d) for d in result.disputes],
                session.disputes,
                f"{report.review_id}：争议回写形状不一致")
            self.assertEqual(
                [revised.get(f.id, f.confidence) for f in findings],
                [i["confidence"] for i in session.issues],
                f"{report.review_id}：降权后的置信度不一致")
            checked += 1
        self.assertGreater(checked, 0, "历史语料里没有带 issue 的报告")


@unittest.skipUnless(history_corpus.configured(), HISTORY_REASON)
class EvidenceHistoricalEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = history_corpus.load_reports(require_known=True)

    @staticmethod
    def legacy_dropped(quote, text):
        q = quote
        placeholder = isinstance(q, str) and q.startswith("（") and q.endswith("）")
        return bool(q) and q not in text and not placeholder

    def test_dropped_set_matches_old_implementation(self):
        checked = 0
        for report in self.reports:
            raw = report.data
            text = str(raw.get("target_text") or "")
            issues = raw.get("issues") or []
            findings = [e.finding for e in encode_issues(issues, run_id=RUN_ID)]
            result = anchor_findings(findings, text, run_id=RUN_ID)
            expected = {
                f.id for f, item in zip(findings, issues)
                if self.legacy_dropped(item.get("quote"), text)
            }
            self.assertEqual(
                set(result.dropped_ids), expected,
                f"{report.review_id} 丢弃集合与旧实现不一致")
            checked += len(findings)
        self.assertGreater(checked, 0, "历史语料里一条 issue 都没有")


@unittest.skipUnless(history_corpus.configured(), HISTORY_REASON)
class IssueCodecHistoricalCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = history_corpus.load_reports(require_known=True)

    def test_every_historical_issue_round_trips(self):
        total = 0
        for report in self.reports:
            raw = report.data
            for key in ("issues", "suppressed"):
                issues = raw.get(key) or []
                rebuilt = export_issues(issues, run_id=RUN_ID)
                self.assertEqual(
                    rebuilt, [dict(item) for item in issues],
                    f"{report.path.name}: {key} 回写不一致")
                total += len(issues)
        self.assertGreater(total, 0, "历史语料里一条 issue 都没有")


@unittest.skipUnless(history_corpus.configured(), HISTORY_REASON)
class LegacyAdapterHistoricalCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reports = history_corpus.load_reports(require_known=True)

    def test_corpus_is_present(self):
        self.assertGreaterEqual(len(self.reports), 100)

    def test_all_reports_adapt_without_error(self):
        for report in self.reports:
            with self.subTest(report=report.review_id):
                adapt_report(report.data)

    def test_verdict_deviations_are_exactly_the_known_ones(self):
        deviations = set()
        for report in self.reports:
            adapted = adapt_report(report.data)
            if any("策略复算" in warning for warning in adapted.warnings):
                deviations.add(report.review_id)
        self.assertEqual(
            deviations, set(history_corpus.KNOWN_VERDICT_DEVIATIONS),
            "历史报告的裁定偏差集合变了：要么新增了不符合策略的报告，"
            "要么已知问题被修复（两种情况都要更新清单）")

    def test_finding_counts_and_stats_match_every_report(self):
        for report in self.reports:
            raw = report.data
            with self.subTest(report=report.review_id):
                adapted = adapt_report(raw)
                expected = len(raw.get("issues") or []) + len(
                    raw.get("suppressed") or [])
                self.assertEqual(len(adapted.findings), expected)
                stats = raw.get("stats") or {}
                self.assertEqual(
                    adapted.stats.total,
                    sum(int(stats.get(key) or 0)
                        for key in ("blocker", "major", "minor", "nit")),
                )
                ids = [finding.id for finding in adapted.findings]
                self.assertEqual(len(ids), len(set(ids)), "finding id 重复")

    def test_high_severity_low_confidence_findings_are_documented(self):
        for report in self.reports:
            adapted = adapt_report(report.data)
            for finding in adapted.findings:
                if (finding.severity in (Severity.BLOCKER, Severity.MAJOR)
                        and finding.confidence < 0.7):
                    self.assertIsNotNone(
                        finding.severity_note,
                        f"{report.review_id}/{finding.id} 低置信高严重度但没有说明")

    def test_no_report_data_is_dropped(self):
        for report in self.reports:
            adapted = adapt_report(report.data)
            with self.subTest(report=report.review_id):
                self.assertEqual(
                    set(adapted.extra),
                    set(report.data) - CONSUMED_KEYS)


if __name__ == "__main__":
    unittest.main()
