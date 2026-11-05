#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_web_task_lifecycle.py —— 任务状态/取消适配层的直接契约。

server 的 22 项任务测试从 HTTP 包装侧覆盖主路径；这里直接钉住抽出来的
`TaskLifecycle`，避免以后有人把“记录优先于 JOBS”“v1 报告回退”改回去。
服务层自身的状态机/取消/超时/恢复仍由 `checks/test_task_lifecycle.py` 覆盖。
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from interfaces.web.task_lifecycle import TaskLifecycle, job_view  # noqa: E402
from review_agent.application import FileTaskStore, TaskRecord  # noqa: E402
from review_agent.domain import RunStatus, Stage  # noqa: E402

RID = "R-20261105-001"


def _record(status, **over):
    return TaskRecord(
        review_id=RID, run_id=f"{RID}-RUN01", status=status.value,
        stage=Stage.FINALIZE.value, target="targets/a.md", **over)


class JobViewTests(unittest.TestCase):
    def test_cancelled_and_report_id(self):
        view = job_view(
            _record(RunStatus.CANCELLED, report_path=f"outputs/{RID}.json"),
            {"id": "job1"})
        self.assertEqual(view["id"], "job1")
        self.assertEqual(view["status"], "cancelled")
        self.assertEqual(view["task_status"], "cancelled")
        self.assertEqual(view["report_id"], RID)
        self.assertIsNotNone(view["finished"])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.tasks = FileTaskStore(self.base / "state" / "tasks")
        self.jobs = {"job1": {"id": "job1", "review_id": RID,
                              "status": "running", "report_id": RID}}
        self.jobs_dir = self.base / "logs" / ".jobs"
        self.jobs_dir.mkdir(parents=True)
        self.outputs = self.base / "outputs"

    def lifecycle(self):
        return TaskLifecycle(
            jobs=self.jobs, jobs_lock=threading.RLock(), tasks=self.tasks,
            jobs_dir=self.jobs_dir, outputs=self.outputs, procs={},
            procs_lock=threading.RLock(), task_service=None,
            read_json=lambda p: json.loads(p.read_text(encoding="utf-8")))

    def test_missing_job(self):
        result = self.lifecycle().status("nope")
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_record_wins_and_v1_report_is_read(self):
        self.tasks.save(_record(
            RunStatus.COMPLETED, report_path=f"outputs/v1/{RID}.json"))
        (self.jobs_dir / "job1.jsonl").write_text(
            '{"step": "S1"}\n', encoding="utf-8")
        report = self.outputs / "v1" / f"{RID}.json"
        report.parent.mkdir(parents=True)
        report.write_text('{"verdict": "pass"}', encoding="utf-8")
        result = self.lifecycle().status("job1")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["job"]["status"], "done")
        self.assertEqual(result["steps"], [{"step": "S1"}])
        self.assertEqual(result["report"], {"verdict": "pass"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
