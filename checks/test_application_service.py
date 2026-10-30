#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_application_service.py —— Application 层：组装、执行、保存、恢复。

Application 不产生结论，它做四件事：把节点拼成流水线、交给编排器、把状态存下来、
按 run_id 读回来继续。这四条各自都有明确的失败模式（拼错节点、跑完不落盘、
落盘读不回、恢复时重跑已完成阶段），所以逐条测。

用假执行器与假 reader：这里验证的是「组装与生命周期」，不是审查能力本身。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.application import PipelinePorts, ReviewApplication  # noqa: E402
from review_agent.domain import (                       # noqa: E402
    STAGE_ORDER,
    Finding,
    FindingOrigin,
    Location,
    ReviewProfile,
    RuleRef,
    Severity,
    Stage,
)
from review_agent.runtime import (                      # noqa: E402
    FileRunStore,
    NodeContext,
    NodeResult,
    state_to_dict,
)
from review_agent.runtime.nodes import SemanticStepOutcome  # noqa: E402

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"
TEXT = "截止目前，项目已顺利完成 20 个里程碑节点。"


def finding(seq):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=RUN_ID, dimension="fact",
        severity=Severity.MINOR, quote=TEXT, location=Location("第 1 句", 1),
        confidence=0.8, origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id="FACT-001", dimension="fact", text="规则原文"),
    )


class FinalizeNode:
    name = "finalize"
    stage = Stage.FINALIZE
    produces = frozenset({"report", "audit_export"})

    async def run(self, ctx: NodeContext) -> NodeResult:
        return NodeResult.ok(output={
            "report": {"review_id": ctx.state.review_id,
                       "findings": len(ctx.state.findings)},
            "audit_export": {},
        })


def ports(*, rule_executor=None, semantic=None, error=None):
    def default_rule(step):
        if error:
            raise RuntimeError(error)
        return [finding(1)] if step.dimension else []

    return PipelinePorts(
        reader=lambda path: (TEXT, "utf-8", None),
        rule_executor=rule_executor or default_rule,
        finalize_node=FinalizeNode(),
        semantic_executor=semantic,
    )


def profile():
    return ReviewProfile(id="doc-review", name="文档审查", version="1",
                         dimensions=("fact",))


def _canonical(state):
    """按 JSON 规范化后比较：内存里的 tuple 落盘后会变成 list，语义相同。"""
    return json.dumps(state_to_dict(state), ensure_ascii=False,
                      sort_keys=True, default=str)


def config():
    return {"target_path": "targets/a.md", "dimensions": ["fact"],
            "handlers": {"fact": "builtin:fact"}, "checks": []}


class CompositionTests(unittest.TestCase):
    def test_workflow_stages_are_in_order(self):
        app = ReviewApplication()
        workflow = app.workflow(ports())
        self.assertEqual(workflow.stages,
                         (Stage.INGESTION, Stage.PLANNING, Stage.ANALYSIS,
                          Stage.EVIDENCE, Stage.CROSS_REVIEW,
                          Stage.DECISION, Stage.FINALIZE))

    def test_semantic_node_joins_analysis_when_executor_given(self):
        app = ReviewApplication()
        without = app.workflow(ports())
        with_semantic = app.workflow(ports(
            semantic=lambda step, start: SemanticStepOutcome()))
        self.assertEqual(len(without.nodes_for(Stage.ANALYSIS)), 1)
        self.assertEqual(len(with_semantic.nodes_for(Stage.ANALYSIS)), 2)

    def test_initial_state_starts_after_intake(self):
        app = ReviewApplication()
        state = app.initial_state(review_id=REVIEW_ID, run_id=RUN_ID,
                                  profile=profile(), target_hash="a" * 64)
        self.assertIs(state.stage, Stage.INGESTION)
        self.assertEqual(state.completed_stages, (Stage.INTAKE,))
        self.assertEqual(state.artifacts["target_hash"], "a" * 64)


class StartAndPersistTests(unittest.TestCase):
    def test_run_completes_and_persists_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileRunStore(Path(tmp))
            app = ReviewApplication(store=store)
            state = app.initial_state(review_id=REVIEW_ID, run_id=RUN_ID,
                                      profile=profile())
            outcome = asyncio.run(app.run(state, ports(), config=config()))
            self.assertTrue(outcome.ok, outcome.errors)
            self.assertEqual(outcome.state.completed_stages, STAGE_ORDER)
            self.assertEqual(outcome.state.artifacts["report"]["findings"], 1)

            loaded = app.state_of(RUN_ID)
            self.assertIsNotNone(loaded)
            self.assertEqual(_canonical(loaded), _canonical(outcome.state))

    def test_snapshot_is_serializable_view(self):
        app = ReviewApplication()
        state = app.initial_state(review_id=REVIEW_ID, run_id=RUN_ID,
                                  profile=profile())
        snapshot = app.snapshot(state)
        self.assertEqual(snapshot["run_id"], RUN_ID)
        self.assertEqual(snapshot["stage"], "ingestion")

    def test_state_of_unknown_run_is_none(self):
        app = ReviewApplication()
        self.assertIsNone(app.state_of("R-20261030-999-RUN01"))


class ResumeTests(unittest.TestCase):
    def test_failed_run_can_resume_and_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileRunStore(Path(tmp))
            app = ReviewApplication(store=store)
            state = app.initial_state(review_id=REVIEW_ID, run_id=RUN_ID,
                                      profile=profile())
            failed = asyncio.run(app.run(state, ports(error="规则执行炸了"),
                                         config=config()))
            self.assertFalse(failed.ok)
            self.assertIs(failed.state.stage, Stage.ANALYSIS)

            resumed = asyncio.run(app.resume(RUN_ID, ports(), config=config()))
            self.assertIsNotNone(resumed)
            self.assertTrue(resumed.ok, resumed.errors)
            self.assertEqual(resumed.state.completed_stages, STAGE_ORDER)

    def test_resume_of_finished_run_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ReviewApplication(store=FileRunStore(Path(tmp)))
            state = app.initial_state(review_id=REVIEW_ID, run_id=RUN_ID,
                                      profile=profile())
            asyncio.run(app.run(state, ports(), config=config()))
            again = asyncio.run(app.resume(RUN_ID, ports(), config=config()))
            self.assertTrue(again.ok)
            self.assertEqual(again.events, ())

    def test_resume_unknown_run_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ReviewApplication(store=FileRunStore(Path(tmp)))
            self.assertIsNone(asyncio.run(app.resume("R-20261030-999-RUN01",
                                                     ports(), config=config())))


if __name__ == "__main__":
    unittest.main()
