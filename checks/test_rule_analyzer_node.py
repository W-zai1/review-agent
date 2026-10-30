#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_rule_analyzer_node.py —— Rule Analyzer 节点的独立验收。

这个节点只做**调度**：决定跑哪些检查、按什么顺序、每步产出多少条。
检查函数本身仍在能力层（`_h_*` / Agent 自带 `checks.py`），通过 `RuleExecutor` 注入。

为什么调度值得单独测：旧实现里「先维度处理器、再附加检查」写在
`run_agent_steps` 的循环里，漏跑一类检查时报告依然"看起来正常"——
节点把每步变成一条 `RuleStepOutcome`，漏跑/失败会立刻显形。

四层验收：
    1. 计划：维度顺序、缺处理器跳过、附加检查追加、空处理器名忽略
    2. 执行：产出聚合、每步计数、两种失败语义（raise / record）
    3. 节点：阶段校验、缺原文、空计划、部分失败、全失败
    4. 状态机：产出进 RunState、`analysis_stats` 落产物、阶段推进
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
from review_agent.runtime import NodeContext, RunState, advance, stage_requirements  # noqa: E402
from review_agent.runtime.nodes import (                # noqa: E402
    RuleAnalysisResult,
    RuleAnalyzerNode,
    RuleStep,
    plan_rule_steps,
    run_rule_plan,
)

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"
TEXT = "截止目前，项目已顺利完成 20 个里程碑节点。"


def finding(seq, *, run_id=RUN_ID):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=run_id, dimension="fact",
        severity=Severity.MINOR, quote=TEXT, location=Location("第 1 句", 1),
        confidence=0.8, origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id="FACT-001", dimension="fact", text="规则原文"),
    )


class PlanTests(unittest.TestCase):
    def test_dimension_order_is_manifest_order(self):
        steps = plan_rule_steps(["fact", "safety", "style"],
                                {"fact": "builtin:fact", "safety": "builtin:safety"})
        self.assertEqual([s.handler for s in steps],
                         ["builtin:fact", "builtin:safety"])
        self.assertEqual([s.dimension for s in steps], ["fact", "safety"])

    def test_dimension_without_handler_is_skipped(self):
        steps = plan_rule_steps(["fact", "logic"], {"fact": "builtin:fact"})
        self.assertEqual([s.dimension for s in steps], ["fact"])

    def test_checks_run_after_dimensions_without_dimension(self):
        steps = plan_rule_steps(["fact"], {"fact": "builtin:fact"},
                                ["builtin:pattern", "pack:x"])
        self.assertEqual([s.handler for s in steps],
                         ["builtin:fact", "builtin:pattern", "pack:x"])
        self.assertIsNone(steps[-1].dimension)

    def test_blank_entries_are_ignored(self):
        steps = plan_rule_steps(["fact", "style"],
                                {"fact": "", "style": "   "}, ["", None])
        self.assertEqual(steps, ())

    def test_step_requires_handler(self):
        with self.assertRaises(ValueError):
            RuleStep(handler="")


class RunPlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_findings_are_aggregated_in_step_order(self):
        plan = (RuleStep("a"), RuleStep("b"))
        produced = {"a": [finding(1)], "b": [finding(2), finding(3)]}
        result = await run_rule_plan(plan, lambda step: produced[step.handler])
        self.assertEqual([f.id for f in result.findings],
                         ["ISS-0001", "ISS-0002", "ISS-0003"])
        self.assertEqual([len(o.findings) for o in result.outcomes], [1, 2])
        self.assertEqual(result.executed, ("a", "b"))

    async def test_async_executor_is_awaited(self):
        # 工具注册表的 execute 是 async 的：计划执行器必须能接住 awaitable
        async def executor(step):
            return [finding(1)]

        result = await run_rule_plan((RuleStep("a"),), executor)
        self.assertEqual([f.id for f in result.findings], ["ISS-0001"])

    async def test_raise_mode_propagates(self):
        def boom(_step):
            raise RuntimeError("处理器炸了")

        with self.assertRaises(RuntimeError):
            await run_rule_plan((RuleStep("a"),), boom)

    async def test_record_mode_keeps_going_and_reports_errors(self):
        def executor(step):
            if step.handler == "a":
                raise RuntimeError("处理器炸了")
            return [finding(1)]

        result = await run_rule_plan((RuleStep("a"), RuleStep("b")), executor,
                                     on_error="record")
        self.assertEqual([o.ok for o in result.outcomes], [False, True])
        self.assertEqual(len(result.findings), 1)
        self.assertTrue(result.errors)
        self.assertIn("RuntimeError", result.errors[0])

    async def test_bad_on_error_value_is_rejected(self):
        with self.assertRaises(ValueError):
            await run_rule_plan((), lambda s: (), on_error="ignore")

    async def test_stats_include_every_step(self):
        result = await run_rule_plan((RuleStep("a", "fact"),),
                                     lambda step: [finding(1)])
        stats = detail(result)
        self.assertEqual(stats["total_findings"], 1)
        self.assertEqual(stats["steps"][0]["handler"], "a")
        self.assertEqual(stats["steps"][0]["dimension"], "fact")


def detail(result: RuleAnalysisResult):
    return dict(result.stats())


def analysis_state(*, text=TEXT, stage=Stage.ANALYSIS, dimensions=("fact", "safety")):
    done = STAGE_ORDER[:STAGE_ORDER.index(stage)]
    artifacts = {"target_text": text} if text is not None else {}
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=stage,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1",
                              dimensions=tuple(dimensions)),
        completed_stages=done, artifacts=artifacts,
    )


class RuleNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_node_executes_plan_and_reports_stats(self):
        seen = []

        def executor(step):
            seen.append(step.handler)
            return [finding(len(seen))]

        state = analysis_state()
        node = RuleAnalyzerNode(executor)
        result = await node.run(NodeContext(
            state=state, config={"handlers": {"fact": "builtin:fact"},
                                 "checks": ["builtin:pattern"]}))
        self.assertTrue(result.success)
        self.assertEqual(seen, ["builtin:fact", "builtin:pattern"])
        self.assertEqual([f.id for f in result.findings], ["ISS-0001", "ISS-0002"])
        stats = dict(result.output)["analysis_stats"]
        self.assertEqual(stats["total_findings"], 2)

    async def test_missing_target_text_fails(self):
        state = analysis_state(text=None)
        result = await RuleAnalyzerNode(lambda s: ()).run(
            NodeContext(state=state, config={"handlers": {"fact": "builtin:fact"}}))
        self.assertFalse(result.success)
        self.assertIn("target_text", "；".join(result.errors))

    async def test_empty_plan_is_a_failure_not_a_clean_report(self):
        state = analysis_state()
        result = await RuleAnalyzerNode(lambda s: ()).run(
            NodeContext(state=state, config={"handlers": {}}))
        self.assertFalse(result.success)
        self.assertIn("没有可执行", "；".join(result.errors))

    async def test_partial_failure_is_a_warning(self):
        def executor(step):
            if step.handler == "builtin:fact":
                raise RuntimeError("炸了")
            return [finding(1)]

        state = analysis_state()
        result = await RuleAnalyzerNode(executor).run(NodeContext(
            state=state, config={"handlers": {"fact": "builtin:fact"},
                                 "checks": ["builtin:pattern"]}))
        self.assertTrue(result.success)
        self.assertEqual(len(result.findings), 1)
        self.assertTrue(result.warnings)

    async def test_all_steps_failing_is_a_failure(self):
        def executor(_step):
            raise RuntimeError("全炸")

        state = analysis_state()
        result = await RuleAnalyzerNode(executor).run(NodeContext(
            state=state, config={"handlers": {"fact": "builtin:fact"}}))
        self.assertFalse(result.success)
        self.assertIn("全部规则处理器失败", "；".join(result.errors))

    async def test_wrong_stage_is_rejected(self):
        state = analysis_state(stage=Stage.PLANNING)
        result = await RuleAnalyzerNode(lambda s: ()).run(NodeContext(state=state))
        self.assertFalse(result.success)


class AdvanceTests(unittest.TestCase):
    def test_findings_and_stats_enter_state(self):
        state = analysis_state()
        node = RuleAnalyzerNode(
            lambda step: [finding(1)])
        result = asyncio.run(node.run(NodeContext(
            state=state, config={"handlers": {"fact": "builtin:fact"}})))
        # 这一阶段实际只挂了规则审：必需产物按「节点 produces 的并集」算，
        # 不要求语义审才有的 semantic_stats（见 advance(required=) 的说明）
        nxt = advance(state, Stage.ANALYSIS, result,
                      required=stage_requirements((node,)))
        self.assertIs(nxt.stage, Stage.EVIDENCE)
        self.assertEqual([f.id for f in nxt.findings], ["ISS-0001"])
        self.assertIn("analysis_stats", nxt.artifacts)
        self.assertEqual(nxt.artifacts["analysis_stats"]["total_findings"], 1)


if __name__ == "__main__":
    unittest.main()
