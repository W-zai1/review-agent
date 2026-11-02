#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_server_v1_reports.py —— 控制台读取/删除 outputs/v1 报告。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import server  # noqa: E402

RID = "R-20261102-201"


class _Handler:
    def __init__(self):
        self.favs = []

    def _favorites(self):
        return list(self.favs)

    def _save_favorites(self, favs):
        self.favs = list(favs)


class V1ReportCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.outputs = self.base / "outputs"
        self.v1 = self.outputs / "v1"
        self.logs = self.base / "logs"
        self.state = self.base / "state"
        self.trash = self.outputs / ".trash"
        for path in (self.v1, self.logs / "v1" / RID, self.state, self.trash):
            path.mkdir(parents=True, exist_ok=True)
        self.names = ("OUTPUTS", "LOG_DIR", "STATE_DIR", "TRASH", "LEDGER",
                      "FAVORITES", "LLM_CACHE_DIR")
        self.saved = {name: getattr(server, name) for name in self.names}
        self.addCleanup(self._restore)
        server.OUTPUTS = self.outputs
        server.LOG_DIR = self.logs
        server.STATE_DIR = self.state
        server.TRASH = self.trash
        server.LEDGER = self.base / "ledger"
        server.FAVORITES = self.base / "config" / "favorites.json"
        server.LLM_CACHE_DIR = self.base / "cache" / "llm"

        self.report = {
            "schema_version": "1.0-v1-shadow",
            "engine": "architecture-v1",
            "review_id": RID,
            "run_id": f"{RID}-RUN01",
            "target_path": "targets/a.md",
            "target_type": "contract",
            "timestamp": "2026-11-02T22:00:00",
            "verdict": "warn",
            "issues": [{"issue_id": "ISS-0001"}],
            "stats": {"major": 1},
            "integrity": {"report_hash": "a" * 64},
            "evidence": [],
            "graph": {"answers": {}},
        }
        (self.v1 / f"{RID}.json").write_text(
            json.dumps(self.report, ensure_ascii=False), encoding="utf-8")
        (self.logs / "v1" / RID / "task.json").write_text(
            json.dumps({"review_id": RID, "engine": "architecture-v1"}),
            encoding="utf-8")

    def _restore(self):
        for name, value in self.saved.items():
            setattr(server, name, value)

    def test_history_lists_v1_report(self):
        data = server.Handler._reports(_Handler())
        self.assertEqual([x["id"] for x in data["items"]], [RID])
        self.assertEqual(data["items"][0]["target_name"], "a.md")
        self.assertEqual(data["items"][0]["engine"], "architecture-v1")

    def test_single_report_reads_v1(self):
        data = server.Handler._report(_Handler(), RID)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["report"]["review_id"], RID)

    def test_logs_fall_back_to_logs_v1(self):
        data = server.Handler._logs(_Handler(), RID)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["task"]["engine"], "architecture-v1")

    def test_delete_v1_only_report(self):
        data = server.Handler._delete_report(
            _Handler(), RID, {"confirm": RID})
        self.assertTrue(data["ok"], data)
        self.assertFalse((self.v1 / f"{RID}.json").exists())
        self.assertTrue(any(p.name.startswith("v1_") for p in self.trash.iterdir()))


if __name__ == "__main__":
    unittest.main()
