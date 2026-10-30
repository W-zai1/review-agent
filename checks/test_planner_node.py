#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_planner_node.py —— Planner 节点的独立验收。

计划是「这次承诺审什么」：`plan.dimensions` / `plan.handlers` 与最终的
`findings`、`coverage` 一对照，漏审立刻显形。所以它只有三条硬要求：
    1. 计划必须包含 Profile 声明的全部维度（不许少列）
    2. 计划为空必须失败（不允许产出「干净」的空报告）
    3. 没有任何本地检查时必须有警告（纯规则模式下等于没审）
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import STAGE_ORDER, ReviewProfile, Stage  # noqa: E402
from review_agent.runtime import NodeContext, RunState, advance  # noqa: E402
from review_agent.runtime.nodes import PlannerNode, build_plan  # noqa: E402

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"


def planning_state(*, dimensions=("fact", "style"), stage=Stage.PLANNING):
    done = STAGE_ORDER[:STAGE_ORDER.index(stage)]
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=stage,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1",
                              dimensions=tuple(dimensions)),
        completed_stages=done,
    )


class BuildPlanTests(unittest.TestCase):
    def test_plan_lists_declared_dimensions_and_handlers(self):
        plan = build_plan(["fact", "style"], {"fact": "builtin:fact"},
                          ["builtin:pattern"])
        self.assertEqual(plan["dimensions"], ["fact", "style"])
        self.assertEqual(plan["handlers"], {"fact": "builtin:fact", "style": ""})
        self.assertEqual(plan["checks"], ["builtin:pattern"])
        self.assertEqual(plan["local_checks"], 1)

    def test_blank_entries_are_ignored(self):
        plan = build_plan(["fact", "  "], {"fact": ""}, ["", None])
        self.assertEqual(plan["dimensions"], ["fact"])
        self.assertEqual(plan["checks"], [])
        self.assertEqual(plan["local_checks"], 0)


class PlannerNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_is_produced(self):
        state = planning_state()
        result = await PlannerNode().run(NodeContext(
            state=state, config={"handlers": {"fact": "builtin:fact"}}))
        self.assertTrue(result.success)
        plan = dict(result.output)["plan"]
        self.assertEqual(plan["dimensions"], ["fact", "style"])
        self.assertEqual(result.warnings, ())

    async def test_no_local_checks_warns(self):
        state = planning_state()
        result = await PlannerNode().run(NodeContext(state=state, config={}))
        self.assertTrue(result.success)
        self.assertTrue(result.warnings)

    async def test_empty_dimensions_fail(self):
        state = planning_state(dimensions=())
        result = await PlannerNode().run(NodeContext(state=state))
        self.assertFalse(result.success)

    async def test_bad_handlers_config_is_rejected(self):
        state = planning_state()
        result = await PlannerNode().run(
            NodeContext(state=state, config={"handlers": ["not", "a", "mapping"]}))
        self.assertFalse(result.success)

    async def test_wrong_stage_is_rejected(self):
        state = planning_state(stage=Stage.INGESTION)
        result = await PlannerNode().run(NodeContext(state=state))
        self.assertFalse(result.success)

    async def test_advance_stores_plan(self):
        state = planning_state()
        result = await PlannerNode().run(NodeContext(
            state=state, config={"handlers": {"fact": "builtin:fact"}}))
        nxt = advance(state, Stage.PLANNING, result)
        self.assertIn("plan", nxt.artifacts)
        self.assertEqual(nxt.artifacts["plan"]["dimensions"], ["fact", "style"])


if __name__ == "__main__":
    unittest.main()
