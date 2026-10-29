#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_console_reliability.py —— 控制台可靠性回归（公开、无敏感样本）。

守住三件容易回退的事：
  1. 审查子进程超时可配置（默认 1800s，clamp [30, 86400]，CLI > env > 默认）；
  2. 服务重启后能从 logs/.jobs/*.jsonl 恢复任务状态，绝不伪造 running；
  3. 超过 7 天的 .jobs 进度文件在启动时被清理。

运行：
    python -m unittest discover -s checks -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import server  # noqa: E402


def _iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat()


def _event(step, name, ts, detail=None):
    item = {
        "step": step,
        "name": name,
        "status": "success",
        "ts": _iso(ts),
        "latency_ms": 0,
    }
    if detail:
        item["detail"] = detail
    return item


def _write_jsonl(path, events):
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
        encoding="utf-8")


class ReviewTimeoutTests(unittest.TestCase):
    """REVIEW_AGENT_REVIEW_TIMEOUT：默认 1800，非法回落，clamp 上下限。"""

    def test_default_is_1800_without_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server._review_timeout(), 1800)

    def test_env_overrides_default(self):
        with mock.patch.dict(
                os.environ, {"REVIEW_AGENT_REVIEW_TIMEOUT": "60"}, clear=True):
            self.assertEqual(server._review_timeout(), 60)

    def test_invalid_env_falls_back_to_default(self):
        for bad in ("abc", "", "   ", "12.5", "None", "--"):
            with mock.patch.dict(
                    os.environ, {"REVIEW_AGENT_REVIEW_TIMEOUT": bad},
                    clear=True):
                self.assertEqual(server._review_timeout(), 1800, bad)

    def test_env_is_clamped_to_lower_bound(self):
        for raw in ("1", "0", "-100"):
            with mock.patch.dict(
                    os.environ, {"REVIEW_AGENT_REVIEW_TIMEOUT": raw},
                    clear=True):
                self.assertEqual(server._review_timeout(), 30, raw)

    def test_env_is_clamped_to_upper_bound(self):
        with mock.patch.dict(
                os.environ,
                {"REVIEW_AGENT_REVIEW_TIMEOUT": "999999"}, clear=True):
            self.assertEqual(server._review_timeout(), 86400)

    def test_env_bounds_are_inclusive(self):
        for raw, want in (("30", 30), ("86400", 86400)):
            with mock.patch.dict(
                    os.environ, {"REVIEW_AGENT_REVIEW_TIMEOUT": raw},
                    clear=True):
                self.assertEqual(server._review_timeout(), want, raw)


class ReviewTimeoutArgvTests(unittest.TestCase):
    """--review-timeout 的优先级：CLI > 环境变量 > 默认。"""

    def _parse(self, argv, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=True):
            return server._parse_review_timeout(argv)

    def test_cli_overrides_env(self):
        self.assertEqual(
            self._parse(["--review-timeout", "600"],
                        {"REVIEW_AGENT_REVIEW_TIMEOUT": "120"}),
            600)

    def test_equals_form(self):
        self.assertEqual(
            self._parse(["--review-timeout=45"],
                        {"REVIEW_AGENT_REVIEW_TIMEOUT": "120"}),
            45)

    def test_env_used_without_cli(self):
        self.assertEqual(
            self._parse(["--port", "8000"],
                        {"REVIEW_AGENT_REVIEW_TIMEOUT": "120"}),
            120)

    def test_default_used_without_cli_and_env(self):
        self.assertEqual(self._parse([]), 1800)

    def test_invalid_cli_falls_back_to_env(self):
        self.assertEqual(
            self._parse(["--review-timeout", "abc"],
                        {"REVIEW_AGENT_REVIEW_TIMEOUT": "120"}),
            120)

    def test_cli_is_clamped(self):
        self.assertEqual(self._parse(["--review-timeout", "1"]), 30)
        self.assertEqual(self._parse(["--review-timeout", "999999"]), 86400)


class JobRecoveryTests(unittest.TestCase):
    """从 .jobs/*.jsonl 恢复任务：有报告 done；无报告 error；不伪造 running。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.outputs = self.dir / "outputs"
        self.outputs.mkdir()
        self.jsonl = self.dir / "a1b2c3d4e5f6.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def _events(self, steps=("S1", "S2", "S3", "S6")):
        start = time.time() - 120
        events = []
        for i, step in enumerate(steps):
            detail = None
            if step == "S1":
                detail = {"待审对象": "targets/sample.txt"}
            events.append(_event(step, f"步骤{step}", start + i, detail))
        return events

    def _write_report(self, rid, ts, target="targets/sample.txt"):
        (self.outputs / f"{rid}.json").write_text(json.dumps({
            "review_id": rid,
            "target_path": target,
            "timestamp": _iso(ts),
        }, ensure_ascii=False), encoding="utf-8")

    def test_existing_report_marks_done(self):
        events = self._events()
        _write_jsonl(self.jsonl, events)
        self._write_report("R-20261029-001", server._parse_iso_ts(events[-1]["ts"]))

        state = server._job_state_from_jsonl(
            self.jsonl, 1800, outputs_dir=self.outputs)

        self.assertEqual(state["status"], "done")
        self.assertEqual(state["report_id"], "R-20261029-001")
        self.assertIsNone(state["error"])
        self.assertEqual(state["timeout"], 1800)
        self.assertTrue(state["recovered"])
        self.assertEqual(state["recover_reason"], "done")

    def test_report_for_other_target_is_not_matched(self):
        events = self._events()
        _write_jsonl(self.jsonl, events)
        self._write_report("R-20261029-002",
                           server._parse_iso_ts(events[-1]["ts"]),
                           target="targets/other.txt")

        state = server._job_state_from_jsonl(
            self.jsonl, 1800, outputs_dir=self.outputs)

        self.assertEqual(state["status"], "error")
        self.assertIsNone(state["report_id"])

    def test_no_report_marks_error_not_running(self):
        events = self._events(("S1", "S2", "S3"))
        _write_jsonl(self.jsonl, events)

        state = server._job_state_from_jsonl(
            self.jsonl, 1800, outputs_dir=self.outputs)

        self.assertEqual(state["status"], "error")
        self.assertNotEqual(state["status"], "running")
        self.assertIsNone(state["report_id"])
        self.assertIn("服务重启，任务已中断，请重新审查", state["error"])
        self.assertFalse(state["timed_out"])
        self.assertEqual(state["recover_reason"], "interrupted")
        self.assertEqual(state["last_step"], "S3")

    def test_no_report_past_timeout_is_flagged(self):
        events = self._events(("S1", "S2"))
        _write_jsonl(self.jsonl, events)
        later = self.jsonl.stat().st_mtime + 1801

        state = server._job_state_from_jsonl(
            self.jsonl, 1800, outputs_dir=self.outputs, now=later)

        self.assertEqual(state["status"], "error")
        self.assertTrue(state["timed_out"])
        self.assertEqual(state["recover_reason"], "timeout")
        self.assertIn("服务重启，任务已中断，请重新审查", state["error"])

    def test_empty_or_broken_progress_file_is_not_running(self):
        self.jsonl.write_text(
            '{"step": "S1", "detail": {"待审对象": "targets/sample.txt"}\n'
            "not json at all\n",
            encoding="utf-8")

        state = server._job_state_from_jsonl(
            self.jsonl, 1800, outputs_dir=self.outputs)

        self.assertEqual(state["status"], "error")
        self.assertNotEqual(state["status"], "running")


class StartupRecoveryTests(unittest.TestCase):
    """_recover_jobs：恢复未过期任务，清掉超过 7 天的进度文件。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.jobs = self.dir / ".jobs"
        self.jobs.mkdir()
        self.outputs = self.dir / "outputs"
        self.outputs.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_recovers_recent_and_prunes_seven_day_old(self):
        recent = self.jobs / "a1b2c3d4e5f6.jsonl"
        _write_jsonl(recent, [_event(
            "S1", "接收解析", time.time() - 30,
            {"待审对象": "targets/sample.txt"})])
        old = self.jobs / "ffffffffffff.jsonl"
        _write_jsonl(old, [_event("S1", "接收解析", time.time() - 9 * 86400)])
        old_mtime = time.time() - (7 * 86400 + 60)
        os.utime(old, (old_mtime, old_mtime))

        with mock.patch.object(server, "JOBS_DIR", self.jobs), \
                mock.patch.object(server, "OUTPUTS", self.outputs), \
                mock.patch.object(server, "REVIEW_TIMEOUT", 1800), \
                mock.patch.dict(server.JOBS, clear=True):
            recovered = server._recover_jobs(now=time.time())
            self.assertEqual(recovered, 1)
            self.assertIn("a1b2c3d4e5f6", server.JOBS)
            self.assertEqual(server.JOBS["a1b2c3d4e5f6"]["status"], "error")
            self.assertNotEqual(server.JOBS["a1b2c3d4e5f6"]["status"], "running")
            self.assertEqual(server.JOBS["a1b2c3d4e5f6"]["timeout"], 1800)

        self.assertFalse(old.exists(), "超过 7 天的 .jobs 进度文件应被清理")
        self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
