#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_report_evidence.py —— 报告 `evidence[]` 段（阶段 1/2 的交叉验收）。

产品定位是「证据驱动」：`issues[].quote` 只能说明「我引了哪一句」，
不能说明「这句核过没有」。报告新增的 `evidence[]` 回答后者：

    anchored     逐字命中原文（可回查）
    placeholder  占位式引用（缺失型问题，原文确实没有）
    （不会出现 unanchored：不可回溯的条目已在 S4 被移出报告）

三条验收：
    1. 每条 issue 都能在 evidence 里找到自己（不许悬空、不许漏）
    2. 标了 anchored 的引用必须真的能在原文里找到（不许自称校验过）
    3. 既有字段一个不动：schema_version 升级、issues 结构不变
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import Evidence, EvidenceKind, Location, Verification  # noqa: E402
from review_agent.runtime.nodes import evidence_to_report  # noqa: E402

TARGET = "targets/adversarial_meeting_001.txt"

# 端到端用例要跑真实审查，依赖本地私有资产（targets/ 样本 + agents/ Agent 定义）；
# 公开 clone 不含这两者（见 .gitignore），缺失时整类跳过，本地带资产时照常运行。
PRIVATE_OK = (ROOT / TARGET).exists() and (ROOT / "agents").is_dir()


class EvidenceReportShapeTests(unittest.TestCase):
    def test_each_evidence_points_at_its_finding(self):
        ev = Evidence(id="EV-0001", run_id="R-20261030-001-RUN01",
                      kind=EvidenceKind.QUOTE, quote="引用", location=Location("第 1 句", 1),
                      verification=Verification.ANCHORED,
                      verified_by="review.s4.evidence_anchor",
                      finding_ids=("ISS-0001",))
        row = evidence_to_report([ev])[0]
        self.assertEqual(row["issue_id"], "ISS-0001")
        self.assertEqual(row["verification"], "anchored")
        self.assertEqual(row["verified_by"], "review.s4.evidence_anchor")
        self.assertEqual(row["location"], "第 1 句")

    def test_placeholder_has_no_verifier(self):
        ev = Evidence(id="EV-0002", run_id="R-20261030-001-RUN01",
                      kind=EvidenceKind.QUOTE, quote="（文档首段）",
                      verification=Verification.PLACEHOLDER)
        row = evidence_to_report([ev])[0]
        self.assertIsNone(row["verified_by"])
        self.assertIsNone(row["issue_id"])       # 没有关联发现时不编一个出来

    def test_fields_are_flat_and_stable(self):
        ev = Evidence(id="EV-0003", run_id="R-20261030-001-RUN01", quote="引用")
        self.assertEqual(
            list(evidence_to_report([ev])[0]),
            ["evidence_id", "issue_id", "kind", "quote", "location",
             "verification", "verified_by"])


@unittest.skipUnless(PRIVATE_OK,
                     "私有审查资产缺失（targets/ 样本或 agents/ 定义不在公开仓库），"
                     "跳过报告证据端到端用例")
class ReportEvidenceEndToEndTests(unittest.TestCase):
    """跑一次真实审查（进程内、临时目录），检查报告里的证据段。"""

    def setUp(self):
        import review
        self.review = review
        self._saved = {name: getattr(review, name)
                       for name in ("OUTPUTS", "LOGS", "LEDGER", "GAP_LOG")}
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger"):
            (base / name).mkdir()
        review.OUTPUTS = base / "outputs"
        review.LOGS = base / "logs"
        review.LEDGER = base / "ledger"
        review.GAP_LOG = base / "state" / "gap_log.jsonl"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.review, name, value)
        self.tmp.cleanup()

    def test_report_carries_verifiable_evidence(self):
        report = self.review.run_one(
            ROOT / TARGET, self.review.load_rules(), use_llm=False)
        self.assertEqual(report["schema_version"], "1.2")
        evidence = report["evidence"]
        self.assertTrue(evidence, "报告里没有任何证据对象")

        by_issue = {row["issue_id"]: row for row in evidence}
        for issue in report["issues"]:
            self.assertIn(issue["issue_id"], by_issue, "有结论找不到自己的证据")
            row = by_issue[issue["issue_id"]]
            self.assertEqual(row["quote"], issue["quote"])
            self.assertIn(row["verification"], {"anchored", "placeholder"})
            if row["verification"] == "anchored":
                self.assertIn(row["quote"], report["target_text"],
                              "自称已锚定，却回查不到原文")
        self.assertEqual(len(by_issue), len(report["issues"]))

    def test_issue_fields_are_unchanged(self):
        report = self.review.run_one(
            ROOT / TARGET, self.review.load_rules(), use_llm=False)
        for issue in report["issues"]:
            # 新增报告字段不允许顺手改既有字段
            for key in ("issue_id", "dimension", "severity", "quote", "location",
                        "rule_id", "rule_text", "evidence", "tool", "confidence",
                        "suggestion", "open_discovery"):
                self.assertIn(key, issue)


if __name__ == "__main__":
    unittest.main()
