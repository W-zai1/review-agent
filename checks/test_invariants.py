#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_invariants.py —— 可公开、可进 CI 的最小不变量集。

为什么单独放 `checks/` 而不是 `tests/`：
`tests/` 在本仓库被 .gitignore 排除（对抗样本/漏洞细节不公开），所以从
远程 clone 出来的仓库**没有那 600 多项回归测试**。CI 如果只跑 `tests/`，
实际是 0 项。这里放的是不含敏感样本、但能守住本轮修复的少量硬约束：
法条分档、覆盖率真话、控制台安全边界、版本单一事实源。

运行：
    python -m unittest discover -s checks -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import law_cite  # noqa: E402
import llm_client  # noqa: E402
import review  # noqa: E402
import server  # noqa: E402
import version  # noqa: E402


class LawTierInvariants(unittest.TestCase):
    """第十九条是分档的：不能再用扁平的 6 个月放行一年期合同。"""

    def test_one_year_contract_four_month_probation_is_illegal(self):
        issues, verified, skipped = law_cite.check(
            "本合同期限为一年，试用期为四个月。")
        self.assertEqual(len(issues), 1, issues)
        self.assertEqual(issues[0]["citation"]["article_no"], 19)
        self.assertIn("2 个月", issues[0]["evidence"])
        self.assertEqual(verified, [])
        self.assertEqual(skipped, [])

    def test_unknown_term_is_not_reported_as_verified(self):
        issues, verified, skipped = law_cite.check("试用期为三个月。")
        self.assertEqual(issues, [])
        self.assertEqual(verified, [], "期限未知时绝不能给“已核验”")
        self.assertTrue(any("档位无法确定" in s for s in skipped), skipped)

    def test_unknown_term_over_six_is_still_illegal(self):
        issues, _verified, _skipped = law_cite.check("试用期为十二个月。")
        self.assertEqual(len(issues), 1)
        self.assertIn("六个月", issues[0]["evidence"])

    def test_chinese_percentage_is_checked(self):
        issues, _verified, _skipped = law_cite.check(
            "试用期工资为转正工资的百分之六十。")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["citation"]["article_no"], 20)


class CoverageTruthInvariants(unittest.TestCase):
    """失败块必须让 complete=False，不能继续打印“全覆盖”。"""

    def test_failed_chunk_is_not_full_coverage(self):
        cov = review.coverage_from_llm_meta(
            {"chars_reviewed": 60, "chunks_attempted": 3, "chunks_succeeded": 2,
             "failed_chunks": [{"dim": "fact", "chunk": 2, "start": 60, "end": 100}]},
            100)
        self.assertFalse(cov["complete"])
        self.assertEqual(cov["chars_reviewed"], 60)
        self.assertEqual(cov["text_coverage_pct"], 60)

    def test_zero_reviewed_does_not_fall_back_to_full_text(self):
        cov = review.coverage_from_llm_meta(
            {"chars_reviewed": 0, "chunks_attempted": 1, "chunks_succeeded": 0,
             "failed_chunks": [{"dim": "fact", "chunk": 1, "start": 0, "end": 100}]},
            100)
        self.assertFalse(cov["complete"])
        self.assertEqual(cov["chars_reviewed"], 0)

    def test_rule_only_is_not_a_failure(self):
        cov = review.coverage_from_llm_meta({}, 100)
        self.assertTrue(cov["complete"])
        self.assertEqual(cov["chars_reviewed"], 100)


class ConsoleSecurityInvariants(unittest.TestCase):
    """控制台的三个硬边界：mask 不回尾、userinfo 拒绝、Origin 带端口。"""

    def test_masked_key_does_not_expose_tail(self):
        key = "sk-abcdefghijklmnopqrstuvwxyz123456"
        masked = llm_client.LLMConfig(api_key=key).masked_key()
        self.assertNotIn(key[-4:], masked)
        self.assertLessEqual(len(masked), 5)

    def test_userinfo_url_is_detected(self):
        self.assertTrue(server._url_has_userinfo("https://u:p@api.example.com/v1"))
        self.assertFalse(server._url_has_userinfo("https://api.example.com/v1"))

    def test_origin_keeps_port(self):
        self.assertEqual(server._origin_parts("http://127.0.0.1:5173"),
                         ("http", "127.0.0.1", 5173))
        self.assertIsNone(server._origin_parts("null"))


class VersionInvariants(unittest.TestCase):
    """版本号只能有一个可写处：根目录 VERSION。"""

    def test_version_file_is_readable(self):
        self.assertRegex(version.read_version(), r"^\d+\.\d+\.\d+")

    def test_readme_title_uses_same_version(self):
        first = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(version.__version__, first)

    def test_report_uses_same_version(self):
        self.assertEqual(review.REPORT_MODEL_VERSION, version.report_version())

    def test_console_uses_same_version(self):
        self.assertIn(version.__version__, server.Handler.server_version)


if __name__ == "__main__":
    unittest.main()
