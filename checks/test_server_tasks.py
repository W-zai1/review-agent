#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_server_tasks.py —— 服务端接任务记录后的归属、闸门、取消（批次 2 / C2）。

改造前服务端的三件事都是"猜"出来的：

    报告归谁     —— 跑前/跑后 `outputs/` 的差集（两个任务并发时会认领到对方那份）
    跑完没有     —— 内存里的 `JOBS`，进程一重启就失忆
    能不能取消   —— 没有这个能力（全仓 `cancel` 零命中）

现在：号由本端预先发（`--print-review-id`）、报告路径由子进程自报（`REVIEW_RESULT`）、
状态落在 `state/tasks/<review_id>.json`、执行进程登记在 `PROCS` 里可被 terminate。

这里不启动真实子进程（那样会写真实 outputs/ledger）：被测的是**归属与状态机**，
把 `_allocate_review_id` 与 `_run_review_job` 换成可控替身，断言它们被怎么调用。
"""
import datetime
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import server                                            # noqa: E402
from review_agent.application import FileTaskStore, TaskOutcome, TaskRecord  # noqa: E402
from review_agent.domain import RunStatus, Stage         # noqa: E402

RID = "R-20261101-007"


class ResultParsingTests(unittest.TestCase):
    def test_parses_marker_line(self):
        out = ("[review] 开始\n"
               'REVIEW_RESULT {"review_id": "R-1", "report_path": "outputs/R-1.json",'
               ' "verdict": "pass"}\n')
        self.assertEqual(server._parse_review_result(out)["review_id"], "R-1")

    def test_last_line_wins(self):
        out = ('REVIEW_RESULT {"review_id": "R-1"}\n'
               'REVIEW_RESULT {"review_id": "R-2"}\n')
        self.assertEqual(server._parse_review_result(out)["review_id"], "R-2")

    def test_garbage_is_ignored(self):
        self.assertIsNone(server._parse_review_result("没有结果行"))
        self.assertIsNone(server._parse_review_result(""))
        self.assertIsNone(server._parse_review_result("REVIEW_RESULT 不是JSON"))
        self.assertIsNone(server._parse_review_result('REVIEW_RESULT {"x": 1}'))


class IdempotencyKeyTests(unittest.TestCase):
    def key(self, **over):
        params = dict(file="targets/a.md", llm=False, research=False, type_=None,
                      agent=None, scenario=None, spec=None, purpose_text="")
        params.update(over)
        return server._idempotency_key(**params)

    def test_same_params_same_key(self):
        self.assertEqual(self.key(), self.key())

    def test_any_param_change_changes_key(self):
        base = self.key()
        for change in ({"llm": True}, {"research": True}, {"type_": "code"},
                       {"agent": "doc-review"}, {"scenario": "s1"},
                       {"spec": "course"}, {"purpose_text": "查合规"},
                       {"file": "targets/b.md"}):
            self.assertNotEqual(self.key(**change), base, msg=str(change))

    def test_purpose_is_not_plaintext_in_key(self):
        """键会落进 state/tasks/，用户填的目的不该以明文出现在键里。"""
        key = self.key(purpose_text="这份合同想干什么")
        self.assertNotIn("合同", key)
        self.assertEqual(len(key), 64)


class JobViewTests(unittest.TestCase):
    def record(self, status, **over):
        return TaskRecord(
            review_id=RID, run_id=f"{RID}-RUN01", status=status.value,
            stage=Stage.FINALIZE.value, target="targets/a.md", **over)

    def test_status_mapping(self):
        cases = {
            RunStatus.QUEUED: "running",
            RunStatus.RUNNING: "running",
            RunStatus.COMPLETED: "done",
            RunStatus.FAILED: "error",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.ABORTED: "cancelled",
        }
        for status, shown in cases.items():
            view = server._job_view(self.record(status), {"id": "job1"})
            self.assertEqual(view["status"], shown, msg=status.value)
            self.assertEqual(view["task_status"], status.value)
            self.assertEqual(view["review_id"], RID)

    def test_report_id_comes_from_task_record(self):
        view = server._job_view(
            self.record(RunStatus.COMPLETED, report_path="outputs/R-20261101-007.json"))
        self.assertEqual(view["report_id"], RID)


class _BlockingRunner:
    """替身 Runner：记录调用、阻塞到测试放行，绝不真的起子进程。"""

    started = threading.Event()
    release = threading.Event()
    calls: list = []

    def __init__(self, spec):
        self.spec = spec

    def __call__(self, target, *, review_id, profile_id, purpose, engine,
                 use_llm, token=None):
        # 「先落任务记录，再起执行器」：这条不变量在替身里也要被验证
        if server.TASKS.get(review_id) is None:
            raise AssertionError("执行器启动前任务记录必须已落盘")
        type(self).calls.append((review_id, self.spec))
        type(self).started.set()
        type(self).release.wait(5)
        return TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE,
                           report_path="outputs/x.json", verdict="pass")


class StartReviewTests(unittest.TestCase):
    """`_start_review` 的三件事：落任务记录、幂等命中、并发闸门。

    任务状态现在由 TaskService 写；执行器通过 Runner 适配层注入，
    被测的是「创建 → 执行 → 终态」仍走同一条链，而不是某个线程函数被怎么调。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.targets = self.base / "targets"
        self.targets.mkdir()
        (self.targets / "a.md").write_text("本公司营业收入 100 亿元。", encoding="utf-8")
        self.saved = {name: getattr(server, name)
                      for name in ("ROOT", "TARGETS", "TASKS", "JOBS", "JOBS_DIR",
                                   "LLM_HISTORY", "LANGUAGE" if hasattr(server, "LANGUAGE") else "ROOT")}
        self.addCleanup(self._restore)
        server.ROOT = self.base
        server.TARGETS = self.targets
        server.UPLOADS = self.targets / "uploads"
        server.TASKS = FileTaskStore(self.base / "state" / "tasks")
        server.JOBS = {}
        server.JOBS_DIR = self.base / "logs" / ".jobs"
        server.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        _BlockingRunner.started = threading.Event()
        _BlockingRunner.release = threading.Event()
        _BlockingRunner.calls = []
        self._patches = [
            mock.patch.object(server, "_allocate_review_id", return_value=RID),
            mock.patch.object(server, "_SubprocessRunner", _BlockingRunner),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        _BlockingRunner.release.set()
        for rid in (RID, "R-20261101-008"):
            try:
                server._task_service().wait(rid, timeout=5)
            except Exception:                                  # noqa: BLE001
                pass
        server._task_service().close()

    def _restore(self):
        for name, value in self.saved.items():
            setattr(server, name, value)

    def start(self, **over):
        data = {"file": "targets/a.md", "llm": False}
        data.update(over)
        return server.Handler._start_review(mock.MagicMock(), data)

    def test_creates_task_record_before_spawn(self):
        result = self.start()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["review_id"], RID)
        self.assertTrue(_BlockingRunner.started.wait(5), "执行器没有被调起")
        record = server.TASKS.get(RID)
        self.assertIsNotNone(record, "起进程之前必须先把任务记录落盘")
        # QUEUED 先落盘、执行器拿到执行权后转 RUNNING；替身被调到时已是 RUNNING。
        self.assertEqual(record.status, RunStatus.RUNNING.value)
        self.assertEqual(record.stage, Stage.INTAKE.value)
        self.assertTrue(record.idempotency_key)
        self.assertEqual(len(_BlockingRunner.calls), 1)
        self.assertEqual(_BlockingRunner.calls[0][0], RID)
        job = server.JOBS[result["job_id"]]
        self.assertEqual(job["review_id"], RID)
        self.assertEqual(job["status"], "running")

    def test_duplicate_submit_is_deduped(self):
        first = self.start()
        self.assertTrue(_BlockingRunner.started.wait(5))
        second = self.start()
        self.assertTrue(second["ok"], second)
        self.assertTrue(second.get("deduped"), "同一份材料 + 同一套参数不该建第二个任务")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(_BlockingRunner.calls), 1, "幂等命中不该再起一个执行器")

    def test_different_params_are_blocked_by_gate(self):
        first = self.start()
        self.assertTrue(first["ok"])
        self.assertTrue(_BlockingRunner.started.wait(5))
        # 换参数 = 另一个任务，但此刻已有任务在跑 → 闸门拦下（不是静默并发）
        second = self.start(llm=True)
        self.assertFalse(second["ok"], second)
        self.assertIn("进行中", second["error"])
        self.assertEqual(second["review_id"], RID)

    def test_finished_task_does_not_block(self):
        first = self.start()
        self.assertTrue(_BlockingRunner.started.wait(5))
        _BlockingRunner.release.set()
        server._task_service().wait(RID, timeout=5)
        with mock.patch.object(server, "_allocate_review_id",
                               return_value="R-20261101-008"):
            second = self.start(llm=True)
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["review_id"], "R-20261101-008")
        self.assertNotEqual(first["review_id"], second["review_id"])


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self._saved = {name: getattr(server, name) for name in ("TASKS", "JOBS", "PROCS")}
        self.addCleanup(self._restore)
        server.TASKS = FileTaskStore(self.base / "state" / "tasks")
        server.JOBS = {}
        server.PROCS = {}
        self.stamp = datetime.datetime.now().isoformat(timespec="seconds")
        server.TASKS.save(TaskRecord(
            review_id=RID, run_id=f"{RID}-RUN01",
            status=RunStatus.RUNNING.value, stage=Stage.ANALYSIS.value,
            target="targets/a.md", created_at=self.stamp, updated_at=self.stamp))
        server.JOBS["job1"] = {"id": "job1", "review_id": RID, "status": "running"}

    def _restore(self):
        for name, value in self._saved.items():
            setattr(server, name, value)

    def test_cancel_terminates_process_and_marks_record(self):
        class FakeProc:
            terminated = False

            def terminate(self):
                FakeProc.terminated = True

        server.PROCS[RID] = FakeProc()
        result = server.Handler._cancel_review(mock.MagicMock(), {"job": "job1"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], RunStatus.CANCELLED.value)
        self.assertTrue(FakeProc.terminated, "取消必须真的停掉子进程，不能只改状态")
        self.assertEqual(server.TASKS.get(RID).status, RunStatus.CANCELLED.value)
        self.assertEqual(server.JOBS["job1"]["status"], "cancelled")

    def test_cancel_is_idempotent(self):
        first = server.Handler._cancel_review(mock.MagicMock(), {"review_id": RID})
        second = server.Handler._cancel_review(mock.MagicMock(), {"review_id": RID})
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"], "重复取消应当原样返回，而不是报错")
        self.assertEqual(second["status"], RunStatus.CANCELLED.value)

    def test_cannot_cancel_finished(self):
        server.TASKS.save(TaskRecord(
            review_id=RID, run_id=f"{RID}-RUN01",
            status=RunStatus.COMPLETED.value, stage=Stage.FINALIZE.value,
            target="targets/a.md", created_at=self.stamp, updated_at=self.stamp))
        result = server.Handler._cancel_review(mock.MagicMock(), {"review_id": RID})
        self.assertFalse(result["ok"])
        self.assertIn("终态", result["error"])

    def test_unknown_task_is_reported(self):
        result = server.Handler._cancel_review(mock.MagicMock(), {"review_id": "R-1"})
        self.assertFalse(result["ok"])
        self.assertIn("找不到", result["error"])


if __name__ == "__main__":
    unittest.main()
