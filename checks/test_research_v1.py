#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_research_v1.py —— 库外事实核验接入 v1（批次 2 / A2）的验收。

`research` 是 `unsupported_blocks` 里最后一项。它之所以留到最后，不是"没人做"，
而是它要把三件事同时接对，任何一件错了报告都会"看起来正常"：

    1. **三态语义**：supported 不进发现、refuted 进发现 + 证据、unverified
       既不进发现也不能被读成"没问题"（它只是"查过、没找到"）；
    2. **严重度与来源强度**：两个独立来源/官方域 → major；单一来源 → minor
       且进 `needs_confirm`（口径不同就可能出现差异，不能替用户拍板）；
    3. **证据形态**：`EXTERNAL_SOURCE` + `source_ref`（来源 URL）——这是全仓
       第一次真正产出外部来源证据。

另外两组用例守着**协议边界**：
    · `research.web` 是 `Permission.RESEARCH`：缺省策略不授予，不显式开就调不到；
    · 网页内容是 `untrusted=True`，`official` 标记要按白名单二次校验；
    · 证据编号必须是 Run 级资源：analysis 阶段先发了 EV-0001，
      evidence 阶段不能又发一条 EV-0001。

不联网：一律注入假执行器 / 假 verify。真实检索的判定边界在 `tests/test_research.py`。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import research                                           # noqa: E402
import review                                              # noqa: E402
from review_agent.application import (                     # noqa: E402
    UNSUPPORTED_BLOCKS,
    build_shadow_report,
    render_shadow_md,
)
from review_agent.capabilities import (                     # noqa: E402
    Permission,
    ResearchClaimArgs,
    ResearchWebTool,
    ToolContext,
    ToolRegistry,
    ToolPermissionError,
)
from review_agent.domain import (                           # noqa: E402
    STAGE_ORDER,
    Decision,
    Evidence,
    EvidenceKind,
    EvidenceStrength,
    Finding,
    Location,
    ReviewProfile,
    Severity,
    SourceRef,
    Stage,
    Verdict,
    Verification,
)
from review_agent.runtime import NodeResult, RunState, advance  # noqa: E402
from review_agent.runtime.nodes import (                     # noqa: E402
    ClaimVerdict,
    FactCheckNode,
    ResearchClaim,
    ResearchOutcome,
    research_limitations,
)
from review_agent.runtime.node import NodeContext              # noqa: E402

REVIEW_ID = "R-20261101-091"
RUN_ID = f"{REVIEW_ID}-RUN01"
TARGET = ROOT / "targets" / "selftest" / "st21_fact_xiaomi_research.txt"
TEXT = "小米 2024 年全球智能手机出货量为 5 亿台，位居全球第三。"

SOURCES = (
    {"title": "小米 2024 年出货量 1.69 亿部", "url": "https://example.com/a",
     "snippet": "小米 2024 年全球智能手机出货量 1.69 亿部。",
     "official": False, "source": "so360", "display": "1.69亿"},
    {"title": "统计公报", "url": "https://www.gov.cn/b",
     "snippet": "2024 年小米手机出货量 1.69 亿部。",
     "official": True, "source": "so360", "display": "1.69亿"},
)


def claim(**over):
    base = dict(quote=TEXT, entity="小米", metric="出货量", kind="number",
                value=5.0, unit="亿", year=2024, sentence_index=1, raw="5 亿")
    base.update(over)
    return ResearchClaim(**base)


def item_for(status, *, needs_confirm=False, **over):
    item = {
        "quote": TEXT, "entity": "小米", "metric": "出货量", "kind": "number",
        "value": 5.0, "unit": "亿", "status": status,
        "reason": "两个独立来源与原文数值不一致" if status == "refuted" else "",
        "display": "1.69亿" if status == "refuted" else "",
        "query": "小米 出货量 2024", "method": "search", "providers": ["web"],
        "sources": list(SOURCES[:2] if status == "refuted" else []),
    }
    item.update(over)
    if needs_confirm:
        item["needs_confirm"] = True
    return item


def verdict(status="refuted", *, confidence=0.8, needs_confirm=False, **over):
    return ClaimVerdict(
        claim=claim(**over.pop("claim", {})),
        status=status, confidence=confidence, method="search",
        value=1.69e8 if status == "refuted" else None,
        display="1.69亿" if status == "refuted" else "",
        reason="两个独立来源与原文数值不一致" if status == "refuted" else "",
        query="小米 出货量 2024",
        sources=tuple(SOURCES[:2]) if status == "refuted" else (),
        item=item_for(status, needs_confirm=needs_confirm),
    )


def outcome(*verdicts, errors=(), stopped_early=False):
    items = [v.item for v in verdicts]
    block = {
        "enabled": True, "claims_found": len(verdicts), "checked": len(verdicts),
        "supported": [i for i in items if i.get("status") == "supported"],
        "refuted": [i for i in items if i.get("status") == "refuted"],
        "unverified": [i for i in items
                       if i.get("status") not in ("supported", "refuted")],
        "cache_hits": 0, "learned_hits": 0, "local_hits": 0,
        "needs_confirm": [i for i in items if i.get("needs_confirm")],
        "providers": ["local"], "errors": list(errors), "items": items,
        "latency_ms": 12,
    }
    if stopped_early:
        block["stopped_early"] = True
    return ResearchOutcome(block=block, verdicts=tuple(verdicts),
                           errors=tuple(errors))


def state_for_node(*, evidence=(), artifacts=None, config_dims=("fact",)):
    artifacts = {"target_text": TEXT, **(artifacts or {})}
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.ANALYSIS,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1",
                              dimensions=tuple(config_dims)),
        completed_stages=STAGE_ORDER[:3],
        evidence=tuple(evidence),
        artifacts=artifacts,
    )


class _FakeExecutor:
    """可注入的检索执行器：记录被调用次数，返回预置结果。"""

    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.texts = []

    async def __call__(self, text):
        self.calls += 1
        self.texts.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def run_node(node, state, *, config=None, next_seq=1):
    ctx = NodeContext(state=state, config=config or {"dimensions": ["fact"]},
                      next_finding_seq=next_seq)
    return asyncio.run(node.run(ctx))


# ---------------------------------------------------------------------------
# 节点：三态 → 发现 / 证据
# ---------------------------------------------------------------------------
class FactCheckNodeTests(unittest.TestCase):
    def test_authoritative_refutation_becomes_major_finding_with_evidence(self):
        result = run_node(FactCheckNode(_FakeExecutor(outcome(verdict()))),
                          state_for_node())
        self.assertTrue(result.success, result.errors)
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.severity, Severity.MAJOR)
        self.assertEqual(finding.rule.rule_id, "FACT-001")
        self.assertEqual(finding.dimension, "fact")
        self.assertEqual(finding.quote, TEXT)
        self.assertEqual(finding.location.label, "第 1 句")
        self.assertEqual(finding.tool, "research:search")
        self.assertIn("1.69亿", finding.suggestion)

        self.assertEqual(len(result.evidence), 1)
        ev = result.evidence[0]
        self.assertEqual(ev.kind, EvidenceKind.EXTERNAL_SOURCE)
        self.assertEqual(ev.source_ref, "https://example.com/a")
        self.assertEqual(ev.finding_ids, (finding.id,))
        self.assertEqual(ev.verified_by, "research.web")
        self.assertIn("外部检索", ev.note or "")
        # 批次 3 / B1：外部来源必须带 SourceRef（域名/官方域/抓取时间/快照哈希），
        # 否则「外部核验过」无法复核；强度在评估节点（B2）之前保持 UNASSESSED
        self.assertIsNotNone(ev.source)
        self.assertEqual(ev.source.domain, "example.com")
        self.assertFalse(ev.source.official)
        self.assertFalse(ev.source.is_snapshot)
        self.assertEqual(ev.strength, EvidenceStrength.UNASSESSED)

    def test_single_source_refutation_is_minor_and_needs_confirm(self):
        result = run_node(FactCheckNode(_FakeExecutor(
            outcome(verdict(confidence=0.7, needs_confirm=True)))), state_for_node())
        finding = result.findings[0]
        self.assertEqual(finding.severity, Severity.MINOR)
        self.assertLess(finding.confidence, 0.7)
        block = result.output["research"]
        self.assertEqual(len(block["needs_confirm"]), 1)

    def test_supported_and_unverified_create_no_findings(self):
        result = run_node(FactCheckNode(_FakeExecutor(outcome(
            verdict("supported", confidence=0.9),
            verdict("unverified", confidence=0.0)))), state_for_node())
        self.assertEqual(result.findings, ())
        self.assertEqual(result.evidence, ())
        block = result.output["research"]
        self.assertEqual(len(block["supported"]), 1)
        self.assertEqual(len(block["unverified"]), 1)
        # 「未找到」不是失败：节点不该把它报成 error
        self.assertEqual(result.errors, ())

    def test_block_is_stored_verbatim(self):
        payload = outcome(verdict())
        result = run_node(FactCheckNode(_FakeExecutor(payload)), state_for_node())
        self.assertEqual(result.output["research"]["checked"], 1)
        self.assertEqual(result.output["research"]["refuted"], payload.block["refuted"])

    def test_missing_target_text_fails(self):
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.ANALYSIS,
            profile=ReviewProfile(id="a", name="a", version="1"),
            completed_stages=STAGE_ORDER[:3], artifacts={})
        result = run_node(FactCheckNode(_FakeExecutor(outcome())), state)
        self.assertFalse(result.success)
        self.assertIn("target_text", result.errors[0])

    def test_dimension_gate_skips_network(self):
        executor = _FakeExecutor(outcome(verdict()))
        state = state_for_node(config_dims=("safety",))
        result = run_node(FactCheckNode(executor), state,
                          config={"dimensions": ["safety"]})
        self.assertTrue(result.success)
        self.assertEqual(executor.calls, 0, "维度不含 fact 时不该发任何检索请求")
        self.assertFalse(result.output["research"]["enabled"])

    def test_stopped_early_and_errors_become_warnings(self):
        result = run_node(FactCheckNode(_FakeExecutor(
            outcome(verdict(), errors=("检索超时",), stopped_early=True))),
            state_for_node())
        self.assertTrue(result.success)
        joined = "；".join(result.warnings)
        self.assertIn("提前结束", joined)
        self.assertIn("检索失败", joined)

    def test_executor_exception_fails_the_node(self):
        result = run_node(FactCheckNode(_FakeExecutor(RuntimeError("网络不可用"))),
                          state_for_node())
        self.assertFalse(result.success)
        self.assertIn("网络不可用", result.errors[0])

    def test_evidence_numbering_continues_after_existing(self):
        existing = Evidence(id="EV-0002", run_id=RUN_ID, quote="x",
                            location=Location("第 1 句", 1))
        result = run_node(FactCheckNode(_FakeExecutor(outcome(verdict()))),
                          state_for_node(evidence=[existing]))
        self.assertEqual(result.evidence[0].id, "EV-0003")

    def test_wrong_stage_fails(self):
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.PLANNING,
            profile=ReviewProfile(id="a", name="a", version="1"),
            completed_stages=STAGE_ORDER[:2], artifacts={})
        result = run_node(FactCheckNode(_FakeExecutor(outcome())), state)
        self.assertFalse(result.success)

    def test_concrete_rewrite_travels_to_report_fix(self):
        """「改成什么」必须传到整改单，否则可改的直接降级成人工改写。"""
        result = run_node(FactCheckNode(_FakeExecutor(outcome(verdict()))),
                          state_for_node())
        finding = result.findings[0]
        fix = result.output["research"]["fixes"][finding.id]
        self.assertTrue(fix["has_rewrite"])
        self.assertEqual(fix["action"], "replace")
        self.assertIn("1.69亿", fix["to"])

        report = build_shadow_report(
            RunState(
                review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.FINALIZE,
                profile=ReviewProfile(id="doc-review", name="审查", version="1"),
                completed_stages=STAGE_ORDER, findings=result.findings,
                evidence=result.evidence,
                decision=Decision(id="DEC-0001", run_id=RUN_ID,
                                  verdict=Verdict.WARN, rationale="1 条 major"),
                artifacts={"target_text": TEXT,
                           "research": result.output["research"]}))
        issue = report["issues"][0]
        self.assertTrue(issue["fix"]["has_rewrite"])
        # 传输字段不能漏进对外的 research 段
        self.assertNotIn("fixes", report["research"])


class EvidenceSequenceTests(unittest.TestCase):
    def test_anchor_starts_after_external_evidence(self):
        """analysis 阶段先发 EV-0001，evidence 阶段必须从 EV-0002 起。"""
        from review_agent.domain import FindingOrigin, RuleRef
        from review_agent.runtime.nodes import EvidenceAnalyzerNode

        external = Evidence(id="EV-0001", run_id=RUN_ID,
                            kind=EvidenceKind.EXTERNAL_SOURCE,
                            quote="外部来源片段", location=Location("第 1 句", 1),
                            source_ref="https://example.com/a",
                            source=SourceRef(url="https://example.com/a"))
        finding = Finding(id="ISS-0001", run_id=RUN_ID, dimension="fact",
                          severity=Severity.MAJOR,
                          quote="小米 2024 年全球智能手机出货量为 5 亿台",
                          location=Location("第 1 句", 1), confidence=0.8,
                          origin=FindingOrigin.RESEARCH,
                          rule=RuleRef(rule_id="FACT-001", dimension="fact"))
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.EVIDENCE,
            profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
            completed_stages=STAGE_ORDER[:4],
            findings=(finding,), evidence=(external,),
            artifacts={"target_text": TEXT})
        result = run_node(EvidenceAnalyzerNode(), state,
                          config={"dimensions": ["fact"]})
        self.assertTrue(result.success, result.errors)
        self.assertEqual([e.id for e in result.evidence], ["EV-0002"])
        self.assertEqual(result.evidence[0].kind, EvidenceKind.QUOTE)


class ArtifactContractTests(unittest.TestCase):
    def test_research_is_an_allowed_analysis_artifact(self):
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.ANALYSIS,
            profile=ReviewProfile(id="doc-review", name="审查", version="1"),
            completed_stages=STAGE_ORDER[:3],
            artifacts={"target_text": TEXT, "analysis_stats": {},
                       "semantic_stats": {}})
        nxt = advance(state, Stage.ANALYSIS,
                      NodeResult.ok(output={"research": {"enabled": True}}))
        self.assertEqual(nxt.artifacts["research"], {"enabled": True})

    def test_unregistered_artifact_still_rejected(self):
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.ANALYSIS,
            profile=ReviewProfile(id="doc-review", name="审查", version="1"),
            completed_stages=STAGE_ORDER[:3],
            artifacts={"target_text": TEXT, "analysis_stats": {},
                       "semantic_stats": {}})
        with self.assertRaises(Exception):
            advance(state, Stage.ANALYSIS, NodeResult.ok(output={"research_x": 1}))


# ---------------------------------------------------------------------------
# 工具：权限 / 白名单 / 不可信输入
# ---------------------------------------------------------------------------
class ResearchToolTests(unittest.TestCase):
    def _tool(self, verdict_payload, **kwargs):
        return ResearchWebTool(verify=lambda claim: verdict_payload, **kwargs)

    def _context(self, **kwargs):
        base = dict(review_id=REVIEW_ID, run_id=RUN_ID, actor="pipeline")
        base.update(kwargs)
        return ToolContext(**base)

    def test_research_permission_is_not_granted_by_default(self):
        registry = ToolRegistry()
        registry.register(self._tool({"status": "unverified", "sources": []}))
        args = {"quote": TEXT, "entity": "小米", "metric": "出货量"}
        with self.assertRaises(ToolPermissionError):
            asyncio.run(registry.execute("research.web", args, self._context()))
        # 显式授予后才可执行
        result, _ = asyncio.run(registry.execute(
            "research.web", args,
            self._context(permissions=frozenset({Permission.RESEARCH}))))
        self.assertTrue(result.ok)

    def test_success_marks_result_untrusted(self):
        tool = self._tool({"status": "unverified", "confidence": 0.0, "sources": []})
        result = asyncio.run(tool.execute(
            ResearchClaimArgs(quote=TEXT, entity="小米"), self._context()))
        self.assertTrue(result.ok)
        self.assertTrue(result.untrusted, "网页内容必须标记为不可信输入")

    def test_unlisted_official_source_is_downgraded(self):
        tool = self._tool({"status": "refuted", "confidence": 0.9,
                           "sources": [{"title": "自称官网", "url": "https://evil.example.com/x",
                                        "official": True, "snippet": "x"}]},
                          official_domains=("gov.cn",))
        result = asyncio.run(tool.execute(
            ResearchClaimArgs(quote=TEXT, entity="小米"), self._context()))
        source = result.data["sources"][0]
        self.assertFalse(source["official"])
        self.assertTrue(source["official_downgraded"])
        self.assertTrue(any("白名单" in w for w in result.warnings))

    def test_listed_official_source_keeps_flag(self):
        tool = self._tool({"status": "refuted", "confidence": 0.9,
                           "sources": [{"title": "统计公报", "url": "https://www.gov.cn/a",
                                        "official": True, "snippet": "x"}]},
                          official_domains=("gov.cn",))
        result = asyncio.run(tool.execute(
            ResearchClaimArgs(quote=TEXT, entity="小米"), self._context()))
        self.assertTrue(result.data["sources"][0]["official"])

    def test_invalid_status_is_rejected(self):
        tool = self._tool({"status": "maybe", "sources": []})
        result = asyncio.run(tool.execute(
            ResearchClaimArgs(quote=TEXT, entity="小米"), self._context()))
        self.assertFalse(result.ok)
        self.assertIn("非法", result.errors[0])

    def test_conclusion_without_sources_is_rejected(self):
        tool = self._tool({"status": "refuted", "confidence": 0.9, "sources": []})
        result = asyncio.run(tool.execute(
            ResearchClaimArgs(quote=TEXT, entity="小米"), self._context()))
        self.assertFalse(result.ok)
        self.assertIn("来源", result.errors[0])

    def test_missing_entity_is_rejected(self):
        tool = self._tool({"status": "unverified", "sources": []})
        result = asyncio.run(tool.execute(
            ResearchClaimArgs(quote=TEXT), self._context()))
        self.assertFalse(result.ok)
        self.assertIn("entity", result.errors[0])


# ---------------------------------------------------------------------------
# 报告：research 段 / 证据字段 / 边界
# ---------------------------------------------------------------------------
class ShadowReportResearchTests(unittest.TestCase):
    def _state(self, research=None):
        artifacts = {"target_text": TEXT}
        if research is not None:
            artifacts["research"] = research
        return RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.FINALIZE,
            profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
            completed_stages=STAGE_ORDER,
            decision=Decision(id="DEC-0001", run_id=RUN_ID, verdict=Verdict.WARN,
                              rationale="存在 1 条 major"),
            artifacts=artifacts)

    def test_unsupported_blocks_is_empty(self):
        self.assertEqual(UNSUPPORTED_BLOCKS, ())
        report = build_shadow_report(self._state())
        self.assertEqual(report["unsupported_blocks"], [])

    def test_research_block_comes_from_state(self):
        report = build_shadow_report(self._state(
            {"enabled": True, "checked": 1, "supported": [], "refuted": [{"a": 1}],
             "unverified": [], "needs_confirm": [], "items": [], "claims_found": 1}))
        self.assertTrue(report["research"]["enabled"])
        self.assertEqual(len(report["research"]["refuted"]), 1)
        self.assertTrue(any("库外事实核验" in item for item in report["limitations"]))

    def test_missing_research_is_declared_not_hidden(self):
        report = build_shadow_report(self._state())
        self.assertIsNone(report["research"])
        self.assertTrue(any("未做库外事实核验" in item
                            for item in report["limitations"]))

    def test_evidence_rows_carry_kind_and_source(self):
        from review_agent.domain import FindingOrigin
        ev = Evidence(id="EV-0001", run_id=RUN_ID,
                      kind=EvidenceKind.EXTERNAL_SOURCE, quote="来源片段",
                      location=Location("第 1 句", 1),
                      source_ref="https://example.com/a", note="外部检索：refuted",
                      source=SourceRef(url="https://example.com/a", official=True),
                      verified_by="research.web", finding_ids=("ISS-0001",))
        finding = Finding(id="ISS-0001", run_id=RUN_ID, dimension="fact",
                          severity=Severity.MAJOR, quote=TEXT,
                          location=Location("第 1 句", 1), confidence=0.8,
                          origin=FindingOrigin.RESEARCH)
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.FINALIZE,
            profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
            completed_stages=STAGE_ORDER, findings=(finding,), evidence=(ev,),
            decision=Decision(id="DEC-0001", run_id=RUN_ID, verdict=Verdict.WARN,
                              rationale="存在 1 条 major"),
            artifacts={"target_text": TEXT})
        report = build_shadow_report(state)
        row = report["evidence"][0]
        self.assertEqual(row["kind"], "external_source")
        self.assertEqual(row["source_ref"], "https://example.com/a")
        self.assertEqual(row["note"], "外部检索：refuted")
        self.assertEqual(row["issue_id"], "ISS-0001")

    def test_contradicted_quote_row_carries_the_evidence_id(self):
        """反证要能从报告读出来：强度 + 指名的外部证据编号（决策书 4.4）。"""
        from review_agent.domain import FindingOrigin
        quote = Evidence(id="EV-0001", run_id=RUN_ID,
                         kind=EvidenceKind.QUOTE, quote=TEXT,
                         location=Location("第 1 句", 1, 0, len(TEXT)),
                         verification=Verification.ANCHORED,
                         verified_by="review.s4.evidence_anchor",
                         strength=EvidenceStrength.CONTRADICTED,
                         contradicted_by=("EV-0002",),
                         finding_ids=("ISS-0001",))
        source = Evidence(id="EV-0002", run_id=RUN_ID,
                          kind=EvidenceKind.EXTERNAL_SOURCE, quote="来源片段",
                          location=Location("第 1 句", 1),
                          source_ref="https://example.com/a",
                          source=SourceRef(url="https://example.com/a"),
                          verified_by="research.web", finding_ids=("ISS-0001",))
        finding = Finding(id="ISS-0001", run_id=RUN_ID, dimension="fact",
                          severity=Severity.MAJOR, quote=TEXT,
                          location=Location("第 1 句", 1), confidence=0.8,
                          origin=FindingOrigin.RESEARCH)
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.FINALIZE,
            profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
            completed_stages=STAGE_ORDER, findings=(finding,),
            evidence=(quote, source),
            decision=Decision(id="DEC-0001", run_id=RUN_ID, verdict=Verdict.WARN,
                              rationale="存在 1 条 major"),
            artifacts={"target_text": TEXT})
        report = build_shadow_report(state)
        rows = {row["evidence_id"]: row for row in report["evidence"]}
        self.assertEqual(rows["EV-0001"]["strength"], "contradicted")
        self.assertEqual(rows["EV-0001"]["contradicted_by"], ["EV-0002"])
        self.assertEqual(rows["EV-0001"]["verification"], "anchored")
        self.assertIn("反证 EV-0002", render_shadow_md(report))

    def test_markdown_renders_research_section(self):
        report = build_shadow_report(self._state(
            {"enabled": True, "checked": 1, "claims_found": 1,
             "supported": [], "unverified": [], "needs_confirm": [],
             "refuted": [item_for("refuted")],
             "items": [item_for("refuted")]}))
        text = render_shadow_md(report)
        self.assertIn("库外事实核验", text)
        self.assertIn("example.com/a", text)

    def test_limitations_cover_stopped_and_needs_confirm(self):
        limits = research_limitations({
            "enabled": True, "checked": 1, "claims_found": 3,
            "supported": [], "refuted": [item_for("refuted", needs_confirm=True)],
            "unverified": [], "needs_confirm": [item_for("refuted", needs_confirm=True)],
            "errors": ["检索超时"], "stopped_early": True, "items": [],
        })
        joined = "；".join(limits)
        self.assertIn("提前结束", joined)
        self.assertIn("待人工确认", joined)
        self.assertIn("部分检索失败", joined)


# ---------------------------------------------------------------------------
# 与旧链路的一致性：同一条判定链、同一个报告块形状
# ---------------------------------------------------------------------------
class ParityTests(unittest.TestCase):
    def test_legacy_and_v1_blocks_match(self):
        fixture = {
            "status": "refuted", "confidence": 0.8, "value": 169000000.0,
            "display": "1.69亿", "reason": "两个独立来源与原文数值不一致",
            "query": "小米 出货量 2024", "providers": ["web"],
            "sources": list(SOURCES),
        }
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        learned = base / "learned.jsonl"
        cache_dir = base / "cache"
        sentences = list(review.iter_sentences(TEXT))
        with mock.patch.object(research, "verify", return_value=fixture), \
             mock.patch.object(research, "local_verdict", return_value=None), \
             mock.patch.object(research, "learned_lookup", return_value=None), \
             mock.patch.object(research, "cache_get", return_value=None), \
             mock.patch.object(research, "cache_put"), \
             mock.patch.object(research, "learn_verdict", return_value=False), \
             mock.patch.object(research, "LEARNED", learned), \
             mock.patch.object(research, "CACHE_DIR", cache_dir):
            session = review.Session(REVIEW_ID, "targets/x.txt", "llm_response")
            legacy = research.review_claims(
                session, TEXT, sentences, [], enabled=True, web=False)
            executor = review.make_research_executor(
                review_id=REVIEW_ID, run_id=RUN_ID, use_tool=False,
                record_egress=False, web=False)
            v1 = asyncio.run(executor(TEXT))

        for key in ("enabled", "claims_found", "checked", "supported", "refuted",
                    "unverified", "needs_confirm", "providers", "errors", "items"):
            self.assertEqual(legacy[key], dict(v1.block)[key], f"块字段 {key} 不一致")
        self.assertTrue(legacy["refuted"], "样本没有触发反驳，这条用例退化成比空")
        self.assertEqual(len(legacy["items"][0]["sources"]), 2)

    def test_v1_executor_marks_needs_confirm_like_legacy(self):
        fixture = {
            "status": "refuted", "confidence": 0.7, "value": 169000000.0,
            "display": "1.69亿", "reason": "单一来源", "query": "小米",
            "providers": ["web"],
            "sources": [SOURCES[0]],
        }
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(research, "verify", return_value=fixture), \
             mock.patch.object(research, "local_verdict", return_value=None), \
             mock.patch.object(research, "learned_lookup", return_value=None), \
             mock.patch.object(research, "cache_get", return_value=None), \
             mock.patch.object(research, "cache_put"), \
             mock.patch.object(research, "learn_verdict", return_value=False), \
             mock.patch.object(research, "LEARNED", Path(tmp.name) / "l.jsonl"), \
             mock.patch.object(research, "CACHE_DIR", Path(tmp.name) / "c"):
            result = asyncio.run(review.make_research_executor(
                review_id=REVIEW_ID, run_id=RUN_ID, use_tool=False,
                record_egress=False, web=False)(TEXT))
        self.assertEqual(len(result.block["needs_confirm"]), 1)
        self.assertFalse(result.verdicts[0].authoritative)


class LocalSourceParityTests(unittest.TestCase):
    """用**真实本地来源库**（`knowledge/sources/`，小米 1.69 亿 × 2 个独立来源）
    跑两条链路，逐项比对——这一条才是「搬过去了」的实质证据：

    * `research` 段逐字段一致（checked / refuted / 分类 / 来源 URL / needs_confirm）
    * 产生的 `FACT-001` 发现与旧 issue 的规则、严重度、置信度、一句话建议一致
    * 整改单里的改写建议一致（改成 1.69亿，而不是「请补口径」）

    不联网：本地来源命中即返回（`local_verdict` 优先级最高）。
    """

    def setUp(self):
        if not TARGET.is_file():
            self.skipTest(f"缺测试语料：{TARGET.relative_to(ROOT)}")
        if not (ROOT / "knowledge" / "sources").is_dir():
            self.skipTest("缺本地来源库 knowledge/sources/")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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
        self.rules = review.load_rules()
        # 缓存/已学落临时目录：核验会写这两处，不能污染真实 state/
        for mod_name, attr, path in (
                (research, "CACHE_DIR", self.base / "cache"),
                (research, "LEARNED", self.base / "learned.jsonl")):
            patcher = mock.patch.object(mod_name, attr, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = {"providers": ["local"], "max_claims_per_doc": 3,
                    "max_seconds": 30, "official_domains": ["gov.cn"],
                    "max_results": 8, "timeout_seconds": 5}

    def _restore(self):
        for name, value in self.saved.items():
            setattr(review, name, value)

    def _v1(self):
        state, outcome, session = review.run_typed_pipeline(
            Path(str(TARGET)), self.rules, review_id=REVIEW_ID, use_llm=False,
            research_enabled=True, research_cfg=dict(self.cfg))
        self.assertTrue(outcome.ok, outcome.errors)
        return state, session

    def _legacy(self):
        with mock.patch.object(research, "load_config",
                               return_value=dict(self.cfg)):
            return review.run_one(Path(str(TARGET)), self.rules, use_llm=False,
                                  research_enabled=True)

    def test_research_block_matches_legacy(self):
        legacy = self._legacy()
        state, _session = self._v1()
        old = legacy.get("research") or {}
        new = dict(state.artifacts.get("research") or {})
        self.assertTrue(old.get("enabled"), "旧链路没有执行核验，比对无意义")
        self.assertTrue(new.get("enabled"))
        for key in ("enabled", "claims_found", "checked", "cache_hits",
                    "learned_hits", "local_hits"):
            self.assertEqual(old.get(key), new.get(key), f"research.{key} 不一致")
        for key in ("supported", "refuted", "unverified", "needs_confirm"):
            self.assertEqual(len(old.get(key) or []), len(new.get(key) or []),
                             f"research.{key} 条数不一致")
        old_ref = (old.get("refuted") or [{}])[0]
        new_ref = (new.get("refuted") or [{}])[0]
        for key in ("quote", "entity", "metric", "status", "display", "method"):
            self.assertEqual(old_ref.get(key), new_ref.get(key),
                             f"refuted[0].{key} 不一致")
        self.assertEqual([s.get("url") for s in old_ref.get("sources") or []],
                         [s.get("url") for s in new_ref.get("sources") or []],
                         "来源顺序/URL 必须一致")

    def test_finding_matches_legacy_issue(self):
        legacy = self._legacy()
        state, _session = self._v1()
        old = [i for i in legacy["issues"] if i["rule_id"] == "FACT-001"]
        new = [f for f in state.findings
               if f.rule is not None and f.rule.rule_id == "FACT-001"]
        self.assertEqual(len(old), 1)
        self.assertEqual(len(new), 1)
        self.assertEqual(old[0]["severity"], new[0].severity.value)
        self.assertEqual(old[0]["quote"], new[0].quote)
        self.assertEqual(round(float(old[0]["confidence"]), 4),
                         round(float(new[0].confidence), 4))
        self.assertEqual(old[0]["suggestion"], new[0].suggestion)
        self.assertEqual(old[0]["location"], new[0].location.label)

    def test_rewrite_fix_matches_legacy(self):
        legacy = self._legacy()
        state, _session = self._v1()
        old = next(i for i in legacy["issues"] if i["rule_id"] == "FACT-001")
        self.assertTrue(old["fix"]["has_rewrite"])
        report = build_shadow_report(
            state, target_meta=state.artifacts.get("target_meta"), logs_hash=None)
        issue = report["issues"][0]
        self.assertEqual(issue["fix"]["action"], old["fix"]["action"])
        self.assertEqual(issue["fix"]["to"], old["fix"]["to"])
        self.assertTrue(issue["fix"]["has_rewrite"])


# ---------------------------------------------------------------------------
# 端到端：整条流水线 + 工具审计
# ---------------------------------------------------------------------------
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        if not TARGET.is_file():
            self.skipTest(f"缺测试语料：{TARGET.relative_to(ROOT)}")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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
        self.rules = review.load_rules()

    def _restore(self):
        for name, value in self.saved.items():
            setattr(review, name, value)

    def test_pipeline_runs_research_and_audits_the_tool(self):
        fixture = {
            "status": "refuted", "confidence": 0.9, "value": 169000000.0,
            "display": "1.69亿", "reason": "权威来源与原文数值不一致",
            "query": "小米 出货量 2024", "providers": ["local"],
            "sources": [SOURCES[1]],
        }
        cfg = {"providers": ["local"], "max_claims_per_doc": 3,
               "max_seconds": 30, "official_domains": ["gov.cn"]}
        with mock.patch.object(research, "verify", return_value=fixture), \
             mock.patch.object(research, "local_verdict", return_value=None), \
             mock.patch.object(research, "learned_lookup", return_value=None), \
             mock.patch.object(research, "cache_get", return_value=None), \
             mock.patch.object(research, "cache_put"), \
             mock.patch.object(research, "learn_verdict", return_value=False), \
             mock.patch.object(research, "LEARNED", self.base / "learned.jsonl"), \
             mock.patch.object(research, "CACHE_DIR", self.base / "cache"):
            state, outcome, session = review.run_typed_pipeline(
                Path(str(TARGET)), self.rules, review_id=REVIEW_ID,
                use_llm=False, research_enabled=True, research_cfg=cfg)

        self.assertTrue(outcome.ok, outcome.errors)
        block = state.artifacts.get("research") or {}
        self.assertTrue(block.get("enabled"))
        self.assertEqual(len(block.get("refuted") or []), 1)
        external = [e for e in state.evidence
                    if e.kind is EvidenceKind.EXTERNAL_SOURCE]
        self.assertTrue(external, "v1 必须产出 EXTERNAL_SOURCE 证据")
        self.assertEqual(external[0].source_ref, "https://www.gov.cn/b")
        # 工具调用要留审计：否则「谁授权出网」查不到
        self.assertTrue(any(i["tool"] == "research.web"
                            for i in session.tool_invocations))
        self.assertTrue(all(i["permission"] == "research"
                            for i in session.tool_invocations
                            if i["tool"] == "research.web"))
        # 出网留痕（只记元信息）：4.6 起是 attempt + result 两行，按 attempt_id 配对
        egress = self.base / "state" / "egress.log"
        if egress.exists():
            rows = [json.loads(line) for line in
                    egress.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual([r["phase"] for r in rows], ["attempt", "result"])
            self.assertEqual(rows[0]["attempt_id"], rows[1]["attempt_id"])
            for record in rows:
                self.assertEqual(record["kind"], "research")
                self.assertNotIn("出货量为 5 亿台", json.dumps(
                    record, ensure_ascii=False))

        report = build_shadow_report(
            state, target_meta=state.artifacts.get("target_meta"),
            events=outcome.events, checkpoints=outcome.checkpoints,
            logs_hash=None)
        self.assertEqual(report["unsupported_blocks"], [])
        self.assertTrue(report["research"]["enabled"])
        self.assertTrue(any(e["kind"] == "external_source"
                            for e in report["evidence"]))
        self.assertTrue(any("库外事实核验" in item and "检索" in item
                            for item in report["limitations"]))

    def test_research_off_by_default(self):
        state, outcome, _session = review.run_typed_pipeline(
            Path(str(TARGET)), self.rules, review_id=REVIEW_ID, use_llm=False)
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertIsNone(state.artifacts.get("research"))
        report = build_shadow_report(state, logs_hash=None)
        self.assertTrue(any("未做库外事实核验" in item
                            for item in report["limitations"]))


if __name__ == "__main__":
    unittest.main()
