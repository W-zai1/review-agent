"""决策书 4.2 验收：任务生命周期只有一个事实来源。

这些用例不重复单测细节（状态机、超时、落盘格式分别有 test_task_service /
test_task_lifecycle 覆盖），只逐条钉住 4.2 的七条验收标准：

1. 任务状态只有一个事实来源：`JOBS` 与任务记录冲突时以记录为准；
2. Web 与 CLI 使用同一个 `TaskService`，不是各跑一套；
3. 同一幂等请求不会重复创建任务；
4. 取消不会被随后算出的 `completed` 覆盖；
5. 取消后报告状态与记录一致（已产出的报告路径不丢、没产出的不凭空出现）；
6. 强制终止后能明确判断「可恢复」还是「已废弃」；
7. 并发任务状态不会互相覆盖。

生产代码不为这些用例改动：它们验证的正是阶段二 2.15/2.16 落下来的行为。
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from interfaces.web import task_lifecycle
from review_agent.application import (
    FileTaskStore,
    TaskOutcome,
    TaskRecord,
    TaskService,
)
from review_agent.domain import RunStatus, Stage
from review_agent.domain.ids import format_run_id


class _Harness(unittest.TestCase):
    """独立临时目录 + 任务存储 + 编号分配器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "targets").mkdir()
        (self.base / "outputs" / "v1").mkdir(parents=True)
        self.target = self.base / "targets" / "sample.md"
        self.target.write_text("本公司 2025 年营业收入 100 亿元。", encoding="utf-8")
        self.tasks = FileTaskStore(self.base / "tasks")
        self._seq = 0
        self._services: list[TaskService] = []

    def alloc(self, day):
        self._seq += 1
        return f"R-{day.strftime('%Y%m%d')}-{self._seq:03d}"

    def make_service(self, runner):
        service = TaskService(
            runner=runner, store=self.tasks, allocate=self.alloc, root=self.base)
        self._services.append(service)
        self.addCleanup(service.close)
        return service

    def save_record(self, review_id, status="completed", **kwargs):
        record = TaskRecord(
            review_id=review_id,
            run_id=format_run_id(review_id, 1),
            status=status,
            stage=kwargs.pop("stage", Stage.FINALIZE.value),
            target="targets/sample.md",
            updated_at=kwargs.pop(
                "updated_at", datetime.datetime.now().isoformat(timespec="seconds")),
            **kwargs,
        )
        self.tasks.save(record)
        return record

    def lifecycle(self, *, jobs, task_service=None, outputs=None):
        return task_lifecycle.TaskLifecycle(
            jobs=jobs, jobs_lock=threading.Lock(), tasks=self.tasks,
            jobs_dir=self.base / ".jobs", outputs=outputs or self.base / "outputs",
            procs={}, procs_lock=threading.Lock(),
            task_service=task_service,
            read_json=lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
        )


# ---------------------------------------------------------------------------
# 1. 单一事实来源
# ---------------------------------------------------------------------------
class SingleSourceTests(_Harness):
    def test_task_record_wins_over_stale_jobs_view(self):
        """JOBS 说 running、记录说 completed：状态与报告都必须以记录为准。"""
        review_id = "R-20261111-001"
        report = self.base / "outputs" / "v1" / f"{review_id}.json"
        report.write_text(json.dumps({"review_id": review_id, "verdict": "pass"}),
                          encoding="utf-8")
        self.save_record(review_id, report_path=f"outputs/v1/{review_id}.json")
        jobs = {"sync-abc": {"id": "sync-abc", "review_id": review_id,
                             "status": "running", "finished": None}}
        result = self.lifecycle(jobs=jobs).status("sync-abc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["job"]["status"], "done")
        self.assertEqual(result["job"]["task_status"], "completed")
        self.assertEqual(result["report"]["review_id"], review_id)

    def test_jobs_only_task_without_record_is_still_shown(self):
        """重启后只有 .jobs 视图、还没有记录的老任务：按视图显示，不假装失败。"""
        jobs = {"sync-old": {"id": "sync-old", "review_id": "R-OLD",
                             "status": "running"}}
        result = self.lifecycle(jobs=jobs).status("sync-old")
        self.assertTrue(result["ok"])
        self.assertEqual(result["job"]["status"], "running")


# ---------------------------------------------------------------------------
# 2. Web 与 CLI 同语义
# ---------------------------------------------------------------------------
class CliParityTests(_Harness):
    def test_cli_builds_the_same_task_service(self):
        """CLI 的 v1 入口必须建任务记录，而不是绕开 TaskService 直接跑。"""
        import review

        calls = []

        class FakeService:
            def start_review(self, target, **kwargs):
                calls.append((str(target), kwargs))
                return SimpleNamespace(
                    status=RunStatus.FAILED.value, error="忽略：只验证装配",
                    review_id="R-20261111-099")

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(review, "make_task_service",
                               lambda **kwargs: FakeService()), \
                mock.patch.object(review.sys, "exit",
                                  side_effect=SystemExit), \
                contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                review._run_v1_via_task("targets/sample.md", None, use_llm=False)

        self.assertEqual(len(calls), 1)
        _, kwargs = calls[0]
        self.assertEqual(kwargs.get("engine"), "v1")
        self.assertIn("use_llm", kwargs)

    def test_make_task_service_persists_to_state_tasks(self):
        """CLI 与 server 用的是同一种存储：`state/tasks/` 下的任务记录。"""
        import review

        service = review.make_task_service()
        self.addCleanup(service.close)
        self.assertIsInstance(service, TaskService)
        self.assertEqual(Path(service._store.root), Path(review.STATE) / "tasks")


# ---------------------------------------------------------------------------
# 3–4. 幂等 / 取消不被完成覆盖
# ---------------------------------------------------------------------------
class IdempotencyAndCancelTests(_Harness):
    def test_same_key_does_not_create_two_tasks(self):
        gate = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            gate.wait(timeout=5)
            return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE)

        service = self.make_service(blocking)
        first = service.start_async(self.target, idempotency_key="same-material")
        second = service.start_async(self.target, idempotency_key="same-material")
        try:
            self.assertEqual(first.review_id, second.review_id)
            self.assertEqual(len(self.tasks.all()), 1)
        finally:
            gate.set()
            service.wait(first.review_id, timeout=5)

    def test_cancel_is_not_overwritten_by_completed_outcome(self):
        gate = threading.Event()
        entered = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            entered.set()
            gate.wait(timeout=5)
            return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE)

        service = self.make_service(blocking)
        record = service.start_async(self.target)
        self.assertTrue(entered.wait(timeout=5))
        cancelled = service.cancel(record.review_id, reason="用户取消")
        gate.set()
        final = service.wait(record.review_id, timeout=5)
        self.assertEqual(cancelled.status, RunStatus.CANCELLED.value)
        self.assertEqual(final.status, RunStatus.CANCELLED.value,
                         "取消后算出的 completed 不能覆盖 CANCELLED")


# ---------------------------------------------------------------------------
# 5. 取消后报告与记录一致
# ---------------------------------------------------------------------------
class CancelReportConsistencyTests(_Harness):
    def test_cancel_keeps_report_path_produced_before_stop(self):
        """取消前已经算出报告：记录保留路径，但状态仍是 CANCELLED。"""
        gate = threading.Event()
        entered = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            entered.set()
            gate.wait(timeout=5)
            return TaskOutcome(
                status=RunStatus.COMPLETED, stage=Stage.FINALIZE,
                report_path=f"outputs/v1/{review_id}.json")

        service = self.make_service(blocking)
        record = service.start_async(self.target)
        self.assertTrue(entered.wait(timeout=5))
        service.cancel(record.review_id, reason="用户取消")
        gate.set()
        final = service.wait(record.review_id, timeout=5)
        self.assertEqual(final.status, RunStatus.CANCELLED.value)
        self.assertEqual(final.report_path,
                         f"outputs/v1/{final.review_id}.json")

    def test_cancel_before_any_result_leaves_no_report_path(self):
        gate = threading.Event()
        entered = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            entered.set()
            gate.wait(timeout=5)
            return TaskOutcome(status=RunStatus.FAILED, stage=Stage.ANALYSIS,
                               error="已取消")

        service = self.make_service(blocking)
        record = service.start_async(self.target)
        self.assertTrue(entered.wait(timeout=5))
        service.cancel(record.review_id, reason="用户取消")
        gate.set()
        final = service.wait(record.review_id, timeout=5)
        self.assertEqual(final.status, RunStatus.CANCELLED.value)
        self.assertIsNone(final.report_path)


# ---------------------------------------------------------------------------
# 6. 强制终止后可判断是否可恢复
# ---------------------------------------------------------------------------
class RecoverabilityTests(_Harness):
    def test_cancelled_task_is_terminal_and_not_resurrected(self):
        self.save_record("R-20261111-003", status=RunStatus.CANCELLED.value,
                         error="用户取消")
        service = self.make_service(lambda *a, **k: None)
        report = service.recover(has_checkpoint=lambda run_id: True)
        self.assertEqual(report.resumable, ())
        self.assertEqual(report.orphaned, ())

    def test_killed_task_without_checkpoint_is_orphaned_not_resumable(self):
        self.save_record("R-20261111-004", status=RunStatus.RUNNING.value)
        service = self.make_service(lambda *a, **k: None)
        report = service.recover(has_checkpoint=lambda run_id: False)
        self.assertEqual(report.resumable, ())
        self.assertEqual(len(report.orphaned), 1)
        self.assertEqual(report.orphaned[0].status, RunStatus.FAILED.value)

    def test_killed_task_with_checkpoint_is_resumable(self):
        self.save_record("R-20261111-005", status=RunStatus.RUNNING.value)
        service = self.make_service(lambda *a, **k: None)
        report = service.recover(has_checkpoint=lambda run_id: True)
        self.assertEqual(len(report.resumable), 1)
        self.assertEqual(report.orphaned, ())


class CrossProcessCancelTests(_Harness):
    """CLI 是独立进程：取消必须靠持久任务记录到达执行进程。"""

    def test_external_cancel_reaches_executing_runner(self):
        started = threading.Event()
        result = {}
        observed = {}

        def runner(target, *, review_id, token=None, **kwargs):
            started.set()
            deadline = time.monotonic() + 2
            while not token.cancelled and time.monotonic() < deadline:
                time.sleep(0.01)
            observed["cancelled"] = bool(token and token.cancelled)
            return TaskOutcome(
                status=RunStatus.ABORTED, stage=Stage.ANALYSIS,
                error="cancelled")

        executing = self.make_service(runner)
        writer = self.make_service(lambda *args, **kwargs: TaskOutcome(
            status=RunStatus.COMPLETED, stage=Stage.FINALIZE))

        worker = threading.Thread(
            target=lambda: result.update(
                record=executing.start_review(self.target)))
        worker.start()
        self.assertTrue(started.wait(1), "runner 没有开始")

        review_id = self.tasks.all()[0].review_id
        writer.cancel(review_id, reason="外部 CLI 取消")
        worker.join(3)

        self.assertFalse(worker.is_alive(), "外部取消没有让 runner 结束")
        self.assertTrue(observed.get("cancelled"),
                        "外部取消没有传到执行中的 token")
        self.assertEqual(result["record"].status, RunStatus.CANCELLED.value)


# ---------------------------------------------------------------------------
# 7. 并发任务互不覆盖
# ---------------------------------------------------------------------------
class ConcurrencyTests(_Harness):
    def test_two_tasks_keep_their_own_status(self):
        gate = threading.Event()

        def blocking(target, *, review_id, **kwargs):
            gate.wait(timeout=5)
            return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE)

        service = self.make_service(blocking)
        first = service.start_async(self.target, idempotency_key="a")
        second = service.start_async(self.target, idempotency_key="b")
        self.assertNotEqual(first.review_id, second.review_id)
        service.cancel(first.review_id, reason="只取消第一个")
        gate.set()
        one = service.wait(first.review_id, timeout=5)
        two = service.wait(second.review_id, timeout=5)
        self.assertEqual(one.status, RunStatus.CANCELLED.value)
        self.assertEqual(two.status, RunStatus.COMPLETED.value)


if __name__ == "__main__":
    unittest.main()
