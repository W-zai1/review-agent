#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_endpoint_security.py —— 模型端点的传输安全边界。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import llm_client  # noqa: E402
import server  # noqa: E402


class EndpointSecurityTests(unittest.TestCase):
    def test_remote_https_is_allowed(self):
        self.assertIsNone(
            llm_client.endpoint_security_error("https://api.example.com/v1"))

    def test_remote_http_is_rejected(self):
        error = llm_client.endpoint_security_error("http://api.example.com/v1")
        self.assertTrue(error)
        self.assertIn("https://", error)

    def test_local_loopback_http_is_allowed(self):
        for url in ("http://127.0.0.1:11434/v1",
                    "http://localhost:11434/v1",
                    "http://[::1]:11434/v1"):
            self.assertIsNone(llm_client.endpoint_security_error(url), url)

    def test_private_lan_http_is_allowed(self):
        self.assertIsNone(
            llm_client.endpoint_security_error("http://192.168.1.20:11434/v1"))

    def test_userinfo_is_rejected_even_on_localhost(self):
        self.assertTrue(llm_client.endpoint_security_error(
            "http://user:pass@127.0.0.1:11434/v1"))

    def test_non_http_scheme_is_rejected(self):
        self.assertTrue(llm_client.endpoint_security_error(
            "ftp://api.example.com/v1"))

    def test_missing_host_is_rejected(self):
        self.assertTrue(llm_client.endpoint_security_error("https:///v1"))

    def test_insecure_remote_is_not_available_even_with_key(self):
        cfg = llm_client.LLMConfig(
            base_url="http://api.example.com/v1", model="m", api_key="x")
        self.assertFalse(cfg.available)
        self.assertTrue(cfg.endpoint_error)

    def test_server_save_rejects_remote_http(self):
        result = server.Handler._save_llm_config(
            None, {"base_url": "http://api.example.com/v1", "model": "m"})
        self.assertFalse(result["ok"])
        self.assertIn("https://", result["error"])

    def test_server_test_rejects_remote_http(self):
        result = server.Handler._test_llm(
            None, {"base_url": "http://api.example.com/v1",
                   "model": "m", "api_key": "x"})
        self.assertFalse(result["ok"])
        self.assertIn("https://", result["error"])

    def test_server_restore_rejects_remote_http(self):
        result = server.Handler._restore_llm_history(
            None, {"base_url": "http://api.example.com/v1", "model": "m"})
        self.assertFalse(result["ok"])
        self.assertIn("https://", result["error"])


if __name__ == "__main__":
    unittest.main()
