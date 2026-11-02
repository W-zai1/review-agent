#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_url_safety.py —— 联网核验的 SSRF 边界。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import research  # noqa: E402


class UrlSafetyTests(unittest.TestCase):
    def test_public_https_is_allowed(self):
        self.assertEqual(
            research.validate_fetch_url("https://8.8.8.8/"),
            "https://8.8.8.8/",
        )

    def test_loopback_private_and_link_local_are_rejected(self):
        for url in (
            "http://127.0.0.1/",
            "http://10.0.0.5/",
            "http://192.168.1.5/",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/",
        ):
            with self.assertRaises(research.UnsafeURLError, msg=url):
                research.validate_fetch_url(url)

    def test_userinfo_and_non_http_are_rejected(self):
        for url in ("https://u:p@8.8.8.8/", "ftp://8.8.8.8/file"):
            with self.assertRaises(research.UnsafeURLError, msg=url):
                research.validate_fetch_url(url)

    def test_external_non_standard_port_is_rejected(self):
        with self.assertRaises(research.UnsafeURLError):
            research.validate_fetch_url("https://8.8.8.8:8443/")

    def test_private_mode_allows_local_test_server(self):
        self.assertEqual(
            research.validate_fetch_url(
                "http://127.0.0.1:54321/", allow_private=True),
            "http://127.0.0.1:54321/",
        )

    def test_fetch_rejects_before_network(self):
        # 不 mock urlopen：非法地址必须在构造请求前就被拒绝。
        with self.assertRaises(research.UnsafeURLError):
            research.fetch_url("http://127.0.0.1:1/")


if __name__ == "__main__":
    unittest.main()
