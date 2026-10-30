#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_evidence_node.py —— Evidence Analyzer 节点的独立验收。

S4 的规则本身只有一句「非空 quote 必须逐字回查原文」，但它是**报告可信度的安全线**：
判松了，伪造的引用会带着「有原文依据」的标签进入报告；判严了，缺失型问题会被当垃圾丢掉。
所以这里分三层测：

    1. 规则（verify_quote）：四类归一——空 / 命中 / 占位 / 不可回溯
    2. 节点（EvidenceAnalyzerNode）：阶段校验、缺原文即失败、产物形态、证据归属
    3. 历史等价（重放）：207 份报告的每条 quote，新规则的丢弃集合与旧实现逐条一致

历史语料只在本地有（outputs/ 被 .gitignore 排除）；CI 跑前两层。
"""
import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (                       # noqa: E402
    STAGE_ORDER,
    Finding,
    FindingOrigin,
    InvariantViolation,
    Location,
    ReviewProfile,
    RuleRef,
    Severity,
    Stage,
    Verification,
)
from review_agent.runtime import NodeContext, NodeResult, RunState, advance  # noqa: E402
from review_agent.runtime.nodes import (                # noqa: E402
    EvidenceAnalyzerNode,
    anchor_findings,
    verify_quote,
)

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"
TEXT = "截止目前，项目已顺利完成 20 个里程碑节点，剩余 3 个待验收。"


def finding(seq, quote, *, run_id=RUN_ID, severity=Severity.MINOR, statement=""):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=run_id, dimension="compliance",
        severity=severity, quote=quote, location=Location("第 1 句", 1),
        confidence=0.8, origin=FindingOrigin.RULE, statement=statement,
        rule=RuleRef(rule_id="COMP-004", dimension="compliance",
                     text="披露具体数据须标注口径与来源"),
    )


class VerifyQuoteTests(unittest.TestCase):
    def test_empty_quote_is_missing_not_unanchored(self):
        self.assertIs(verify_quote("", TEXT), Verification.NOT_CHECKED)
        self.assertIs(verify_quote(None, TEXT), Verification.NOT_CHECKED)

    def test_literal_quote_is_anchored(self):
        self.assertIs(verify_quote("剩余 3 个待验收", TEXT), Verification.ANCHORED)

    def test_placeholder_is_kept(self):
        self.assertIs(verify_quote("（文档首段）", TEXT), Verification.PLACEHOLDER)

    def test_invented_quote_is_unanchored(self):
        self.assertIs(verify_quote("项目已完成 42 个里程碑节点", TEXT),
                      Verification.UNANCHORED)

    def test_placeholder_that_exists_in_text_is_anchored(self):
        # 顺序有意义：能逐字命中就算可回溯，不因为长得像占位就降级。
        self.assertIs(verify_quote("（文档首段）", "开头（文档首段）说明"),
                      Verification.ANCHORED)


class AnchorFindingsTests(unittest.TestCase):
    def setUp(self):
        self.findings = [
            finding(1, "剩余 3 个待验收"),        # anchored
            finding(2, "（文档首段）"),           # placeholder
            finding(3, "项目已完成 42 个里程碑"),  # dropped
            finding(4, "", statement="原文缺少争议解决条款"),  # missing
        ]
        self.result = anchor_findings(self.findings, TEXT, run_id=RUN_ID)

    def test_classification(self):
        self.assertEqual(self.result.dropped_ids, ("ISS-0003",))
        self.assertEqual(self.result.anchored_ids, ("ISS-0001",))
        self.assertEqual(self.result.placeholder_ids, ("ISS-0002",))
        self.assertEqual([f.id for f in self.result.missing], ["ISS-0004"])
        self.assertEqual(self.result.traceable_count, 1)

    def test_evidence_ids_are_sequential_and_stable(self):
        self.assertEqual([e.id for e in self.result.evidence], ["EV-0001", "EV-0002"])
        again = anchor_findings(self.findings, TEXT, run_id=RUN_ID)
        self.assertEqual([e.id for e in again.evidence],
                         [e.id for e in self.result.evidence])

    def test_evidence_points_back_to_its_finding(self):
        self.assertEqual([e.finding_ids for e in self.result.evidence],
                         [("ISS-0001",), ("ISS-0002",)])

    def test_only_anchored_evidence_records_verifier(self):
        by_id = {e.finding_ids[0]: e for e in self.result.evidence}
        self.assertEqual(by_id["ISS-0001"].verified_by, "review.s4.evidence_anchor")
        self.assertIsNone(by_id["ISS-0002"].verified_by)

    def test_first_evidence_seq_offsets_numbering(self):
        result = anchor_findings([finding(1, "剩余 3 个待验收")], TEXT,
                                 run_id=RUN_ID, first_evidence_seq=7)
        self.assertEqual(result.evidence[0].id, "EV-0007")


def evidence_state(findings, *, text=TEXT, stage=Stage.EVIDENCE):
    done = STAGE_ORDER[:STAGE_ORDER.index(stage)]
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=stage,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
        completed_stages=done, findings=tuple(findings),
        artifacts={"target_text": text} if text is not None else {},
    )


class EvidenceNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_node_returns_evidence_and_drop_list(self):
        state = evidence_state([finding(1, "剩余 3 个待验收"),
                                finding(2, "项目已完成 42 个里程碑")])
        result = await EvidenceAnalyzerNode().run(NodeContext(state=state))
        self.assertTrue(result.success)
        self.assertEqual([e.finding_ids[0] for e in result.evidence], ["ISS-0001"])
        dropped = dict(result.output)["evidence_dropped"]
        self.assertEqual([d["finding_id"] for d in dropped], ["ISS-0002"])
        self.assertTrue(result.warnings, "丢条目必须给出可见提示")

    async def test_missing_target_text_fails_loudly(self):
        state = evidence_state([finding(1, "剩余 3 个待验收")], text=None)
        result = await EvidenceAnalyzerNode().run(NodeContext(state=state))
        self.assertFalse(result.success)
        self.assertIn("target_text", "；".join(result.errors))

    async def test_wrong_stage_is_rejected(self):
        state = evidence_state([], stage=Stage.ANALYSIS)
        result = await EvidenceAnalyzerNode().run(NodeContext(state=state))
        self.assertFalse(result.success)
        self.assertFalse(result.retryable)


class AdvanceIntegrationTests(unittest.TestCase):
    def _advance(self, state, result):
        return advance(state, Stage.EVIDENCE, result)

    def test_evidence_enters_state_and_advances(self):
        state = evidence_state([finding(1, "剩余 3 个待验收")])
        result = asyncio.run(EvidenceAnalyzerNode().run(NodeContext(state=state)))
        nxt = self._advance(state, result)
        self.assertIs(nxt.stage, Stage.CROSS_REVIEW)
        self.assertEqual(len(nxt.evidence), 1)
        self.assertIn("evidence_dropped", nxt.artifacts)

    def test_evidence_for_unknown_finding_is_rejected(self):
        state = evidence_state([finding(1, "剩余 3 个待验收")])
        result = asyncio.run(EvidenceAnalyzerNode().run(NodeContext(state=state)))
        # 把证据指向一条不存在的发现：报告上会变成「有证据但点不开」
        bogus = replace(result.evidence[0], finding_ids=("ISS-9999",))
        with self.assertRaises(InvariantViolation):
            self._advance(state, NodeResult.ok(evidence=(bogus,),
                                               output={"evidence_dropped": ()}))


def historical_reports():
    outputs = ROOT / "outputs"
    if not outputs.is_dir():
        return []
    out = []
    for path in sorted(outputs.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "review_id" in data and isinstance(
                data.get("verdict"), str):
            out.append(data)
    return out


@unittest.skipUnless(historical_reports(), "outputs/ 不存在（CI 无历史语料）")
class HistoricalEquivalenceTests(unittest.TestCase):
    """新规则必须和旧实现逐条等价——包括「空 quote 保留」这条容易写错的边界。"""

    @staticmethod
    def legacy_dropped(quote, text):
        """旧实现 review.py S4（改造前）的原样规则，独立重写一遍用于对照。"""
        q = quote
        placeholder = isinstance(q, str) and q.startswith("（") and q.endswith("）")
        return bool(q) and q not in text and not placeholder

    def test_dropped_set_matches_old_implementation(self):
        from review_agent.adapters.issue_codec import encode_issues

        checked = 0
        for report in historical_reports():
            text = str(report.get("target_text") or "")
            issues = report.get("issues") or []
            findings = [e.finding for e in encode_issues(issues, run_id=RUN_ID)]
            result = anchor_findings(findings, text, run_id=RUN_ID)
            expected = {
                f.id for f, raw in zip(findings, issues)
                if self.legacy_dropped(raw.get("quote"), text)
            }
            self.assertEqual(set(result.dropped_ids), expected,
                             f"{report['review_id']} 丢弃集合与旧实现不一致")
            checked += len(findings)
        self.assertGreater(checked, 0, "历史语料里一条 issue 都没有")


if __name__ == "__main__":
    unittest.main()
