#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_lint.py —— `tools/lint.py` 的两条链路与五种外部结局。

决策书 4.8 的验收口径：外部程序「不存在 / 无执行权限 / 超时 / 非零退出 /
正常」五种情况都必须给出明确结果，并且**无论 linter 是否可用，纯 Python
密钥扫描都要执行**。

为什么注入假的 `subprocess.run` 而不真装 ruff/eslint：
- 这台机器上两者都没装（`shutil.which` 均为 None），"正常"与"非零退出"
  这两类根本跑不出来；
- "无执行权限"更不可能靠改系统 ACL 反复复现，那个副作用不该进测试。
被测的是 `run_lint` 对这些结局的**翻译**，不是 ruff 本身。
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import lint  # noqa: E402

# 用 regex_check 认的形态（`sk-` 前缀的新规则在 secret_patterns 里，不归 lint 管）
SECRET_LINE = 'password = "hunter2xyz"\n'


class RunLintTests(unittest.TestCase):
    """五种外部结局 × 密钥扫描是否照跑。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    # ---------------------------------------------------------------- helpers
    def _file(self, text, name="sample.py"):
        path = self.base / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _run(self, path, *, error=None, completed=None):
        fake = mock.Mock()
        if error is not None:
            fake.side_effect = error
        else:
            fake.return_value = completed
        with mock.patch.object(lint.subprocess, "run", fake):
            return lint.run_lint(path)

    @staticmethod
    def _proc(returncode, stdout="", stderr=""):
        return subprocess.CompletedProcess(["ruff"], returncode,
                                           stdout=stdout, stderr=stderr)

    # ------------------------------------------------------------------ 正常
    def test_clean_run_is_ok(self):
        result = self._run(self._file("x = 1\n"), completed=self._proc(0))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["lint_status"], "ok")
        self.assertEqual(result["secret_status"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["findings"], [])

    def test_normal_run_parses_findings(self):
        path = self._file("import json\n")
        out = f"{path}:1:1: F401 'json' imported but unused\n"
        result = self._run(path, completed=self._proc(1, stdout=out))
        # ruff 用退出码 1 表示「有告警」，这是正常结局，不是崩溃
        self.assertEqual(result["lint_status"], "issues")
        self.assertEqual(result["status"], "issues")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["findings"][0]["code"], "F401")

    def test_exit_one_without_parseable_output_is_not_a_pass(self):
        """退出码 1 却解析不出告警：结果不可信，不能报 ok。"""
        result = self._run(self._file("x = 1\n"),
                           completed=self._proc(1, stderr="unknown output shape"))
        self.assertEqual(result["lint_status"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertIn("未解析", result["details"])

    # ------------------------------------------------------------- 不存在
    def test_missing_linter_is_skipped_and_secret_still_scanned(self):
        path = self._file(SECRET_LINE)
        result = self._run(path, error=FileNotFoundError(2, "No such file", "ruff"))
        self.assertEqual(result["lint_status"], "skipped")
        self.assertIn("未安装", result["details"])
        self.assertEqual(result["secret_status"], "issues")
        self.assertEqual(len(result["secret_findings"]), 1,
                         "linter 没装也必须扫出硬编码口令")
        self.assertEqual(result["status"], "issues")

    # --------------------------------------------------------- 无执行权限
    def test_permission_error_is_reported_and_secret_still_scanned(self):
        path = self._file(SECRET_LINE)
        result = self._run(path, error=PermissionError(13, "Permission denied"))
        self.assertEqual(result["lint_status"], "failed",
                         "「程序在但跑不起来」必须与「没装」区分开")
        self.assertIn("权限", result["details"])
        self.assertEqual(len(result["secret_findings"]), 1,
                         "权限异常不许把密钥扫描一起吞掉")
        # 有密钥命中 → 综合结论是 issues；lint 轴单独记 failed
        self.assertEqual(result["status"], "issues")
        self.assertEqual(result["lint_status"], "failed")

    def test_permission_error_without_findings_is_failed(self):
        result = self._run(self._file("x = 1\n"),
                           error=PermissionError(13, "Permission denied"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["lint_status"], "failed")
        self.assertEqual(result["secret_status"], "ok")

    def test_other_oserror_is_failed(self):
        result = self._run(self._file("x = 1\n"),
                           error=OSError(22, "invalid argument"))
        self.assertEqual(result["lint_status"], "failed")
        self.assertIn("无法执行", result["details"])
        self.assertEqual(result["status"], "failed")

    # --------------------------------------------------------------- 超时
    def test_timeout_is_reported_and_secret_still_scanned(self):
        path = self._file(SECRET_LINE)
        result = self._run(path, error=subprocess.TimeoutExpired(["ruff"], 30))
        self.assertEqual(result["lint_status"], "failed")
        self.assertIn("超时", result["details"])
        self.assertEqual(len(result["secret_findings"]), 1)
        self.assertEqual(result["status"], "issues")

    # ----------------------------------------------------------- 非零退出
    def test_nonzero_exit_is_failed_not_ok(self):
        result = self._run(self._file("x = 1\n"),
                           completed=self._proc(2, stderr="ruff internal error"))
        self.assertEqual(result["status"], "failed",
                         "退出码 2 是工具崩溃，不能当成 ok")
        self.assertEqual(result["lint_status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("退出码 2", result["details"])

    def test_nonzero_exit_with_secret_keeps_both_axes(self):
        result = self._run(self._file(SECRET_LINE),
                           completed=self._proc(2, stderr="eslint config error"))
        self.assertEqual(result["status"], "issues")
        self.assertEqual(result["lint_status"], "failed")
        self.assertEqual(result["secret_status"], "issues")
        self.assertEqual(len(result["secret_findings"]), 1)

    # --------------------------------------------- 密钥扫描自身失败 / 非代码
    def test_unreadable_file_marks_secret_scan_failed(self):
        """文件读不了：密钥扫描「没做成」，不能记成「没命中」。"""
        directory = self.base / "boom.py"
        directory.mkdir()
        result = self._run(str(directory), error=FileNotFoundError(2, "missing", "ruff"))
        self.assertEqual(result["secret_status"], "failed")
        self.assertEqual(result["status"], "failed")
        self.assertIn("密钥扫描", result["details"])

    def test_non_code_file_skips_lint_but_scans_secret(self):
        path = self._file(SECRET_LINE, name="notes.txt")
        result = self._run(path, completed=self._proc(0))
        self.assertEqual(result["lint_status"], "skipped")
        self.assertIn("非代码文件", result["details"])
        self.assertEqual(len(result["secret_findings"]), 1)
        self.assertEqual(result["status"], "issues")

    def test_return_shape_is_unchanged_for_callers(self):
        """旧键必须还在：review.py / TextLintTool 都按它们取值。"""
        result = self._run(self._file("x = 1\n"), completed=self._proc(0))
        for key in ("file", "status", "details", "findings", "secret_findings"):
            self.assertIn(key, result, f"旧口径缺了键 {key}")


if __name__ == "__main__":
    unittest.main()
