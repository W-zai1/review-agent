"""同步审查适配层：失败分支与响应契约。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from interfaces.web.review_sync import SyncReview
from review_agent.domain import NotFoundError, RunStatus


class _Store:
    def __init__(self, records=()):
        self._records = list(records)

    def all(self):
        return list(self._records)


def _record(**kw):
    base = {"review_id": "R-1", "status": RunStatus.COMPLETED,
            "report_path": None, "error": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _make(record=None, *, service=None, store=None, target_error=None,
          agent_error=None, report_path=None, clock=None):
    record = record or _record(report_path=report_path)
    calls = {"start": 0}

    class Service:
        def start_review_with(self, *args, **kw):
            calls["start"] += 1
            if service is not None:
                return service(*args, **kw)
            return record

    def build_runner(request, job_id, progress):
        return {"job_id": job_id, "progress": progress}

    def read_json(path):
        return {"review_id": "R-1", "ok": True}

    times = iter(clock or [100.0, 100.4])
    sync = SyncReview(
        tasks=store or _Store(),
        task_stale_after=timedelta(hours=1),
        target_types={"docx"},
        resolve_target=lambda file: (
            (None, target_error) if target_error
            else (Path("target.docx"), None)),
        resolve_agent=lambda raw: (
            (None, agent_error) if agent_error else (None, None)),
        resolve_scenario=lambda raw: (None, None),
        resolve_format_spec=lambda raw: (None, None),
        resolve_format_custom=lambda raw: (None, None),
        clean_purpose=lambda raw: raw or "",
        assemble_purpose=lambda text, files: ("", None, None),
        idempotency_key=lambda **kw: "K-1",
        build_runner=build_runner,
        task_service=lambda: Service(),
        read_json=read_json,
        parse_review_error=lambda text: (
            {"kind": "timeout", "http_status": 504}
            if "超时" in text else None),
        jobs_dir=Path(tempfile.gettempdir()) / "ra-sync-tests",
        timeout=120,
        clock=lambda: next(times),
    )
    return sync, calls


class SyncReviewTests(unittest.TestCase):
    def test_target_error_short_circuits(self):
        sync, calls = _make(target_error="目标不存在")
        self.assertEqual(sync.run({}), {"ok": False, "error": "目标不存在"})
        self.assertEqual(calls["start"], 0)

    def test_busy_gate_uses_task_records(self):
        active = SimpleNamespace(
            review_id="R-BUSY", status=RunStatus.RUNNING,
            updated_at=datetime.now().isoformat(timespec="seconds"))
        sync, calls = _make(store=_Store([active]))
        self.assertEqual(
            sync.run({}),
            {"ok": False, "error": "已有审查任务进行中，请稍候",
             "review_id": "R-BUSY"})
        self.assertEqual(calls["start"], 0)

    def test_request_error_short_circuits(self):
        sync, calls = _make(agent_error="未知 Agent：x")
        self.assertEqual(sync.run({}), {"ok": False, "error": "未知 Agent：x"})
        self.assertEqual(calls["start"], 0)

    def test_completed_reads_report(self):
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "R-1.json"
            report.write_text("{}", encoding="utf-8")
            sync, calls = _make(_record(report_path=report))
            out = sync.run({})
        self.assertTrue(out["ok"])
        self.assertEqual(out["id"], "R-1")
        self.assertEqual(out["elapsed"], 0.4)
        self.assertEqual(calls["start"], 1)

    def test_completed_without_report_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing.json"
            sync, _ = _make(_record(report_path=missing))
            out = sync.run({})
        self.assertEqual(out["ok"], False)
        self.assertEqual(out["error"], "审查完成但未生成报告")
        self.assertEqual(out["review_id"], "R-1")

    def test_timeout_wording_and_kind(self):
        sync, _ = _make(_record(
            status=RunStatus.FAILED, error="任务超时（120s）"))
        out = sync.run({})
        self.assertEqual(out["error"], "审查超时（120s）")
        self.assertEqual(out["error_kind"], "timeout")
        self.assertEqual(out["error_status"], 504)

    def test_task_service_not_found(self):
        def boom(*args, **kw):
            raise NotFoundError("找不到目标")
        sync, _ = _make(service=boom)
        self.assertEqual(sync.run({}),
                         {"ok": False, "error": "找不到目标"})

    def test_task_service_oserror(self):
        def boom(*args, **kw):
            raise OSError("disk full")
        sync, _ = _make(service=boom)
        self.assertEqual(sync.run({}),
                         {"ok": False, "error": "无法写入任务记录：disk full"})


if __name__ == "__main__":
    unittest.main()
