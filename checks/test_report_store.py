# -*- coding: utf-8 -*-
"""报告落盘层的独立契约：快照幂等、台账链、JSON/Markdown 一致。"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from review_agent.infrastructure import ledger
from review_agent.infrastructure.report_store import ReportStore


class ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.outputs = self.root / "outputs"
        self.ledger_dir = self.root / "ledger"
        self.state = self.root / "state"
        self.store = ReportStore(
            outputs=self.outputs,
            ledger=self.ledger_dir,
            state=self.state,
            root=self.root,
            compute_hash=lambda report: "a" * 64,
            acquire_lock=lambda: ledger.acquire_lock(self.ledger_dir),
            release_lock=ledger.release_lock,
            now_iso=lambda: "2026-01-01T00:00:00+08:00",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _report(self, review_id="R-2026-01-01-001", run_id="run-1"):
        return {
            "review_id": review_id,
            "run_id": run_id,
            "engine": "architecture-v1",
            "integrity": {
                "target_hash": "b" * 64,
                "rules_dir_hash": "c" * 64,
                "rules_version": "v1",
            },
        }

    def test_snapshot_is_content_addressed_and_idempotent(self):
        first = self.store.write_target_snapshot("同一份原文")
        second = self.store.write_target_snapshot("同一份原文")
        self.assertEqual(first, second)
        self.assertEqual(first["hash"], hashlib.sha256(
            "同一份原文".encode("utf-8")).hexdigest())
        path = self.root / first["ref"]
        self.assertEqual(path.read_text(encoding="utf-8"), "同一份原文")
        self.assertIsNone(self.store.write_target_snapshot(None))
        self.assertIsNone(self.store.write_target_snapshot(""))

    def test_write_report_appends_chain_and_marks_hash(self):
        report = self._report()
        path = self.store.write_report(
            report, official=False, render_markdown=lambda r: f"# {r['review_id']}")
        self.assertEqual(path, self.outputs / "v1" / "R-2026-01-01-001.json")
        self.assertEqual(report["integrity"]["report_hash"], "a" * 64)
        self.assertEqual(report["integrity"]["prev_report_hash"], "0" * 64)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                         report)
        self.assertEqual((self.outputs / "v1" / "R-2026-01-01-001.md")
                         .read_text(encoding="utf-8"),
                         "# R-2026-01-01-001")
        records = ledger.read_records(self.ledger_dir / "v1-chain.log")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["seq"], 1)
        self.assertEqual(records[0]["report_hash"], "a" * 64)

    def test_already_written_by_run_id(self):
        path = self.outputs / "v1" / "R-2026-01-01-001.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"run_id": "run-1"}), encoding="utf-8")
        self.assertTrue(self.store.already_written(path, "run-1"))
        self.assertFalse(self.store.already_written(path, "run-2"))
        path.write_text("{bad json", encoding="utf-8")
        self.assertFalse(self.store.already_written(path, "run-1"))


if __name__ == "__main__":
    unittest.main()
