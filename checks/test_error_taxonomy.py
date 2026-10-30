#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_error_taxonomy.py —— 错误分类：从异常类型到用户能看懂的语义。

阶段 1 的最后一项是「错误类型统一」。验收不是「有没有一个基类」，而是三件事：
    1. 分类有唯一来源（domain.errors），不是各处 if 字符串猜
    2. CLI 把分类**结构化**交给上层（一行 JSON），不再靠正则猜 stderr
    3. server 能读懂这行并原样转发（kind / http_status），不吞、不改写

这里测 1 与 2 的契约 + 3 的解析函数；真实 HTTP 语义由前端按 kind 决定，
因为本项目的 server 返回的是 JSON 载荷（{"ok": false, ...}），不是裸状态码。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import (                       # noqa: E402
    ConfigError,
    DomainError,
    InputError,
    InvariantViolation,
    NotFoundError,
)


class TaxonomyTests(unittest.TestCase):
    def test_default_is_system_error(self):
        exc = DomainError("出了点事")
        self.assertEqual(exc.kind, "system")
        self.assertEqual(exc.http_status, 500)
        self.assertFalse(exc.retryable)

    def test_input_error_is_a_value_error(self):
        # 兼容旧代码的 `except ValueError`
        self.assertTrue(issubclass(InputError, ValueError))
        self.assertEqual(InputError.kind, "input")
        self.assertEqual(InputError.http_status, 400)

    def test_not_found_is_more_specific_than_input(self):
        self.assertTrue(issubclass(NotFoundError, InputError))
        self.assertEqual(NotFoundError.http_status, 404)

    def test_config_error_is_not_the_users_fault(self):
        self.assertEqual(ConfigError.kind, "config")
        self.assertEqual(ConfigError.http_status, 500)
        self.assertFalse(issubclass(ConfigError, InputError))

    def test_invariant_violation_stays_system(self):
        self.assertEqual(InvariantViolation.kind, "system")

    def test_as_dict_is_stable(self):
        info = InputError("路径写错了").as_dict()
        self.assertEqual(set(info), {"kind", "http_status", "retryable", "message"})
        self.assertEqual(info["kind"], "input")
        self.assertEqual(info["message"], "路径写错了")


class ReviewErrorLineTests(unittest.TestCase):
    def test_unreadable_target_is_input_error(self):
        import review
        self.assertTrue(issubclass(review.UnreadableTargetError, InputError))
        exc = review.UnreadableTargetError("坏文件")
        self.assertEqual(exc.kind, "input")
        self.assertEqual(exc.http_status, 400)

    def test_cli_missing_target_reports_structured_error(self):
        proc = subprocess.run(
            [sys.executable, "review.py", "targets/__does_not_exist__.txt"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("找不到待审文件", proc.stdout)      # 人类可读的部分还在
        lines = [ln for ln in proc.stderr.splitlines()
                 if ln.strip().startswith(review_error_marker())]
        self.assertTrue(lines, f"没有结构化错误行：{proc.stderr[:300]}")
        info = json.loads(lines[-1].strip()[len(review_error_marker()):])
        self.assertEqual(info["kind"], "input")
        self.assertEqual(info["http_status"], 404)

    def test_cli_unreadable_target_reports_input_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.docx"
            bad.write_bytes(b"not a zip at all")
            proc = subprocess.run(
                [sys.executable, "review.py", str(bad)],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
            self.assertNotEqual(proc.returncode, 0)
            lines = [ln for ln in proc.stderr.splitlines()
                     if ln.strip().startswith(review_error_marker())]
            self.assertTrue(lines, f"没有结构化错误行：{proc.stderr[:300]}")
            info = json.loads(lines[-1].strip()[len(review_error_marker()):])
            self.assertEqual(info["kind"], "input")


def review_error_marker():
    import review
    return review.ERROR_MARKER


class ServerParsingTests(unittest.TestCase):
    def test_server_reads_the_marker_line(self):
        import server
        raw = ("别的日志\n"
               'REVIEW_ERROR {"kind": "input", "http_status": 404, "retryable": false,'
               ' "message": "找不到待审文件"}\n')
        info = server._parse_review_error(raw)
        self.assertEqual(info["kind"], "input")
        self.assertEqual(info["http_status"], 404)

    def test_server_ignores_output_without_marker(self):
        import server
        self.assertIsNone(server._parse_review_error("普通错误输出\n"))

    def test_server_ignores_broken_marker_json(self):
        import server
        self.assertIsNone(server._parse_review_error("REVIEW_ERROR {不是 json}\n"))

    def test_marker_line_is_not_shown_to_users(self):
        import server
        raw = "目标不可读：坏文件\nREVIEW_ERROR {\"kind\": \"input\"}\n"
        self.assertEqual(server._strip_error_marker(raw), "目标不可读：坏文件")


if __name__ == "__main__":
    unittest.main()
