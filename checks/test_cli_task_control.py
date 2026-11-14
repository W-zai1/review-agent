"""CLI 任务控制入口：状态查询、取消分发和参数边界。"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from types import SimpleNamespace

from interfaces.cli import commands, parser


class _Runtime:
    @staticmethod
    def _take_engine(args):
        return "v1", args

    @staticmethod
    def has_run_state(run_id):
        return run_id.endswith("-RUN01")


class ParseTaskControlTests(unittest.TestCase):
    def test_task_cancel_is_an_action_not_a_target(self):
        opts = parser.parse_args(
            ["--task-cancel", "R-20261114-001"], runtime=_Runtime())
        self.assertEqual(opts.task_action, "cancel")
        self.assertEqual(opts.task_review_id, "R-20261114-001")
        self.assertEqual(opts.target_arg, "")

    def test_task_reason_is_optional_and_preserved(self):
        opts = parser.parse_args([
            "--task-cancel", "R-20261114-001",
            "--task-reason", "运维窗口取消",
        ], runtime=_Runtime())
        self.assertEqual(opts.task_reason, "运维窗口取消")

    def test_task_action_requires_review_id(self):
        with self.assertRaises(SystemExit):
            parser.parse_args(["--task-status"], runtime=_Runtime())

    def test_task_recover_does_not_require_review_id(self):
        opts = parser.parse_args(["--task-recover"], runtime=_Runtime())
        self.assertEqual(opts.task_action, "recover")
        self.assertIsNone(opts.task_review_id)


class CommandTaskControlTests(unittest.TestCase):
    def _service(self, record):
        calls = []

        class Service:
            def get_task(self, review_id):
                calls.append(("get", review_id))
                return record

            def cancel(self, review_id, *, reason=""):
                calls.append(("cancel", review_id, reason))
                return SimpleNamespace(
                    status="cancelled",
                    to_dict=lambda: {
                        "review_id": review_id,
                        "status": "cancelled",
                        "error": reason,
                    },
                )

            def recover(self, *, has_checkpoint=None):
                calls.append(("recover", has_checkpoint))
                return SimpleNamespace(
                    resumable=(SimpleNamespace(
                        to_dict=lambda: {"review_id": "R-20261114-002",
                                         "status": "running"}),),
                    orphaned=(),
                    total=1,
                )

            def close(self):
                calls.append(("close",))

        return Service(), calls

    def test_status_prints_the_record(self):
        record = SimpleNamespace(
            review_id="R-20261114-001",
            to_dict=lambda: {"review_id": "R-20261114-001",
                             "status": "running"},
        )
        service, calls = self._service(record)
        runtime = _Runtime()
        runtime.make_task_service = lambda: service

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.main(["--task-status", "R-20261114-001"], runtime=runtime)

        self.assertEqual(json.loads(out.getvalue())["status"], "running")
        self.assertEqual(calls[0], ("get", "R-20261114-001"))
        self.assertEqual(calls[-1], ("close",))

    def test_cancel_uses_task_service_and_reason(self):
        service, calls = self._service(None)
        runtime = _Runtime()
        runtime.make_task_service = lambda: service

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.main([
                "--task-cancel", "R-20261114-001",
                "--task-reason", "运维窗口取消",
            ], runtime=runtime)

        self.assertEqual(
            calls[0], ("cancel", "R-20261114-001", "运维窗口取消"))
        self.assertEqual(json.loads(out.getvalue())["status"], "cancelled")
        self.assertEqual(calls[-1], ("close",))

    def test_recover_reports_resumable_and_orphaned_tasks(self):
        service, calls = self._service(None)
        runtime = _Runtime()
        runtime.make_task_service = lambda: service

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            commands.main(["--task-recover"], runtime=runtime)

        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["resumable"][0]["status"], "running")
        self.assertEqual(payload["orphaned"], [])
        self.assertEqual(calls[0][0], "recover")
        self.assertIs(calls[0][1], runtime.has_run_state)
        self.assertEqual(calls[-1], ("close",))
