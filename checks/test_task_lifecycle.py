#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_task_lifecycle.py —— 任务生命周期：状态机 / 取消 / 超时 / 恢复 / 幂等。

为什么这四件事要一起测：它们共享同一份「任务状态」，任何一件单独做都会漏边界。

    * 取消若不看状态 → 取消一个已完成的报告，等于改写历史结论；
    * 超时若不置位令牌 → 线程继续跑，最后把一份"用户以为已经失败"的报告写进台账；
    * 恢复若不看检查点 → 把一个跑不完的任务说成"可恢复"，用户会一直等下去；
    * 幂等若不落盘 → 进程重启后同一份材料又被审一遍（实测生产台账里
      286 条记录只对应 77 个不同目标，单份材料最多审了 15 次）。

取消的语义是**协作式**的（`runtime/cancel.py`）：只在阶段边界与节点边界生效。
这里用假节点复现三种时点——开始前、节点之间、最后一个节点之后——因为
"取消落在哪个边界"决定了状态该是 CANCELLED 还是照常 COMPLETED。
"""
import asyncio
import datetime
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.application import (                  # noqa: E402
    FileTaskStore,
    TaskOutcome,
    TaskRecord,
    TaskService,
)
from review_agent.domain import (                       # noqa: E402
    InvalidTransition,
    ReviewProfile,
    RunStatus,
    Stage,
    recoverable,
)
from review_agent.runtime import (                      # noqa: E402
    ABORTED,
    CancellationToken,
    NodeContext,
    NodeResult,
    Orchestrator,
    RunState,
    Workflow,
)

REVIEW_ID = "R-20261101-001"
RUN_ID = f"{REVIEW_ID}-RUN01"


# ---------------------------------------------------------------------------
# 编排器侧：取消落在哪个边界
# ---------------------------------------------------------------------------
class FakeNode:
    """假节点：可指定在执行前/执行时触发一次回调（用来模拟"用户此刻点了取消"）。"""

    def __init__(self, name, stage, produces=(), *, on_run=None, build=None):
        self.name = name
        self.stage = stage
        self.produces = frozenset(produces)
        self._on_run = on_run
        self._build = build or (lambda ctx: {})
        self.calls = 0

    async def run(self, ctx: NodeContext) -> NodeResult:
        self.calls += 1
        if self._on_run is not None:
            self._on_run()
        return NodeResult.ok(**self._build(ctx))


def two_stage_workflow(*, cancel_in_first=None):
    """最小完整流水线：ANALYSIS → EVIDENCE → CROSS_REVIEW → DECISION → FINALIZE。

    少一个阶段都不行：编排器对"workflow 未覆盖阶段"是**报错**而不是跳过
    （不允许悄悄跳过阶段出结论），所以假流水线也得走完八阶段的后半段。
    """
    nodes = [
        FakeNode("analyze", Stage.ANALYSIS, {"findings", "analysis_stats"},
                 on_run=cancel_in_first,
                 build=lambda ctx: {"output": {"analysis_stats": {"rules": 1}}}),
        FakeNode("evidence", Stage.EVIDENCE, {"evidence", "evidence_dropped"},
                 build=lambda ctx: {"output": {"evidence_dropped": ()}}),
        FakeNode("cross", Stage.CROSS_REVIEW, {"disputes"},
                 build=lambda ctx: {"disputes": ()}),
        FakeNode("decide", Stage.DECISION, {"decision"},
                 build=lambda ctx: {"output": {}}),
        FakeNode("finalize", Stage.FINALIZE, {"report", "audit_export"},
                 build=lambda ctx: {"output": {"report": {"verdict": "pass"},
                                               "audit_export": {}}}),
    ]
    return Workflow(tuple(nodes)), nodes


def analysis_state():
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=Stage.ANALYSIS,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
        completed_stages=(Stage.INTAKE, Stage.INGESTION, Stage.PLANNING),
        artifacts={"target_text": "样本", "target_meta": {}, "plan": {}},
    )


class CancellationTokenTests(unittest.TestCase):
    def test_token_starts_clean(self):
        token = CancellationToken()
        self.assertFalse(token.cancelled)
        self.assertFalse(bool(token))
        token.raise_if_cancelled()          # 不该抛

    def test_cancel_is_one_way_and_keeps_first_reason(self):
        token = CancellationToken()
        token.cancel("用户点了取消")
        token.cancel("第二次请求")
        self.assertTrue(token.cancelled)
        self.assertEqual(token.reason, "用户点了取消",
                         "第一次的原因才是触发者，后到的请求不该覆盖它")

    def test_raise_if_cancelled_raises(self):
        token = CancellationToken()
        token.cancel("停")
        with self.assertRaises(Exception):
            token.raise_if_cancelled()


class OrchestratorCancellationTests(unittest.TestCase):
    def run_with(self, token, workflow):
        return asyncio.run(Orchestrator(workflow, token=token).run(analysis_state()))

    def test_cancel_before_start_runs_no_node(self):
        workflow, nodes = two_stage_workflow()
        token = CancellationToken()
        token.cancel("开始前取消")
        outcome = self.run_with(token, workflow)
        self.assertEqual(outcome.status, ABORTED)
        self.assertFalse(outcome.ok)
        self.assertEqual([n.calls for n in nodes], [0, 0, 0, 0, 0],
                         "阶段边界检查在先：发现已取消就一个节点都不该跑")

    def test_cancel_between_nodes_stops_advancing(self):
        """第一个节点执行时置位取消：不推进阶段，状态停在 ANALYSIS。"""
        token = CancellationToken()
        workflow, nodes = two_stage_workflow(cancel_in_first=lambda: token.cancel("跑到一半"))
        outcome = self.run_with(token, workflow)
        self.assertEqual(outcome.status, ABORTED)
        self.assertEqual(outcome.state.stage, Stage.ANALYSIS,
                         "取消不推进阶段：状态必须停在原地（恢复/重跑都靠它）")
        self.assertEqual(nodes[1].calls, 0, "取消后不该再跑下一个节点")
        self.assertNotIn(Stage.ANALYSIS, outcome.state.completed_stages)

    def test_no_token_keeps_old_behaviour(self):
        workflow, nodes = two_stage_workflow()
        outcome = asyncio.run(Orchestrator(workflow).run(analysis_state()))
        self.assertEqual(outcome.status, "completed")
        self.assertEqual([n.calls for n in nodes], [1, 1, 1, 1, 1])


# ---------------------------------------------------------------------------
# 领域侧：状态机与"可恢复"判断
# ---------------------------------------------------------------------------
class LifecycleStateMachineTests(unittest.TestCase):
    def test_queued_and_cancelled_are_legal(self):
        from review_agent.domain import ensure_run_transition
        ensure_run_transition(RunStatus.CREATED, RunStatus.QUEUED)
        ensure_run_transition(RunStatus.QUEUED, RunStatus.RUNNING)
        ensure_run_transition(RunStatus.QUEUED, RunStatus.CANCELLED)
        ensure_run_transition(RunStatus.RUNNING, RunStatus.CANCELLED)
        ensure_run_transition(RunStatus.AWAITING_DECISION, RunStatus.CANCELLED)

    def test_cancelled_is_terminal(self):
        from review_agent.domain import ensure_run_transition
        with self.assertRaises(InvalidTransition):
            ensure_run_transition(RunStatus.CANCELLED, RunStatus.RUNNING)
        self.assertTrue(RunStatus.CANCELLED.is_terminal)

    def test_sync_path_may_skip_queue(self):
        """同步入口直接 CREATED→RUNNING 仍然合法（不强逼所有入口先入队）。"""
        from review_agent.domain import ensure_run_transition
        ensure_run_transition(RunStatus.CREATED, RunStatus.RUNNING)

    def test_recoverable_needs_all_three(self):
        self.assertTrue(recoverable("running", has_checkpoint=True,
                                    has_live_executor=False))
        self.assertFalse(recoverable("running", has_checkpoint=False,
                                     has_live_executor=False))
        self.assertFalse(recoverable("running", has_checkpoint=True,
                                     has_live_executor=True))
        self.assertFalse(recoverable("cancelled", has_checkpoint=True,
                                     has_live_executor=False))
        self.assertFalse(recoverable("queued", has_checkpoint=True,
                                     has_live_executor=False))
        self.assertFalse(recoverable("不存在的状态", has_checkpoint=True,
                                     has_live_executor=False))


# ---------------------------------------------------------------------------
# 服务侧：取消 / 超时 / 恢复 / 幂等 / 异步
# ---------------------------------------------------------------------------
class _ServiceHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "targets").mkdir()
        self.target = self.base / "targets" / "sample.md"
        self.target.write_text("本公司 2025 年营业收入 100 亿元。", encoding="utf-8")
        # 跨测试唯一：避免 task 文件名相同（每个测试用独立临时目录，这里只是可读性）
        self.service = None

    def make_service(self, runner, **kwargs):
        self.service = TaskService(
            runner=runner, store=FileTaskStore(self.base / "tasks"),
            allocate=self.alloc, root=self.base, **kwargs)
        self.addCleanup(self.service.close)
        return self.service

    def alloc(self, day):
        self._seq = getattr(self, "_seq", 0) + 1
        return f"R-{day.strftime('%Y%m%d')}-{self._seq:03d}"


def completed_runner(target, *, review_id, profile_id=None, purpose=None,
                     engine="v1", use_llm=False):
    return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE,
                       verdict="pass", findings=2, evidence=2,
                       report_path=f"outputs/v1/{review_id}.json")


class CancelTests(_ServiceHarness):
    def test_cancel_before_any_run(self):
        service = self.make_service(completed_runner)
        record = service.start_async.__self__  # 保持引用语义：仅用于类型提示
        del record
        queued = service.start_async.__doc__   # noqa: F841  (占位，避免遮蔽)
        # 手工建一个 queued 记录：runner 故意阻塞，确保取消发生在执行期
        gate = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            gate.wait(timeout=5)
            return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE)

        self.service.close()
        service = self.make_service(blocking)
        record = service.start_async(self.target)
        self.assertEqual(record.status, RunStatus.QUEUED.value)
        cancelled = service.cancel(record.review_id, reason="用户不要了")
        gate.set()
        self.assertEqual(cancelled.status, RunStatus.CANCELLED.value)
        # 等后台线程收尾，状态必须仍是 CANCELLED（不能被 "COMPLETED" 覆盖）
        final = service.wait(record.review_id, timeout=5)
        self.assertEqual(final.status, RunStatus.CANCELLED.value)
        self.assertIn("用户不要了", final.error or "")

    def test_cancel_is_idempotent(self):
        service = self.make_service(completed_runner)
        gate = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            gate.wait(timeout=5)
            return TaskOutcome(status=RunStatus.ABORTED, stage=Stage.ANALYSIS)

        self.service.close()
        service = self.make_service(blocking)
        record = service.start_async(self.target)
        first = service.cancel(record.review_id, reason="第一次")
        second = service.cancel(record.review_id, reason="第二次")
        gate.set()
        release = second
        del release
        self.assertEqual(first.status, RunStatus.CANCELLED.value)
        self.assertEqual(second.status, RunStatus.CANCELLED.value,
                         "重复取消应原样返回，而不是报错")

    def test_cancel_terminal_task_is_rejected(self):
        service = self.make_service(completed_runner)
        record = service.start_review(self.target)
        self.assertEqual(record.status, RunStatus.COMPLETED.value)
        with self.assertRaises(InvalidTransition):
            service.cancel(record.review_id)

    def test_token_reaches_runner_and_stops_report(self):
        """runner 接了 token 就必须收到它；被取消时不得产出报告路径。"""
        seen = {}

        def runner(target, *, review_id, token=None, **kwargs):
            seen["token"] = token
            token.cancel("测试取消")
            return TaskOutcome(status=RunStatus.FAILED, stage=Stage.ANALYSIS,
                               error=f"已取消：{token.reason}")

        service = self.make_service(runner)
        record = service.start_review(self.target)
        self.assertIsInstance(seen.get("token"), CancellationToken,
                              "新 runner 应当收到取消令牌")
        self.assertEqual(record.status, RunStatus.FAILED.value)
        self.assertIsNone(record.report_path, "取消的运行不该有报告路径")

    def test_legacy_runner_without_token_still_works(self):
        """老 runner（不接 token）不能被新代码打断——迁移期硬要求。"""
        def legacy(target, *, review_id, profile_id=None, purpose=None,
                   engine="v1", use_llm=False):
            return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE,
                               report_path="outputs/v1/x.json")

        service = self.make_service(legacy)
        record = service.start_review(self.target)
        self.assertEqual(record.status, RunStatus.COMPLETED.value)


class TimeoutTests(_ServiceHarness):
    def test_timeout_marks_failed_and_cancels_token(self):
        seen = {}

        def slow(target, *, review_id, token=None, **kwargs):
            seen["token"] = token
            time.sleep(0.3)
            return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE)

        service = self.make_service(slow)
        record = service.start_review(self.target, timeout=0.05)
        self.assertEqual(record.status, RunStatus.FAILED.value)
        self.assertIn("超时", record.error or "")
        self.assertTrue(seen["token"].cancelled,
                        "超时必须置位令牌，否则线程会一直跑下去并在事后写出报告")

    def test_no_timeout_keeps_old_behaviour(self):
        service = self.make_service(completed_runner)
        record = service.start_review(self.target)
        self.assertEqual(record.status, RunStatus.COMPLETED.value)


class IdempotencyTests(_ServiceHarness):
    def test_same_key_returns_same_task_while_running(self):
        gate = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            gate.wait(timeout=5)
            return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE)

        service = self.make_service(blocking)
        first = service.start_async(self.target, idempotency_key="k1")
        second = service.start_async(self.target, idempotency_key="k1")
        self.assertEqual(first.review_id, second.review_id,
                         "同一个键 + 未结束的任务 → 不该建第二个任务")
        gate.set()
        service.wait(first.review_id, timeout=5)

    def test_finished_task_does_not_block_new_submission(self):
        service = self.make_service(completed_runner)
        first = service.start_review(self.target, idempotency_key="k2")
        second = service.start_review(self.target, idempotency_key="k2")
        self.assertNotEqual(first.review_id, second.review_id,
                            "上一单已完成时，重复提交应当真的再跑一次（材料可能改过）")
        self.assertEqual(second.idempotency_key, "k2")

    def test_key_survives_restart(self):
        service = self.make_service(completed_runner)
        first = service.start_review(self.target, idempotency_key="k3")
        service.close()
        # 换一个服务实例（模拟进程重启）：键在记录里，去重仍然成立
        revived = TaskService(runner=completed_runner,
                              store=FileTaskStore(self.base / "tasks"),
                              allocate=self.alloc, root=self.base)
        self.addCleanup(revived.close)
        self.assertEqual(revived.get_task(first.review_id).idempotency_key, "k3")


class RecoveryTests(_ServiceHarness):
    def _seed(self, service, status, *, age_minutes=0, run_id=RUN_ID):
        """直接往 store 里塞一条记录（复现"进程被杀时留下的状态"）。"""
        stamp = (datetime.datetime.now()
                 - datetime.timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
        record = TaskRecord(
            review_id=REVIEW_ID, run_id=run_id, status=status.value,
            stage=Stage.ANALYSIS.value, target="targets/sample.md",
            created_at=stamp, updated_at=stamp)
        service._store.save(record)
        return record

    def test_orphan_without_checkpoint_becomes_failed(self):
        service = self.make_service(completed_runner)
        self._seed(service, RunStatus.RUNNING)
        report = service.recover(has_checkpoint=lambda run_id: False)
        self.assertEqual(len(report.orphaned), 1)
        self.assertEqual(report.resumable, ())
        self.assertEqual(report.orphaned[0].status, RunStatus.FAILED.value)
        self.assertIn("检查点", report.orphaned[0].error or "")

    def test_running_with_checkpoint_is_resumable(self):
        service = self.make_service(completed_runner)
        self._seed(service, RunStatus.RUNNING)
        report = service.recover(has_checkpoint=lambda run_id: True)
        self.assertEqual(len(report.resumable), 1)
        self.assertEqual(report.orphaned, ())
        self.assertEqual(service.get_task(REVIEW_ID).status, RunStatus.RUNNING.value,
                         "可恢复的任务不该被改状态——它还要接着跑")

    def test_stale_running_is_orphaned_even_with_checkpoint(self):
        service = self.make_service(
            completed_runner, stale_after=datetime.timedelta(minutes=5))
        self._seed(service, RunStatus.RUNNING, age_minutes=30)
        report = service.recover(has_checkpoint=lambda run_id: True)
        self.assertEqual(report.resumable, ())
        self.assertEqual(len(report.orphaned), 1)
        self.assertIn("失联", report.orphaned[0].error or "")

    def test_queued_fresh_is_resumable_and_stale_is_orphaned(self):
        service = self.make_service(
            completed_runner, stale_after=datetime.timedelta(minutes=5))
        self._seed(service, RunStatus.QUEUED, run_id=RUN_ID)
        fresh = service.recover(has_checkpoint=lambda run_id: False)
        self.assertEqual(len(fresh.resumable), 1,
                         "刚入队就重启：应当重新入队，而不是判死")

        self._seed(service, RunStatus.QUEUED, age_minutes=30, run_id=RUN_ID)
        stale = service.recover(has_checkpoint=lambda run_id: False)
        self.assertEqual(len(stale.orphaned), 1)

    def test_terminal_tasks_are_ignored(self):
        service = self.make_service(completed_runner)
        self._seed(service, RunStatus.COMPLETED)
        self._seed(service, RunStatus.CANCELLED, run_id=RUN_ID)
        report = service.recover(has_checkpoint=lambda run_id: True)
        self.assertEqual(report.total, 0, "终态任务不该出现在体检结果里")


class AsyncTests(_ServiceHarness):
    def test_async_returns_queued_then_completes(self):
        service = self.make_service(completed_runner)
        record = service.start_async(self.target)
        self.assertEqual(record.status, RunStatus.QUEUED.value,
                         "提交后必须立刻拿到 QUEUED（RUNNING 只属于真正在跑的）")
        final = service.wait(record.review_id, timeout=5)
        self.assertEqual(final.status, RunStatus.COMPLETED.value)
        self.assertEqual(final.findings, 2)

    def test_live_task_is_reported_active(self):
        gate = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            gate.wait(timeout=5)
            return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE)

        service = self.make_service(blocking)
        record = service.start_async(self.target)
        time.sleep(0.05)
        self.assertIn(record.review_id, service.active_review_ids(),
                      "并发闸门要能看见正在跑的任务（替代 server 内存里的布尔判断）")
        gate.set()
        service.wait(record.review_id, timeout=5)
        self.assertEqual(service.active_review_ids(), ())


if __name__ == "__main__":
    unittest.main()
