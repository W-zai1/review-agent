#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_retention.py —— 保留期计划与执行（批次 2 / C5）。

清理逻辑最容易出的错不是"删不掉"，而是**删过头**：台账是审计凭据、
未结束的任务记录是恢复依据、报告本身是结论。这个文件用三条硬规则把它们钉住：

    1. `ledger/` 永不进计划（连看都不看）
    2. `state/tasks/` 里非终态记录永不进计划（`keep=True` 单独列出）
    3. 只按 mtime 与配置天数比较，不存在"全清"选项

另外钉住 `apply()` 的缺省行为是 **dry-run**：清理工具默认不该有副作用。
"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.infrastructure import retention as ret  # noqa: E402

NOW = datetime.datetime(2026, 11, 1, 12, 0, 0)


def age_file(path: Path, days: int) -> None:
    """把 mtime 改成 N 天前。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    ts = (NOW - datetime.timedelta(days=days)).timestamp()
    os.utime(path, (ts, ts))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def planned(self, config=None):
        return ret.plan(self.root, config=config, now=NOW)

    def test_empty_root_plans_nothing(self):
        self.assertEqual(self.planned().total, 0)

    def test_expired_report_is_planned(self):
        age_file(self.root / "outputs" / "R-20250101-001.json", 400)
        plan = self.planned()
        self.assertEqual(plan.total, 1)
        self.assertEqual(plan.items[0].category, "outputs")
        self.assertEqual(plan.by_category(), {"outputs": 1})

    def test_fresh_report_is_skipped(self):
        age_file(self.root / "outputs" / "R-20261031-001.json", 1)
        plan = self.planned()
        self.assertEqual(plan.total, 0)
        self.assertTrue(plan.skipped)
        self.assertIn("未到期", plan.skipped[0].reason)

    def test_ledger_is_never_planned(self):
        """台账是「发生过什么」的凭据：再旧也不能进计划。"""
        age_file(self.root / "ledger" / "chain.log", 3650)
        age_file(self.root / "ledger" / "v1-chain.log", 3650)
        plan = self.planned({"outputs": 0, "logs": 0, "runs": 0, "cache": 0,
                             "trash": 0, "jobs": 0, "tasks": 0})
        self.assertEqual(plan.total, 0)
        self.assertNotIn("ledger", {i.category for i in plan.items})

    def test_unfinished_task_is_kept_and_reported(self):
        task = self.root / "state" / "tasks" / "R-20250101-001.json"
        task.parent.mkdir(parents=True)
        task.write_text(json.dumps({"status": "running"}), encoding="utf-8")
        ts = (NOW - datetime.timedelta(days=400)).timestamp()
        os.utime(task, (ts, ts))
        plan = self.planned()
        self.assertEqual(plan.total, 0, "未结束的任务记录不能删")
        kept = [i for i in plan.skipped if i.category == "tasks"]
        self.assertTrue(kept and kept[0].keep)
        self.assertIn("未结束", kept[0].reason)

    def test_finished_task_is_planned(self):
        task = self.root / "state" / "tasks" / "R-20250101-001.json"
        task.parent.mkdir(parents=True)
        task.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        ts = (NOW - datetime.timedelta(days=400)).timestamp()
        os.utime(task, (ts, ts))
        plan = self.planned()
        self.assertEqual(plan.total, 1)
        self.assertEqual(plan.items[0].category, "tasks")

    def test_categories_cover_runtime_dirs(self):
        age_file(self.root / "outputs" / ".trash" / "old.json", 100)
        age_file(self.root / "logs" / ".jobs" / "abc.jsonl", 100)
        age_file(self.root / "state" / "runs" / "R-1-RUN01.state.json", 100)
        age_file(self.root / "cache" / "llm" / "abc.json", 100)
        age_file(self.root / "state" / "research_cache" / "x.json", 100)
        plan = self.planned()
        self.assertEqual(set(plan.by_category()),
                         {"trash", "jobs", "runs", "cache"})

    def test_directory_items_are_planned_whole(self):
        log_dir = self.root / "logs" / "R-20250101-001"
        age_file(log_dir / "task.json", 400)
        plan = self.planned()
        dirs = [i for i in plan.items if i.is_dir]
        self.assertTrue(dirs)
        self.assertEqual(dirs[0].path.name, "R-20250101-001")

    def test_config_override_changes_threshold(self):
        age_file(self.root / "outputs" / "R-20261030-001.json", 5)
        self.assertEqual(self.planned().total, 0)
        self.assertEqual(self.planned({"outputs": 3}).total, 1)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_missing_file_uses_defaults(self):
        self.assertEqual(ret.load_config(self.base / "nope.yaml"), ret.DEFAULT_DAYS)

    def test_unknown_keys_and_bad_values_ignored(self):
        path = self.base / "retention.yaml"
        path.write_text("outputs: 30\nunknown: 5\ncache: 'oops'\nlogs: -3\n",
                        encoding="utf-8")
        cfg = ret.load_config(path)
        self.assertEqual(cfg["outputs"], 30)
        self.assertEqual(cfg["logs"], 0, "负数按 0 处理，不是 -3")
        self.assertEqual(cfg["cache"], ret.DEFAULT_DAYS["cache"])
        self.assertNotIn("unknown", cfg)

    def test_broken_yaml_falls_back(self):
        path = self.base / "retention.yaml"
        path.write_text("这不是 yaml: [", encoding="utf-8")
        self.assertEqual(ret.load_config(path), ret.DEFAULT_DAYS)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        age_file(self.root / "outputs" / "R-20250101-001.json", 400)
        age_file(self.root / "cache" / "llm" / "abc.json", 400)

    def test_dry_run_deletes_nothing(self):
        plan = ret.plan(self.root, now=NOW)
        self.assertEqual(plan.total, 2)
        outcome = ret.apply(plan, dry_run=True)
        self.assertEqual(outcome["removed"], 0)
        self.assertTrue((self.root / "outputs" / "R-20250101-001.json").exists())

    def test_apply_removes_planned_files(self):
        plan = ret.plan(self.root, now=NOW)
        outcome = ret.apply(plan, dry_run=False)
        self.assertEqual(outcome["removed"], 2)
        self.assertFalse((self.root / "outputs" / "R-20250101-001.json").exists())
        self.assertFalse((self.root / "cache" / "llm" / "abc.json").exists())

    def test_missing_file_does_not_raise(self):
        plan = ret.plan(self.root, now=NOW)
        (self.root / "outputs" / "R-20250101-001.json").unlink()
        outcome = ret.apply(plan, dry_run=False)
        self.assertEqual(outcome["failed"], 1, "删不到要计数，而不是抛异常中断")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        age_file(self.root / "outputs" / "R-20250101-001.json", 400)

    def test_default_is_dry_run(self):
        code = ret.main(["--root", str(self.root), "--json"])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "outputs" / "R-20250101-001.json").exists(),
                        "不带 --apply 时不能删任何东西")

    def test_apply_flag_deletes(self):
        code = ret.main(["--root", str(self.root), "--apply"])
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "outputs" / "R-20250101-001.json").exists())


if __name__ == "__main__":
    unittest.main()
