#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_orchestrator.py —— 编排器的独立验收。

编排器只做四件事：挑阶段 → 按顺序跑该阶段节点 → 合成一次推进 → 落检查点。
这些正是「一次审查的生命周期」里最容易失控的部分（谁先跑、失败了停不停、
从哪恢复、检查点写了什么），所以全部用假节点测，不碰模型也不碰真实报告格式。

重点五条：
    1. 同阶段多节点按声明顺序执行，且**只推进一次**（阶段是状态机的边）
    2. 失败就停在原地，不写检查点、不带半成品进入下一阶段
    3. 可重试的失败按 max_attempts 重试；不可重试的立刻停
    4. 检查点每阶段一条，序号递增，state_hash 可复现
    5. 从任意阶段的 RunState 继续跑，不重跑已完成阶段
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (                       # noqa: E402
    STAGE_ORDER,
    Decision,
    Evidence,
    Finding,
    FindingOrigin,
    InvariantViolation,
    Location,
    ReviewProfile,
    RuleRef,
    Severity,
    Stage,
    Verdict,
)
from review_agent.runtime import (                      # noqa: E402
    FileCheckpointStore,
    MemoryCheckpointStore,
    NodeContext,
    NodeResult,
    Orchestrator,
    RunState,
    Workflow,
    merge_results,
    next_finding_seq,
    stage_requirements,
    state_hash,
)

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"
TEXT = "截止目前，项目已顺利完成 20 个里程碑节点。"


def finding(seq, run_id=RUN_ID):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=run_id, dimension="fact",
        severity=Severity.MINOR, quote=TEXT, location=Location("第 1 句", 1),
        confidence=0.8, origin=FindingOrigin.RULE,
        rule=RuleRef(rule_id="FACT-001", dimension="fact", text="规则原文"),
    )


def evidence(run_id=RUN_ID, finding_id="ISS-0001"):
    return Evidence(id="EV-0001", run_id=run_id, quote=TEXT,
                    location=Location("第 1 句", 1),
                    finding_ids=(finding_id,))


def decision(run_id=RUN_ID):
    return Decision(id="DEC-0001", run_id=run_id, verdict=Verdict.PASS,
                    rationale="没有阻断性问题")


class FakeNode:
    """假节点：按声明产出结果，可选失败（前 N 次 / 永远）。"""

    def __init__(self, name, stage, produces=(), *, build=None,
                 error=None, fail_times=-1, retryable=False):
        self.name = name
        self.stage = stage
        self.produces = frozenset(produces)
        self._build = build or (lambda ctx: {})
        self._error = error
        self._fail_times = fail_times
        self._retryable = retryable
        self.calls = 0

    async def run(self, ctx: NodeContext) -> NodeResult:
        self.calls += 1
        if self._error is not None and (
                self._fail_times < 0 or self.calls <= self._fail_times):
            return NodeResult.fail(self._error, retryable=self._retryable)
        return NodeResult.ok(**self._build(ctx))


def full_workflow(*, analysis_semantic=True, evidence_error=None):
    """一条完整的八阶段流水线（analysis 挂两个节点）。"""
    nodes = [
        FakeNode("intake", Stage.INTAKE, {"target_hash"},
                 build=lambda ctx: {"output": {"target_hash": "hash"}}),
        FakeNode("ingest", Stage.INGESTION, {"target_text", "target_meta"},
                 build=lambda ctx: {"output": {"target_text": TEXT,
                                               "target_meta": {}}}),
        FakeNode("plan", Stage.PLANNING, {"plan"},
                 build=lambda ctx: {"output": {"plan": {"dimensions": ["fact"]}}}),
        FakeNode("rule-analyzer", Stage.ANALYSIS, {"findings", "analysis_stats"},
                 build=lambda ctx: {
                     "findings": (finding(1, ctx.state.run_id),
                                  finding(2, ctx.state.run_id)),
                     "output": {"analysis_stats": {"rules": 1}}}),
    ]
    if analysis_semantic:
        nodes.append(FakeNode("semantic-reviewer", Stage.ANALYSIS, {"findings"},
                              build=lambda ctx: {
                                  "findings": (finding(3, ctx.state.run_id),)}))
    nodes += [
        FakeNode("evidence", Stage.EVIDENCE, {"evidence", "evidence_dropped"},
                 error=evidence_error,
                 build=lambda ctx: {"evidence": (evidence(ctx.state.run_id),),
                                    "output": {"evidence_dropped": ()}}),
        FakeNode("cross-reviewer", Stage.CROSS_REVIEW, {"disputes"},
                 build=lambda ctx: {"disputes": ()}),
        FakeNode("decision-engine", Stage.DECISION, {"decision"},
                 build=lambda ctx: {"decision": decision(ctx.state.run_id)}),
        FakeNode("finalize", Stage.FINALIZE, {"report", "audit_export"},
                 build=lambda ctx: {"output": {"report": {"verdict": "pass"},
                                               "audit_export": {}}}),
    ]
    return Workflow(tuple(nodes)), nodes


def initial_state():
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.INTAKE,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
    )


class WorkflowTests(unittest.TestCase):
    def test_same_stage_may_have_several_nodes(self):
        workflow, _ = full_workflow()
        self.assertEqual([n.name for n in workflow.nodes_for(Stage.ANALYSIS)],
                         ["rule-analyzer", "semantic-reviewer"])

    def test_same_stage_nodes_must_be_contiguous(self):
        a = FakeNode("a", Stage.ANALYSIS)
        b = FakeNode("b", Stage.EVIDENCE)
        c = FakeNode("c", Stage.ANALYSIS)
        with self.assertRaises(InvariantViolation):
            Workflow((a, b, c))

    def test_stages_must_advance(self):
        a = FakeNode("a", Stage.EVIDENCE)
        b = FakeNode("b", Stage.ANALYSIS)
        with self.assertRaises(InvariantViolation):
            Workflow((a, b))

    def test_duplicate_node_names_are_rejected(self):
        with self.assertRaises(InvariantViolation):
            Workflow((FakeNode("a", Stage.ANALYSIS),
                      FakeNode("a", Stage.ANALYSIS)))

    def test_stage_requirements_ignore_typed_artifacts(self):
        a = FakeNode("a", Stage.ANALYSIS, {"findings", "analysis_stats"})
        b = FakeNode("b", Stage.ANALYSIS)
        self.assertEqual(stage_requirements((a, b)), frozenset({"analysis_stats"}))

    def test_merge_rejects_duplicate_artifact_keys(self):
        a = FakeNode("a", Stage.ANALYSIS)
        first = NodeResult.ok(output={"analysis_stats": 1})
        second = NodeResult.ok(output={"analysis_stats": 2})
        with self.assertRaises(InvariantViolation):
            merge_results([first, second])
        self.assertEqual(a.stage, Stage.ANALYSIS)

    def test_merge_rejects_duplicate_finding_ids(self):
        run_id = RUN_ID
        with self.assertRaises(InvariantViolation):
            merge_results([
                NodeResult.ok(findings=(finding(1, run_id),)),
                NodeResult.ok(findings=(finding(1, run_id),)),
            ])


class OrchestratorHappyPathTests(unittest.TestCase):
    def test_full_pipeline_completes(self):
        workflow, nodes = full_workflow()
        store = MemoryCheckpointStore()
        outcome = asyncio.run(Orchestrator(workflow, checkpoints=store)
                              .run(initial_state()))
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertIs(outcome.state.stage, Stage.FINALIZE)
        self.assertEqual(outcome.state.completed_stages, STAGE_ORDER)
        self.assertEqual([f.id for f in outcome.state.findings],
                         ["ISS-0001", "ISS-0002", "ISS-0003"])
        self.assertEqual(len(outcome.state.evidence), 1)
        self.assertIsNotNone(outcome.state.decision)
        self.assertEqual(len(outcome.events), len(nodes))

    def test_checkpoints_are_written_once_per_stage(self):
        workflow, _ = full_workflow()
        store = MemoryCheckpointStore()
        outcome = asyncio.run(Orchestrator(workflow, checkpoints=store)
                              .run(initial_state()))
        checkpoints = store.for_run(RUN_ID)
        self.assertEqual([c.stage for c in checkpoints], list(STAGE_ORDER))
        self.assertEqual([c.sequence for c in checkpoints],
                         list(range(len(STAGE_ORDER))))
        self.assertEqual(len(outcome.checkpoints), len(STAGE_ORDER))
        for checkpoint in checkpoints:
            self.assertEqual(len(checkpoint.state_hash), 64)

    def test_same_stage_nodes_run_in_declared_order(self):
        workflow, nodes = full_workflow()
        order = []

        class Recorder:
            """只记顺序的包装节点（保留内层节点的产出）。"""

            def __init__(self, inner):
                self._inner = inner
                self.name = inner.name
                self.stage = inner.stage
                self.produces = inner.produces

            async def run(self, ctx):
                order.append(self.name)
                return await self._inner.run(ctx)

        wrapped = Workflow(tuple(Recorder(n) for n in nodes))
        asyncio.run(Orchestrator(wrapped).run(initial_state()))
        self.assertEqual(order[:6], ["intake", "ingest", "plan",
                                     "rule-analyzer", "semantic-reviewer",
                                     "evidence"])


class OrchestratorFailureTests(unittest.TestCase):
    def test_failure_stops_at_the_stage(self):
        workflow, _ = full_workflow(evidence_error="锚定工具不可用")
        store = MemoryCheckpointStore()
        outcome = asyncio.run(Orchestrator(workflow, checkpoints=store)
                              .run(initial_state()))
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.status, "failed")
        self.assertIs(outcome.state.stage, Stage.EVIDENCE)
        self.assertIn("锚定工具不可用", outcome.errors[0])
        # 只落已完成阶段的检查点：ANALYSIS 之前 4 个阶段
        self.assertEqual(len(store.for_run(RUN_ID)), 4)

    def test_retryable_failure_is_retried(self):
        workflow, _ = full_workflow(evidence_error=None)
        node = FakeNode("evidence", Stage.EVIDENCE, {"evidence", "evidence_dropped"},
                        error="临时故障", fail_times=1, retryable=True,
                        build=lambda ctx: {"evidence": (evidence(ctx.state.run_id),),
                                           "output": {"evidence_dropped": ()}})
        nodes = [n for n in workflow.nodes if n.name != "evidence"] + [node]
        nodes.sort(key=lambda n: n.stage.order)
        outcome = asyncio.run(Orchestrator(Workflow(tuple(nodes)), max_attempts=2)
                              .run(initial_state()))
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertEqual(node.calls, 2)
        attempts = [e.attempt for e in outcome.events if e.node == "evidence"]
        self.assertEqual(attempts, [1, 2])

    def test_non_retryable_failure_is_not_retried(self):
        workflow, _ = full_workflow(evidence_error=None)
        node = FakeNode("evidence", Stage.EVIDENCE, {"evidence", "evidence_dropped"},
                        error="注定失败", fail_times=-1, retryable=False)
        nodes = [n for n in workflow.nodes if n.name != "evidence"] + [node]
        nodes.sort(key=lambda n: n.stage.order)
        outcome = asyncio.run(Orchestrator(Workflow(tuple(nodes)), max_attempts=3)
                              .run(initial_state()))
        self.assertFalse(outcome.ok)
        self.assertEqual(node.calls, 1)

    def test_uncovered_stage_is_not_silently_skipped(self):
        workflow, nodes = full_workflow()
        partial = Workflow(tuple(n for n in nodes if n.stage.order <= Stage.PLANNING.order))
        outcome = asyncio.run(Orchestrator(partial).run(initial_state()))
        self.assertEqual(outcome.status, "incomplete")
        self.assertIn("未覆盖阶段 analysis", outcome.errors[0])


class ResumeTests(unittest.TestCase):
    def test_resume_does_not_rerun_completed_stages(self):
        first_workflow, first_nodes = full_workflow()
        partial = Workflow(tuple(n for n in first_nodes
                                 if n.stage.order <= Stage.PLANNING.order))
        first = asyncio.run(Orchestrator(partial).run(initial_state()))
        self.assertEqual(first.status, "incomplete")
        self.assertIs(first.state.stage, Stage.ANALYSIS)

        full, full_nodes = full_workflow()
        outcome = asyncio.run(Orchestrator(full).run(first.state))
        self.assertTrue(outcome.ok, outcome.errors)
        self.assertEqual(outcome.state.completed_stages, STAGE_ORDER)
        # 前三个阶段的节点在第二次运行里没有再被调用
        again = [n.name for n in full_nodes[:3]]
        calls = {n.name: n.calls for n in full_nodes}
        self.assertTrue(all(calls[name] == 0 for name in again), calls)


class CheckpointFileTests(unittest.TestCase):
    def test_file_store_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(Path(tmp))
            workflow, _ = full_workflow()
            asyncio.run(Orchestrator(workflow, checkpoints=store)
                        .run(initial_state()))
            reopened = FileCheckpointStore(Path(tmp))
            checkpoints = reopened.for_run(RUN_ID)
            self.assertEqual([c.stage for c in checkpoints], list(STAGE_ORDER))
            self.assertEqual([c.sequence for c in checkpoints],
                             list(range(len(STAGE_ORDER))))

    def test_broken_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FileCheckpointStore(Path(tmp))
            workflow, _ = full_workflow()
            asyncio.run(Orchestrator(workflow, checkpoints=store)
                        .run(initial_state()))
            path = Path(tmp) / f"{RUN_ID}.jsonl"
            path.write_text(path.read_text(encoding="utf-8") + "{坏行\n",
                            encoding="utf-8")
            self.assertEqual(len(FileCheckpointStore(Path(tmp)).for_run(RUN_ID)),
                             len(STAGE_ORDER))


class FindingSequenceTests(unittest.TestCase):
    def test_sequence_starts_at_one(self):
        self.assertEqual(next_finding_seq(initial_state()), 1)

    def test_sequence_continues_after_existing_findings(self):
        state = RunState(
            review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.EVIDENCE,
            profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
            completed_stages=STAGE_ORDER[:STAGE_ORDER.index(Stage.EVIDENCE)],
            findings=(finding(1), finding(7)),
        )
        self.assertEqual(next_finding_seq(state), 8)


class StateHashTests(unittest.TestCase):
    def test_hash_is_reproducible(self):
        state = initial_state()
        self.assertEqual(state_hash(state), state_hash(initial_state()))

    def test_hash_tracks_findings(self):
        base = initial_state()
        changed = RunState(
            review_id=base.review_id, run_id=base.run_id, stage=Stage.EVIDENCE,
            profile=base.profile,
            completed_stages=STAGE_ORDER[:STAGE_ORDER.index(Stage.EVIDENCE)],
            findings=(finding(1),),
        )
        self.assertNotEqual(state_hash(base), state_hash(changed))


class SlowNode:
    """会拖时间的节点：用来验证单节点超时。"""

    def __init__(self, name, stage, produces=(), *, delay=0.2):
        self.name = name
        self.stage = stage
        self.produces = frozenset(produces)
        self.delay = delay
        self.calls = 0

    async def run(self, ctx: NodeContext) -> NodeResult:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return NodeResult.ok(output={key: "x" for key in self.produces})


def slow_workflow(delay=0.2, *, fast_rest=True):
    """第一个节点拖时间，其余节点正常——超时必须停在第一个节点。"""
    nodes = [SlowNode("intake", Stage.INTAKE, {"target_hash"}, delay=delay)]
    if fast_rest:
        nodes.append(FakeNode("ingest", Stage.INGESTION,
                              {"target_text", "target_meta"},
                              build=lambda ctx: {"output": {"target_text": TEXT,
                                                            "target_meta": {}}}))
    return Workflow(tuple(nodes)), nodes


class NodeTimeoutTests(unittest.TestCase):
    """单节点超时（阶段 4）。默认关闭，打开后：超时=可重试失败，且能指出卡在哪个节点。"""

    def test_slow_node_times_out(self):
        workflow, nodes = slow_workflow(delay=0.2)
        outcome = asyncio.run(
            Orchestrator(workflow, timeout=0.05).run(initial_state()))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("超时", outcome.errors[0])
        self.assertIn("intake", outcome.errors[0])

    def test_timeout_is_retried(self):
        workflow, nodes = slow_workflow(delay=0.2)
        outcome = asyncio.run(
            Orchestrator(workflow, timeout=0.05, max_attempts=2)
            .run(initial_state()))
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(nodes[0].calls, 2, "超时是可重试失败，应该跑满次数")
        self.assertEqual([e.attempt for e in outcome.events], [1, 2])
        self.assertEqual([e.ok for e in outcome.events], [False, False])

    def test_no_timeout_by_default(self):
        workflow, nodes = slow_workflow(delay=0.05, fast_rest=False)
        outcome = asyncio.run(Orchestrator(workflow).run(initial_state()))
        # 没配超时时不会因为「慢」被判失败；这里是因为 workflow 没覆盖后续阶段
        self.assertNotIn("超时", " ".join(outcome.errors))
        self.assertEqual(nodes[0].calls, 1)

    def test_fast_nodes_are_unaffected(self):
        workflow, _ = full_workflow()
        outcome = asyncio.run(
            Orchestrator(workflow, timeout=5.0).run(initial_state()))
        self.assertEqual(outcome.status, "completed")

    def test_timeout_does_not_make_deterministic_failures_retryable(self):
        workflow, nodes = full_workflow(evidence_error="证据锚定失败")
        outcome = asyncio.run(
            Orchestrator(workflow, timeout=5.0, max_attempts=3)
            .run(initial_state()))
        self.assertEqual(outcome.status, "failed")
        evidence_node = next(n for n in nodes if n.stage is Stage.EVIDENCE)
        self.assertEqual(evidence_node.calls, 1,
                         "不可重试的失败不该因为开了超时就被重试")

    def test_invalid_timeout_is_rejected(self):
        workflow, _ = slow_workflow()
        for bad in (0, -1.0):
            with self.assertRaises(ValueError):
                Orchestrator(workflow, timeout=bad)


if __name__ == "__main__":
    unittest.main()
