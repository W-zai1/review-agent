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


class SnapshotBoundaryTests(unittest.TestCase):
    def _root(self, tmp: str, *paths: str) -> Path:
        root = Path(tmp)
        for rel in paths:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x\n", encoding="utf-8", newline="")
        return root

    def test_allowed_snapshot_has_no_violations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "review.py", "docs/a.md", "config/llm.yaml")
            self.assertEqual(release_check.snapshot_violations(root), [])

    def test_runtime_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "review.py", "outputs/R-1.json")
            problems = release_check.snapshot_violations(root)
            self.assertTrue(any("outputs" in p for p in problems), problems)

    def test_unknown_top_level_entry_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "review.py", "notes/secret.txt")
            problems = release_check.snapshot_violations(root)
            self.assertTrue(any("notes" in p for p in problems), problems)

    def test_local_llm_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "config/llm.local.json")
            problems = release_check.snapshot_violations(root)
            self.assertTrue(any("llm.local.json" in p for p in problems), problems)

    def test_dotenv_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, ".env")
            problems = release_check.snapshot_violations(root)
            self.assertTrue(any(".env" in p for p in problems), problems)

    def test_key_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "docs/server.key")
            problems = release_check.snapshot_violations(root)
            self.assertTrue(any("server.key" in p for p in problems), problems)

    def test_runtime_state_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "config/favorites.json",
                              "config/rules.state.json")
            problems = release_check.snapshot_violations(root)
            joined = "\n".join(problems)
            self.assertIn("favorites.json", joined)
            self.assertIn("rules.state.json", joined)

    def test_manifest_counts_files_and_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, "review.py", "docs/a.md", "config/llm.yaml")
            manifest = release_check.snapshot_manifest(root)
            self.assertEqual(manifest["files"], 3)
            self.assertEqual(manifest["bytes"], 6)
            self.assertEqual(manifest["top_level"], ["config", "docs", "review.py"])

    @unittest.skipUnless((ROOT / ".git").exists(),
                         "解压出来的发布包没有 .git，白名单一致性由仓库内 CI 保证")
    def test_tracked_top_level_entries_are_whitelisted(self):
        out = subprocess.run(["git", "-c", "core.quotepath=false", "ls-files"],
                             cwd=str(ROOT),
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        names = {line.split("/")[0] for line in out.stdout.splitlines() if line}
        self.assertFalse(names - release_check.RELEASE_TOP_LEVEL,
                         "跟踪中的顶层条目缺少白名单："
                         + ", ".join(sorted(names - release_check.RELEASE_TOP_LEVEL)))


if __name__ == "__main__":
    unittest.main()
