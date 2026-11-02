#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_release_check.py —— 发布检查的路径与快照约束。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import release_check  # noqa: E402


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True,
                   capture_output=True)


class RequiredPathTests(unittest.TestCase):
    def test_required_paths_are_present(self):
        self.assertEqual(release_check.missing_required(ROOT), [])

    def test_required_paths_have_no_duplicates(self):
        self.assertEqual(
            len(release_check.REQUIRED_PATHS),
            len(set(release_check.REQUIRED_PATHS)),
        )

    def test_missing_required_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = release_check.missing_required(Path(tmp))
            self.assertIn("review.py", missing)


class ArchiveTests(unittest.TestCase):
    def test_export_index_contains_only_staged_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _git(root, "init", "-q")
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            _git(root, "add", "tracked.txt")
            (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")

            out = root / "snapshot"
            out.mkdir()
            clean = release_check.export_index(root, out)

            self.assertTrue((clean / "tracked.txt").is_file())
            self.assertFalse((clean / "untracked.txt").exists())

    def test_export_index_rejects_out_of_tree_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "snapshot"
            out.mkdir()
            with self.assertRaises(release_check.ReleaseCheckError):
                release_check._safe_members(out, [type(
                    "Member", (), {"name": "../escape.txt"})()])


if __name__ == "__main__":
    unittest.main()
