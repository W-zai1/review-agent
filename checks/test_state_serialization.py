#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_state_serialization.py —— RunState 存盘 / 读回 / 续跑。

「能否在中断后恢复执行」这条验收，卡点不在编排器，而在**状态能不能重建**：
检查点只有阶段与哈希，进程一重启状态就没了，恢复只能演示不能交付。

验收四条：
    1. 往返一致：`state_to_dict` 的规范化形式在 save → load 之后完全相同
    2. 可 JSON 化：状态里不许混进 Path / 自定义对象（进了就直接报错，不留半个文件）
    3. 脏数据早失败：schema 版本不对、枚举值不认识，在构造处就报，不往下游传
    4. 续跑：存下前三个阶段的 RunState，读回后从 analysis 继续到最后
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (                       # noqa: E402
    STAGE_ORDER,
    Decision,
    Dispute,
    DisputeKind,
    Evidence,
    EvidenceKind,
    Finding,
    FindingOrigin,
    FindingStatus,
    Location,
    ReviewProfile,
    RiskLevel,
    RuleRef,
    Severity,
    Stage,
    Verdict,
    Verification,
)
from review_agent.runtime import (                      # noqa: E402
    FileRunStore,
    NodeContext,
    NodeResult,
    Orchestrator,
    RunState,
    Workflow,
    state_from_dict,
    state_to_dict,
)

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"


def rich_state():
    finding = Finding(
        id="ISS-0001", run_id=RUN_ID, dimension="compliance",
        severity=Severity.MAJOR, quote="截止目前，项目已完成 20 个里程碑节点",
        location=Location("第 5 句", 5, 120, 140), confidence=0.75,
        origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id="COMP-004", dimension="compliance",
                     text="披露具体数据须标注口径", declared_severity=Severity.MAJOR),
        statement="含具体数字但无口径", confidence_basis=None,
        status=FindingStatus.NEEDS_CONFIRMATION, tool="regex",
        suggestion="补口径", evidence_ids=("EV-0001",), severity_note="降级说明",
    )
    evidence = Evidence(
        id="EV-0001", run_id=RUN_ID, kind=EvidenceKind.QUOTE,
        quote=finding.quote, location=Location("第 5 句", 5),
        verification=Verification.ANCHORED, verified_by="review.s4.evidence_anchor",
        note="备注", finding_ids=("ISS-0001",),
    )
    dispute = Dispute(
        id="DSP-0001", run_id=RUN_ID, point="同一句两个维度定级不一致",
        issue_ids=("ISS-0001",), kind=DisputeKind.SEVERITY_CONFLICT,
        between=("fact-reviewer", "safety-reviewer"),
        resolution_hint="交人工裁定",
    )
    decision = Decision(
        id="DEC-0001", run_id=RUN_ID, verdict=Verdict.WARN,
        rationale="存在 1 条 major", risk_level=RiskLevel.MODERATE,
        reasons=("无 blocker，但存在 1 条 major",), rule_refs=("COMP-004",),
        finding_ids=("ISS-0001",), requires_human_review=True,
    )
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.DECISION,
        profile=ReviewProfile(
            id="doc-review", name="文档审查", version="1;scenario:x@1",
            description="说明", target_types=("docx", "md"),
            dimensions=("compliance", "fact"), rule_ids=("COMP-004",),
            confidence_threshold=0.7, fail_on_blocker=True,
            require_human_review=False, source="manifest", rationale="理由"),
        completed_stages=STAGE_ORDER[:STAGE_ORDER.index(Stage.DECISION)],
        findings=(finding,), evidence=(evidence,), disputes=(dispute,),
        decision=decision,
        artifacts={"target_text": "原文", "plan": {"dimensions": ["compliance"]},
                   "analysis_stats": {"total_findings": 1},
                   "evidence_dropped": [{"finding_id": "ISS-0002"}]},
        warnings=("一条警告",), attempt=2,
    )


class RoundTripTests(unittest.TestCase):
    def test_canonical_form_survives(self):
        original = rich_state()
        restored = state_from_dict(json.loads(json.dumps(state_to_dict(original))))
        self.assertEqual(state_to_dict(restored), state_to_dict(original))

    def test_restored_state_equals_original(self):
        original = rich_state()
        restored = state_from_dict(json.loads(json.dumps(state_to_dict(original))))
        self.assertEqual(restored.findings, original.findings)
        self.assertEqual(restored.evidence, original.evidence)
        self.assertEqual(restored.disputes, original.disputes)
        self.assertEqual(restored.decision, original.decision)
        self.assertEqual(restored.profile, original.profile)
        self.assertEqual(restored.completed_stages, original.completed_stages)
        self.assertEqual(restored.stage, original.stage)

    def test_payload_is_json_serializable(self):
        json.dumps(state_to_dict(rich_state()), ensure_ascii=False)

    def test_artifacts_must_be_serializable(self):
        state = rich_state()
        with self.assertRaises(TypeError):
            state_to_dict(RunState(
                review_id=state.review_id, run_id=state.run_id,
                stage=Stage.INTAKE, profile=state.profile,
                artifacts={"bad": Path("x")},
            ))

    def test_unknown_schema_is_rejected(self):
        data = state_to_dict(rich_state())
        data["schema_version"] = 99
        with self.assertRaises(ValueError):
            state_from_dict(data)

    def test_unknown_enum_is_rejected(self):
        data = state_to_dict(rich_state())
        data["findings"][0]["severity"] = "critical"
        with self.assertRaises(ValueError):
            state_from_dict(data)


class FileRunStoreTests(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileRunStore(Path(tmp))
            original = rich_state()
            store.save(original)
            self.assertTrue(store.exists(RUN_ID))
            restored = store.load(RUN_ID)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.findings, original.findings)
            # 原子替换：不留下 .tmp 半成品
            self.assertEqual([p.name for p in Path(tmp).iterdir()],
                             [f"{RUN_ID}.state.json"])

    def test_missing_run_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(FileRunStore(Path(tmp)).load("R-20261030-999-RUN01"))


class _Fake:
    def __init__(self, name, stage, produces=(), build=None):
        self.name, self.stage = name, stage
        self.produces = frozenset(produces)
        self._build = build or (lambda ctx: {})
        self.calls = 0

    async def run(self, ctx: NodeContext) -> NodeResult:
        self.calls += 1
        return NodeResult.ok(**self._build(ctx))


def pipeline():
    return [
        _Fake("intake", Stage.INTAKE, {"target_hash"},
              lambda ctx: {"output": {"target_hash": "h"}}),
        _Fake("ingest", Stage.INGESTION, {"target_text", "target_meta"},
              lambda ctx: {"output": {"target_text": "原文", "target_meta": {}}}),
        _Fake("plan", Stage.PLANNING, {"plan"},
              lambda ctx: {"output": {"plan": {"dimensions": ["fact"]}}}),
        _Fake("rule-analyzer", Stage.ANALYSIS, {"findings", "analysis_stats"},
              lambda ctx: {"output": {"analysis_stats": {"rules": 1}}}),
        _Fake("evidence", Stage.EVIDENCE, {"evidence", "evidence_dropped"},
              lambda ctx: {"output": {"evidence_dropped": []}}),
        _Fake("cross", Stage.CROSS_REVIEW, {"disputes"}),
        _Fake("decision", Stage.DECISION, {"decision"},
              lambda ctx: {"decision": Decision(
                  id="DEC-0001", run_id=ctx.state.run_id, verdict=Verdict.PASS,
                  rationale="没有问题")}),
        _Fake("finalize", Stage.FINALIZE, {"report", "audit_export"},
              lambda ctx: {"output": {"report": {"verdict": "pass"},
                                      "audit_export": {}}}),
    ]


class ResumeTests(unittest.TestCase):
    def test_resume_after_reload(self):
        nodes = pipeline()
        partial = Workflow(tuple(nodes[:3]))
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.INTAKE,
            profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
        )
        first = asyncio.run(Orchestrator(partial).run(state))
        self.assertEqual(first.status, "incomplete")
        self.assertIs(first.state.stage, Stage.ANALYSIS)
        before = [node.calls for node in nodes[:3]]

        with tempfile.TemporaryDirectory() as tmp:
            store = FileRunStore(Path(tmp))
            store.save(first.state)
            reloaded = store.load(RUN_ID)

        outcome = asyncio.run(Orchestrator(Workflow(tuple(nodes))).run(reloaded))
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertEqual(outcome.state.completed_stages, STAGE_ORDER)
        self.assertIsNotNone(outcome.state.decision)
        # 前置阶段的节点没有被重跑
        self.assertEqual([node.calls for node in nodes[:3]], before)


if __name__ == "__main__":
    unittest.main()
