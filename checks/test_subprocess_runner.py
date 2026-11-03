#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_subprocess_runner.py —— server 的子进程 Runner（问题 2 / 第二阶段）。

任务生命周期统一后，`server` 不再自己写任务状态：它把「跑一次审查」封成
Runner 交给 `TaskService`。这里用假 Popen 验证 Runner 的职责边界：

    * 只跑子进程：命令行参数完整、不发号、不写任务记录；
    * 结果翻译：成功 / 失败 / 取消三种结果折成 TaskOutcome；
    * 取消真的停进程：令牌一置位就 terminate，不是只改状态；
    * JOBS 仍只是视图：状态由 TaskOutcome 决定，不是第二套事实来源。

不启动真实 review.py，也不写仓库的 outputs/ledger。
"""
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

import server                                                    # noqa: E402
from review_agent.application import FileTaskStore, TaskRecord   # noqa: E402
from review_agent.domain import RunStatus, Stage                 # noqa: E402
from review_agent.runtime.cancel import CancellationToken        # noqa: E402

RID = "R-20261103-001"
JOB = "job123456789"


class FakeProc:
    """够 `_run_review_job` 用的最小 Popen 替身。"""

    def __init__(self, *, stdout="", stderr="", returncode=0, block=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = None if block else returncode
        self._released = threading.Event()
        if not block:
            self._released.set()
        self.terminated = False
        self.killed = False
        self.cmd = []

    def communicate(self):
        self._released.wait(5)
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._released.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._released.set()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.report = self.base / "outputs" / "R-20261103-001.json"
        self.report.parent.mkdir(parents=True)
        self.report.write_text("{}", encoding="utf-8")
        self._saved = {name: getattr(server, name)
                       for name in ("ROOT", "TASKS", "JOBS", "PROCS", "JOBS_DIR",
                                    "REVIEW_TIMEOUT")}
        self.addCleanup(self._restore)
        server.ROOT = self.base
        server.TASKS = FileTaskStore(self.base / "state" / "tasks")
        server.JOBS = {JOB: {"id": JOB, "review_id": RID, "status": "running"}}
        server.PROCS = {}
        server.JOBS_DIR = self.base / "logs" / ".jobs"
        server.JOBS_DIR.mkdir(parents=True)
        self.proc = None

    def _restore(self):
        for name, value in self._saved.items():
            setattr(server, name, value)

    def _run(self, proc, *, token=None, **kwargs):
        self.proc = proc

        def spawn(cmd, **kw):
            proc.cmd = cmd
            return proc

        with mock.patch.object(server.subprocess, "Popen", side_effect=spawn):
            return server._run_review_job(
                JOB, RID, "targets/a.md", False,
                server.JOBS_DIR / f"{JOB}.jsonl", token=token, **kwargs)

    def _result_line(self):
        return "REVIEW_RESULT " + json.dumps({
            "review_id": RID, "report_id": RID,
            "report_path": str(self.report), "verdict": "pass",
        }, ensure_ascii=False) + "\n"

    def test_success_maps_to_completed_and_updates_view(self):
        outcome = self._run(FakeProc(stdout=self._result_line()))
        self.assertEqual(outcome.status, RunStatus.COMPLETED)
        self.assertEqual(outcome.stage, Stage.FINALIZE)
        self.assertEqual(outcome.report_path, str(self.report))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(server.JOBS[JOB]["status"], "done")
        self.assertEqual(server.JOBS[JOB]["task_status"], "completed")
        self.assertEqual(server.JOBS[JOB]["report_id"], RID)
        self.assertEqual(server.PROCS, {})

    def test_failure_maps_to_failed_without_writing_task_record(self):
        outcome = self._run(FakeProc(stderr="boom", returncode=1))
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertIn("boom", outcome.error)
        self.assertEqual(server.JOBS[JOB]["status"], "error")
        self.assertIsNone(server.TASKS.get(RID), "Runner 不该写任务记录")

    def test_missing_report_is_failed(self):
        missing = self.base / "nope.json"
        line = "REVIEW_RESULT " + json.dumps({
            "review_id": RID, "report_id": RID,
            "report_path": str(missing), "verdict": "pass",
        }, ensure_ascii=False) + "\n"
        outcome = self._run(FakeProc(stdout=line))
        self.assertEqual(outcome.status, RunStatus.FAILED)
        self.assertIn("未生成报告", outcome.error)

    def test_cancel_terminates_process_and_maps_to_aborted(self):
        stamp = "2026-11-03T00:00:00"
        server.TASKS.save(TaskRecord(
            review_id=RID, run_id=f"{RID}-RUN01",
            status=RunStatus.CANCELLED.value, stage=Stage.ANALYSIS.value,
            target="targets/a.md", created_at=stamp, updated_at=stamp))
        token = CancellationToken()
        proc = FakeProc(block=True)
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault("outcome", self._run(proc, token=token)),
            daemon=True)
        thread.start()
        for _ in range(50):
            if server.PROCS.get(RID) is proc:
                break
            threading.Event().wait(0.02)
        token.cancel("测试取消")
        thread.join(10)
        self.assertFalse(thread.is_alive(), "取消后 Runner 没有退出")
        self.assertTrue(proc.terminated, "取消必须真的 terminate 子进程")
        self.assertEqual(result["outcome"].status, RunStatus.ABORTED)
        self.assertEqual(server.JOBS[JOB]["status"], "cancelled")
        self.assertEqual(server.PROCS, {})

    def test_command_carries_all_options(self):
        runner = server._SubprocessRunner({
            "job_id": JOB, "file": "targets/a.md",
            "progress": server.JOBS_DIR / f"{JOB}.jsonl",
            "llm_opt": False, "research_opt": True, "type_opt": "code",
            "agent_opt": "doc-review", "purpose": "查合规", "purpose_max": 10,
            "purpose_quality": ["prompt"], "scenario_opt": "s1",
            "spec_opt": "course", "custom_opt": None,
        })
        proc = FakeProc(stdout=self._result_line())

        def spawn(cmd, **kw):
            proc.cmd = cmd
            return proc

        with mock.patch.object(server.subprocess, "Popen", side_effect=spawn):
            runner("targets/a.md", review_id=RID, profile_id="doc-review",
                   purpose="查合规", engine="v1", use_llm=False)
        self.assertIn("--review-id", proc.cmd)
        for flag, value in (("--no-llm", None), ("--research", None),
                            ("--type", "code"), ("--agent", "doc-review"),
                            ("--scenario", "s1"), ("--format-spec", "course"),
                            ("--purpose", "查合规"), ("--purpose-max", "10")):
            self.assertIn(flag, proc.cmd, msg=flag)
            if value is not None:
                self.assertEqual(proc.cmd[proc.cmd.index(flag) + 1], value)


if __name__ == "__main__":
    unittest.main()
