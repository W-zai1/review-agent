#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_secret_patterns.py —— 凭据正则「只有一份」的守门测试（阶段 3 第二刀）。

背景：`tools/regex_check.py` 与 `tools/secret_scan.py` 原本各写一套密钥正则，
新增一种 token 时很容易只改一处，导致「交付扫描说干净、审查却报密钥」。
现在两族正则都写在 `tools/secret_patterns.py`，两个扫描器各取一个子集。

本测试钉三件事：

    · 身份：两个扫描器拿到的是**同一个对象**，不是复制出来的同值副本
    · 顺序：交付族 8 条的 kind 与次序不变（扫描结果按此顺序产出）
    · 行为：两族的样例仍逐条命中，占位值仍被抑制，且两族不会互相污染

样例一律现拼（如 `"sk-" + "a" * 24`），不在源码里留完整 token 字面量，
否则本文件自己会被 `tools/secret_scan.py` 当成泄露命中。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import regex_check  # noqa: E402
import secret_patterns  # noqa: E402
import secret_scan  # noqa: E402

DELIVERY_KINDS = [
    "OpenAI/DeepSeek 风格 API Key",
    "AWS Access Key",
    "GitHub Token",
    "Slack Token",
    "Google API Key",
    "Stripe Secret Key",
    "私钥文件",
    "疑似硬编码凭据",
]

# kind -> 一定能命中该 kind 的样例（现拼，避免源码出现完整 token）
DELIVERY_SAMPLES = {
    "OpenAI/DeepSeek 风格 API Key": "sk-" + "a" * 24,
    "AWS Access Key": "AKIA" + "A1B2C3D4E5F6G7H8",
    "GitHub Token": "ghp_" + "a" * 24,
    "Slack Token": "xoxb-" + "a" * 12,
    "Google API Key": "AIza" + "a" * 35,
    "Stripe Secret Key": "sk_live_" + "a" * 24,
    "私钥文件": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "疑似硬编码凭据": 'api_key = "' + "a" * 24 + '"',
}

ASSIGNMENT_HITS = [
    'password = "hunter2xyz"',
    "password: 1234567",          # 无引号裸值
    "API_KEY=sk-abcdef123456",    # sk- 前缀
    "密钥: abcdefg",               # 中文键名（半角冒号；全角「：」本就不在旧正则范围内，保持原行为）
]
ASSIGNMENT_MISSES = [
    "password = false",           # 占位值
    "token = None",
    "max_tokens = 2048",          # 键名不同，值过短
]


class SecretPatternsTest(unittest.TestCase):
    # ---- 身份：同一个对象，不是同值副本 ------------------------------------
    def test_delivery_patterns_is_same_object(self):
        self.assertIs(secret_scan.PATTERNS, secret_patterns.DELIVERY_PATTERNS)

    def test_assignment_re_is_same_object(self):
        self.assertIs(regex_check.SECRET_RE, secret_patterns.ASSIGNMENT_RE)
        self.assertIs(regex_check.SECRET_PLACEHOLDER, secret_patterns.PLACEHOLDER_RE)

    # ---- 顺序与种类 -------------------------------------------------------
    def test_delivery_kinds_and_order(self):
        self.assertEqual([kind for kind, _ in secret_scan.PATTERNS], DELIVERY_KINDS)

    # ---- 交付族仍然逐条命中 -----------------------------------------------
    def test_delivery_family_still_hits_every_kind(self):
        for kind, sample in DELIVERY_SAMPLES.items():
            with self.subTest(kind=kind):
                pattern = dict(secret_scan.PATTERNS)[kind]
                self.assertTrue(pattern.search(sample), f"{kind} 不再命中样例")

    def test_scan_text_still_reports_kind(self):
        findings = secret_scan.scan_text(DELIVERY_SAMPLES["GitHub Token"], "checks/样例.md")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "GitHub Token")

    # ---- 评审族仍然命中/抑制 ----------------------------------------------
    def test_assignment_family_still_hits(self):
        for sample in ASSIGNMENT_HITS:
            with self.subTest(sample=sample):
                self.assertTrue(regex_check.SECRET_RE.search(sample), f"漏检：{sample}")

    def test_placeholder_values_still_suppressed(self):
        for sample in ASSIGNMENT_MISSES:
            with self.subTest(sample=sample):
                self.assertEqual(regex_check.find_secret_spans(sample), [])

    def test_lint_secret_findings_still_work(self):
        import lint  # 延迟导入：lint 依赖 regex_check
        findings = lint._secret_findings(ASSIGNMENT_HITS[0])
        self.assertEqual([f["kind"] for f in findings], ["secret"])

    # ---- 两族不互相污染（这是“不合并检测集合”的契约） ---------------------
    def test_delivery_family_ignores_assignment_forms(self):
        for sample in ASSIGNMENT_HITS:
            with self.subTest(sample=sample):
                hits = [k for k, p in secret_scan.PATTERNS if p.search(sample)]
                self.assertEqual(hits, [], f"交付族误吃评审族样例：{sample}")

    def test_assignment_family_ignores_bare_tokens(self):
        for kind in ("OpenAI/DeepSeek 风格 API Key", "GitHub Token", "Google API Key"):
            sample = DELIVERY_SAMPLES[kind]
            with self.subTest(kind=kind):
                self.assertIsNone(regex_check.SECRET_RE.search(sample),
                                  f"评审族误吃裸 token：{sample}")

    # ---- 表里不许长出逻辑 -------------------------------------------------
    def test_module_holds_only_tables(self):
        tree = ast.parse((ROOT / "tools" / "secret_patterns.py").read_text(encoding="utf-8"))
        defs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        self.assertEqual(defs, [], "secret_patterns.py 只应有正则常量，不应有函数/类")
        names = {t.id for n in tree.body if isinstance(n, ast.Assign)
                 for t in n.targets if isinstance(t, ast.Name)}
        self.assertEqual(names, {"ASSIGNMENT_RE", "PLACEHOLDER_RE", "DELIVERY_PATTERNS"})


if __name__ == "__main__":
    unittest.main()
