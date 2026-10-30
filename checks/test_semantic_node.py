#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_semantic_node.py —— Semantic Reviewer 节点的独立验收。

节点接管的是「怎么送审、审了多少、哪些没审成」，不接管 prompt。
模型调用通过 `SemanticExecutor` 端口注入，所以这里全程不联网、不出随机结果。

四层验收：
    1. 计划：维度 × 块，顺序固定（维度声明顺序 → 块序号）
    2. 覆盖：区间并集（块间重叠只算一次）、`max_input_chars` 截断、失败块
       **必须**让 complete=False（旧实现把失败块 continue 掉后仍报全覆盖）
    3. 节点：只有失败全部发生才判阶段失败；部分失败是警告
    4. 编号：发现编号从 `ctx.next_finding_seq` 起，与规则审同阶段不撞号
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    NodeContext,
    Orchestrator,
    RunState,
    Workflow,
)
from review_agent.runtime.nodes import (                # noqa: E402
    RuleAnalyzerNode,
    RuleStep,
    SemanticReviewerNode,
    SemanticStep,
    SemanticStepOutcome,
    build_meta,
    coverage_from_meta,
    covered_chars,
    plan_semantic_steps,
)
from review_agent.runtime.nodes.semantic import DEFAULT_MAX_INPUT_CHARS  # noqa: E402

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"
TEXT = "0123456789"


def finding(seq, run_id=RUN_ID):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=run_id, dimension="fact",
        severity=Severity.MINOR, quote=TEXT[:3], location=Location("第 1 句", 1),
        confidence=0.8, origin=FindingOrigin.MODEL,
        rule=RuleRef(rule_id="FACT-001", dimension="fact"),
    )


def analysis_state(*, text=TEXT, dimensions=("fact", "style"), stage=Stage.ANALYSIS):
    done = STAGE_ORDER[:STAGE_ORDER.index(stage)]
    artifacts = {"target_text": text} if text is not None else {}
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=stage,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1",
                              dimensions=tuple(dimensions)),
        completed_stages=done, artifacts=artifacts,
    )


class PlanTests(unittest.TestCase):
    def test_dimension_then_chunk_order(self):
        steps = plan_semantic_steps(["fact", "style"], [(0, 5), (5, 10)], TEXT)
        self.assertEqual([(s.dimension, s.chunk_index) for s in steps],
                         [("fact", 1), ("fact", 2), ("style", 1), ("style", 2)])
        self.assertEqual([s.text for s in steps], ["01234", "56789", "01234", "56789"])

    def test_invalid_step_is_rejected(self):
        with self.assertRaises(ValueError):
            SemanticStep(dimension="", chunk_index=1, start=0, end=1, text="x")
        with self.assertRaises(ValueError):
            SemanticStep(dimension="fact", chunk_index=0, start=0, end=1, text="x")
        with self.assertRaises(ValueError):
            SemanticStep(dimension="fact", chunk_index=1, start=5, end=1, text="x")


class CoverageTests(unittest.TestCase):
    def test_overlap_counted_once(self):
        self.assertEqual(covered_chars([(0, 5), (3, 9)]), 9)
        self.assertEqual(covered_chars([(0, 5), (5, 10)]), 10)
        self.assertEqual(covered_chars([(10, 10)]), 0)
        self.assertEqual(covered_chars([]), 0)

    def test_unsorted_spans(self):
        self.assertEqual(covered_chars([(5, 10), (0, 5)]), 10)

    def test_meta_without_chars_reviewed_means_full(self):
        coverage = coverage_from_meta({}, 100)
        self.assertEqual(coverage["chars_reviewed"], 100)
        self.assertTrue(coverage["complete"])

    def test_failed_chunk_without_chars_reviewed_is_not_full(self):
        coverage = coverage_from_meta({"failed_chunks": [{"dim": "fact", "chunk": 1}]}, 100)
        self.assertEqual(coverage["chars_reviewed"], 0)
        self.assertFalse(coverage["complete"])

    def test_explicit_zero_is_not_treated_as_missing(self):
        # 0 是有效值：falsy 回退会把「一块都没审成」变成「全文已审」
        coverage = coverage_from_meta({"chars_reviewed": 0,
                                       "failed_chunks": [{"dim": "fact", "chunk": 1}]}, 100)
        self.assertEqual(coverage["chars_reviewed"], 0)
        self.assertEqual(coverage["text_coverage_pct"], 0)
        self.assertFalse(coverage["complete"])

    def test_truncation_marks_incomplete(self):
        coverage = coverage_from_meta({"chars_reviewed": 100,
                                       "input_truncated_dimensions": ["fact"]}, 100)
        self.assertFalse(coverage["complete"])

    def test_clamps_reviewed_to_total(self):
        self.assertEqual(coverage_from_meta({"chars_reviewed": 500}, 100)["chars_reviewed"], 100)


class BuildMetaTests(unittest.TestCase):
    def test_failed_steps_do_not_count_as_covered(self):
        steps = plan_semantic_steps(["fact"], [(0, 5), (5, 10)], TEXT)
        outcomes = [SemanticStepOutcome(candidates=1), SemanticStepOutcome(error="超时")]
        meta = build_meta(steps, outcomes, chars_total=10,
                          max_input_chars=DEFAULT_MAX_INPUT_CHARS)
        self.assertEqual(meta["chars_reviewed"], 5)
        self.assertEqual(meta["chunks_attempted"], 2)
        self.assertEqual(meta["chunks_succeeded"], 1)
        self.assertEqual(meta["failed_chunks"][0]["dim"], "fact")
        self.assertEqual(meta["failed_chunks"][0]["start"], 5)

    def test_same_span_across_dimensions_counted_once(self):
        steps = plan_semantic_steps(["fact", "style"], [(0, 10)], TEXT)
        outcomes = [SemanticStepOutcome(), SemanticStepOutcome()]
        meta = build_meta(steps, outcomes, chars_total=10,
                          max_input_chars=DEFAULT_MAX_INPUT_CHARS)
        self.assertEqual(meta["chars_reviewed"], 10)

    def test_max_input_chars_clips_coverage(self):
        steps = plan_semantic_steps(["fact"], [(0, 10)], TEXT)
        meta = build_meta(steps, [SemanticStepOutcome()], chars_total=10,
                          max_input_chars=4)
        self.assertEqual(meta["chars_reviewed"], 4)

    def test_truncation_and_tokens_are_aggregated(self):
        steps = plan_semantic_steps(["fact", "fact"], [(0, 5), (5, 10)], TEXT)
        outcomes = [
            SemanticStepOutcome(truncated=True, usage={"input": 3, "output": 4}),
            SemanticStepOutcome(output_truncated=True, cached=True,
                                usage={"input": 1, "output": 2}),
        ]
        meta = build_meta(steps, outcomes, chars_total=10,
                          max_input_chars=DEFAULT_MAX_INPUT_CHARS)
        self.assertEqual(meta["input_truncated_dimensions"], ["fact"])
        self.assertEqual(meta["output_truncated_dimensions"], ["fact"])
        self.assertEqual(meta["tokens"], {"input": 4, "output": 6})
        self.assertEqual(meta["cache_hits"], 1)


class SemanticNodeTests(unittest.IsolatedAsyncioTestCase):
    def executor_returning(self, count_per_step=1, *, error=None):
        def executor(step: SemanticStep, start: int):
            if error:
                return SemanticStepOutcome(error=error)
            return SemanticStepOutcome(
                findings=tuple(finding(start + i) for i in range(count_per_step)),
                candidates=count_per_step)

        return executor

    async def test_happy_path_covers_full_text(self):
        state = analysis_state()
        node = SemanticReviewerNode(self.executor_returning())
        result = await node.run(NodeContext(state=state, config={}))
        self.assertTrue(result.success)
        # 两个维度 × 一个块（未分块）= 每步一条
        self.assertEqual([f.id for f in result.findings], ["ISS-0001", "ISS-0002"])
        coverage = dict(result.output)["semantic_stats"]["coverage"]
        self.assertEqual(coverage["chars_reviewed"], len(TEXT))
        self.assertTrue(coverage["complete"])
        self.assertEqual(result.warnings, ())

    async def test_partial_failure_is_a_warning(self):
        calls = {"n": 0}

        def executor(step, start):
            calls["n"] += 1
            if calls["n"] == 1:
                return SemanticStepOutcome(error="模型超时")
            return SemanticStepOutcome(findings=(finding(start),))

        state = analysis_state(dimensions=("fact",))
        result = await SemanticReviewerNode(executor).run(
            NodeContext(state=state, config={"chunks": [(0, 5), (5, 10)]}))
        self.assertTrue(result.success)
        self.assertTrue(result.warnings)
        coverage = dict(result.output)["semantic_stats"]["coverage"]
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["chunks_succeeded"], 1)

    async def test_all_steps_failing_is_a_failure(self):
        state = analysis_state()
        result = await SemanticReviewerNode(self.executor_returning(error="全挂")).run(
            NodeContext(state=state, config={}))
        self.assertFalse(result.success)
        self.assertIn("全部语义审调用失败", "；".join(result.errors))

    async def test_executor_exception_is_recorded_as_failure(self):
        def boom(step, start):
            raise RuntimeError("执行器炸了")

        state = analysis_state(dimensions=("fact",))
        result = await SemanticReviewerNode(boom).run(
            NodeContext(state=state, config={"chunks": [(0, 5), (5, 10)]}))
        self.assertFalse(result.success)
        self.assertIn("RuntimeError", "；".join(result.errors))

    async def test_missing_text_and_empty_dimensions_fail(self):
        node = SemanticReviewerNode(self.executor_returning())
        no_text = analysis_state(text=None)
        self.assertFalse((await node.run(NodeContext(state=no_text))).success)
        no_dims = analysis_state(dimensions=())
        self.assertFalse((await node.run(NodeContext(state=no_dims))).success)

    async def test_wrong_stage_is_rejected(self):
        state = analysis_state(stage=Stage.PLANNING)
        result = await SemanticReviewerNode(self.executor_returning()).run(
            NodeContext(state=state))
        self.assertFalse(result.success)

    async def test_findings_start_from_context_sequence(self):
        state = analysis_state(dimensions=("fact",))
        result = await SemanticReviewerNode(self.executor_returning()).run(
            NodeContext(state=state, config={}, next_finding_seq=8))
        self.assertEqual([f.id for f in result.findings], ["ISS-0008"])


class AnalysisStageCompositionTests(unittest.TestCase):
    """规则审 + 语义审同处 analysis：编号必须接续，产物键必须不撞。"""

    def test_two_nodes_in_analysis_do_not_collide(self):
        rule_node = RuleAnalyzerNode(
            lambda step: [finding(1), finding(2)][: 2 if step.handler == "a" else 0])
        semantic_node = SemanticReviewerNode(
            lambda step, start: SemanticStepOutcome(findings=(finding(start),)))

        state = analysis_state(dimensions=("fact",))
        workflow = Workflow((
            _named(rule_node, "rule-analyzer",
                   lambda ctx: {"findings": (finding(1), finding(2)),
                                "output": {"analysis_stats": {"rules": 1}}}),
            _named(semantic_node, "semantic-reviewer", None),
        ))
        outcome = asyncio.run(Orchestrator(workflow).run(state))
        # 只编排了 analysis：跑完就该停在 EVIDENCE 并报「未覆盖」，不许跳过
        self.assertEqual(outcome.status, "incomplete")
        ids = [f.id for f in outcome.state.findings]
        self.assertEqual(ids, ["ISS-0001", "ISS-0002", "ISS-0003"])
        self.assertIn("analysis_stats", outcome.state.artifacts)
        self.assertIn("semantic_stats", outcome.state.artifacts)


class _WithBuild:
    """把节点包一层，替换它的产出（只用于编排测试）。"""

    def __init__(self, inner, name, build):
        self._inner = inner
        self.name = name
        self.stage = inner.stage
        self.produces = inner.produces
        self._build = build

    async def run(self, ctx):
        if self._build is not None:
            from review_agent.runtime import NodeResult
            return NodeResult.ok(**self._build(ctx))
        return await self._inner.run(ctx)


def _named(inner, name, build):
    return _WithBuild(inner, name, build)


if __name__ == "__main__":
    unittest.main()
