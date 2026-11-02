#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_secret_scan.py —— 凭据扫描器的行为约束。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import secret_scan  # noqa: E402


class SecretPatternTests(unittest.TestCase):
    def test_openai_style_key_is_found_and_redacted(self):
        token = "sk-" + "a" * 32
        findings = secret_scan.scan_text(f'key = "{token}"', "config/demo.py")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "config/demo.py")
        self.assertNotIn(token, findings[0].describe())
        self.assertIn("[REDACTED]", findings[0].describe())

    def test_private_key_header_is_found(self):
        header = "-----BEGIN " + "PRIVATE KEY-----"
        findings = secret_scan.scan_text(
            header + "\nabc\n", "config/key.pem")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "私钥文件")

    def test_clean_text_has_no_findings(self):
        self.assertEqual(
            secret_scan.scan_text("api_key: \"\"\n# 说明文字\n", "config/llm.yaml"),
            [],
        )

    def test_known_fixture_is_allowed_only_in_its_file(self):
        token = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
        self.assertEqual(
            secret_scan.scan_text(f'key = "{token}"', "checks/test_invariants.py"),
            [],
        )
        self.assertEqual(
            len(secret_scan.scan_text(f'key = "{token}"', "config/demo.py")),
            1,
        )

    def test_tree_scan_skips_runtime_directories(self):
        token = "sk-" + "b" * 32
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "targets").mkdir()
            (root / "targets" / "secret.txt").write_text(token, encoding="utf-8")
            findings, scanned = secret_scan.scan_tree(root)
            self.assertEqual(findings, [])
            self.assertEqual(scanned, 0)

    def test_current_repository_has_no_secret(self):
        findings, _scanned = secret_scan.scan_repo(ROOT)
        self.assertEqual(findings, [], "\n".join(f.describe() for f in findings))


if __name__ == "__main__":
    unittest.main()
