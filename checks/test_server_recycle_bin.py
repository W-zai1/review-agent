#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回收站：可恢复删除、禁止覆盖、彻底删除与审计链恢复事件。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from review_agent.infrastructure import ledger  # noqa: E402

RID = "R-20261103-001"


class _Handler:
    def __init__(self):
        self.favs = []

    def _favorites(self):
        return list(self.favs)

    def _save_favorites(self, favs):
        self.favs = list(favs)


def _entry(seq=1, review_id=RID):
    return {
        "seq": seq,
        "review_id": review_id,
        "target_hash": "a" * 64,
        "report_hash": "b" * 64,
        "prev_hash": "0" * 64,
        "ts": "2026-11-03T10:00:00+08:00",
    }


class RecycleBinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.outputs = self.base / "outputs"
        self.logs = self.base / "logs"
        self.state = self.base / "state"
        self.trash = self.outputs / ".trash"
        for path in (self.outputs / "v1", self.logs / "v1", self.state / "runs",
                     self.state / "text", self.base / "ledger",
                     self.state / "research_cache", self.trash):
            path.mkdir(parents=True, exist_ok=True)
        names = ("ROOT", "OUTPUTS", "LOG_DIR", "STATE_DIR", "TRASH", "LEDGER",
                 "FAVORITES", "LLM_CACHE_DIR")
        self.saved = {name: getattr(server, name) for name in names}
        self.addCleanup(self._restore_globals)
        server.ROOT = self.base
        server.OUTPUTS = self.outputs
        server.LOG_DIR = self.logs
        server.STATE_DIR = self.state
        server.TRASH = self.trash
        server.LEDGER = self.base / "ledger"
        server.FAVORITES = self.base / "config" / "favorites.json"
        server.LLM_CACHE_DIR = self.base / "cache" / "llm"
        self.handler = _Handler()

    def _restore_globals(self):
        for name, value in self.saved.items():
            setattr(server, name, value)

    def _seed(self):
        report = {
            "review_id": RID,
            "target_path": "targets/demo.txt",
            "target_name": "demo.txt",
            "verdict": "warn",
            "issues": [],
            "target": {"snapshot_ref": f"state/text/{RID}-snapshot.txt"},
        }
        (self.outputs / "v1" / f"{RID}.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8")
        (self.outputs / "v1" / f"{RID}.md").write_text("# 报告", encoding="utf-8")
        (self.logs / "v1" / RID).mkdir()
        (self.logs / "v1" / RID / "task.json").write_text("{}", encoding="utf-8")
        (self.state / "runs" / f"{RID}-RUN01.state.json").write_text(
            "{}", encoding="utf-8")
        (self.state / "text" / f"{RID}-snapshot.txt").write_text(
            "原文", encoding="utf-8")
        ledger.append(server.LEDGER / "chain.log", _entry())

    def _delete(self):
        return server.Handler._delete_report(
            self.handler, RID, {"confirm": RID})

    def test_delete_lists_and_restore_moves_every_registered_file_back(self):
        self._seed()
        deleted = self._delete()
        self.assertTrue(deleted["ok"], deleted)
        listed = server.Handler._trash_reports(self.handler)
        self.assertEqual([item["id"] for item in listed["items"]], [RID])
        self.assertTrue((self.trash / ".manifests" / f"{RID}.json").is_file())

        restored = server.Handler._restore_report(self.handler, {"id": RID})
        self.assertTrue(restored["ok"], restored)
        self.assertTrue(restored["ledger_restore"], restored)
        self.assertTrue((self.outputs / "v1" / f"{RID}.json").is_file())
        self.assertTrue((self.outputs / "v1" / f"{RID}.md").is_file())
        self.assertTrue((self.logs / "v1" / RID / "task.json").is_file())
        self.assertTrue((self.state / "runs" / f"{RID}-RUN01.state.json").is_file())
        self.assertTrue((self.state / "text" / f"{RID}-snapshot.txt").is_file())
        self.assertFalse((self.trash / ".manifests" / f"{RID}.json").exists())
        self.assertEqual(server.Handler._trash_reports(self.handler)["items"], [])

        _ok, _message, stats = ledger.verify(server.LEDGER / "chain.log")
        self.assertEqual(stats["deleted"], [])
        self.assertEqual(stats["restored"], [1])

    def test_restore_refuses_to_overwrite_existing_report(self):
        self._seed()
        self.assertTrue(self._delete()["ok"])
        (self.outputs / "v1" / f"{RID}.json").write_text("{}", encoding="utf-8")
        result = server.Handler._restore_report(self.handler, {"id": RID})
        self.assertFalse(result["ok"])
        self.assertIn("拒绝覆盖", result["error"])
        self.assertEqual(
            (self.outputs / "v1" / f"{RID}.json").read_text(encoding="utf-8"), "{}")

    def test_permanent_delete_requires_confirmation(self):
        self._seed()
        self.assertTrue(self._delete()["ok"])
        denied = server.Handler._purge_trash_report(self.handler, RID, {})
        self.assertFalse(denied["ok"])
        self.assertTrue(denied["need_confirm"])
        self.assertTrue(server.Handler._trash_reports(self.handler)["items"])

        removed = server.Handler._purge_trash_report(
            self.handler, RID, {"confirm": RID})
        self.assertTrue(removed["ok"], removed)
        self.assertEqual(server.Handler._trash_reports(self.handler)["items"], [])
        self.assertFalse((self.trash / f"v1_{RID}.json").exists())

    def test_empty_trash_requires_explicit_empty_token(self):
        self._seed()
        self.assertTrue(self._delete()["ok"])
        denied = server.Handler._empty_trash(self.handler, {"confirm": RID})
        self.assertFalse(denied["ok"])
        allowed = server.Handler._empty_trash(self.handler, {"confirm": "EMPTY"})
        self.assertTrue(allowed["ok"], allowed)
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_legacy_trash_without_manifest_can_be_restored(self):
        report = {
            "review_id": RID,
            "target_path": "targets/demo.txt",
            "verdict": "pass",
            "issues": [],
        }
        (self.trash / f"{RID}.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8")
        (self.trash / f"{RID}.md").write_text("# old", encoding="utf-8")
        (self.trash / f"{RID}_logs").mkdir()
        (self.trash / f"{RID}_logs" / "task.json").write_text(
            "{}", encoding="utf-8")

        result = server.Handler._restore_report(self.handler, {"id": RID})
        self.assertTrue(result["ok"], result)
        self.assertTrue((self.outputs / f"{RID}.json").is_file())
        self.assertTrue((self.outputs / f"{RID}.md").is_file())
        self.assertTrue((self.logs / RID / "task.json").is_file())


class RecycleBinUiContractTests(unittest.TestCase):
    """锁住“一键移入回收站 + 撤销 + 回收站恢复”的前端主路径。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        cls.app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "web" / "workspace.css").read_text(encoding="utf-8")

    def test_history_has_recycle_bin_entry_and_modal(self):
        for token in ('id="historyTrash"', 'id="trashModal"',
                      'id="trashList"', 'id="trashEmpty"'):
            self.assertIn(token, self.html)

    def test_trash_button_is_one_click_and_has_no_soft_delete_confirmation(self):
        self.assertIn("await trashReport(btn.dataset.del)", self.app)
        self.assertNotIn("askDeleteReport(", self.app)
        self.assertNotIn("pendingDeleteId", self.app)

    def test_toast_offers_undo_and_all_recycle_endpoints_are_wired(self):
        for token in ('label: "撤销"', "ttl: 8000",
                      '"/api/reports/trash/restore"',
                      '"/api/reports/trash"', 'confirm: "EMPTY"'):
            self.assertIn(token, self.app)

    def test_delete_control_is_visible_without_hover_on_touch_devices(self):
        self.assertIn("@media (hover: none)", self.css)
        self.assertIn(".history-del { opacity: 1; }", self.css)


if __name__ == "__main__":
    unittest.main()
