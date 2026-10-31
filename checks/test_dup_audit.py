#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_dup_audit.py —— 把「不重复实现」变成可执行的约束。

`docs/PHASE0_INVENTORY.md` §5 列了 27 个 `main`、6 个 `summarize`……那是**信号**，
不是「抄了两份」的结论：同名函数可能只是都叫 `main` 的 CLI 入口。
只有比**函数体**才分得清，所以这里调 `tools/dup_audit.py` 的扫描结果做断言。

三条不变量：

    1. `review_agent/` 内部不许出现「函数体逐字相同、跨模块」的实现
    2. `review_agent/` 与旧 `tools/` 之间不许出现这种重复——
       新包一旦复制旧实现，「唯一口径」就是空话
    3. 同名本身不是问题（`main` / `summarize` 都是同名不同物），
       所以断言的是「同名很多」**且**「其中没有重复体」

已知例外：`tools/arith_check.run_all` 与 `tools/logic_checks.run_all` 结构相同、
只差各自的 `CHECKS` 常量。它们是旧代码里的「同形不同数据」，合并需要改两边的
模块级常量——收益低、回归面大，登记为已知形态而不是悄悄放过。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import dup_audit  # noqa: E402

# 旧 tools/ 里已知的「同形不同数据」：不算新包复制，也不打算在这一轮合并
KNOWN_LEGACY_SHAPE = {"run_all"}


class DuplicateAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = dup_audit.scan()
        cls.duplicates = cls.report["duplicates"]

    def test_report_has_expected_shape(self):
        self.assertIn("by_name", self.report)
        self.assertIn("duplicates", self.report)
        self.assertGreater(len(self.report["by_name"]), 5,
                           "扫描应该看到明显多于 5 个同名函数")

    def test_no_duplicates_inside_review_agent(self):
        inside = [d for d in self.duplicates
                  if any(m.startswith("review_agent/") for m in d["modules"])]
        self.assertEqual(
            inside, [],
            "review_agent/ 内部出现函数体逐字相同的实现："
            + "; ".join(f"{d['name']} ← {d['modules']}" for d in inside))

    def test_no_duplicates_between_new_package_and_legacy_tools(self):
        crossing = [d for d in self.duplicates
                    if any(m.startswith("review_agent/") for m in d["modules"])
                    and any(m.startswith("tools/") for m in d["modules"])]
        self.assertEqual(
            crossing, [],
            "新包复制了旧实现（应改成注入或委托）："
            + "; ".join(f"{d['name']} ← {d['modules']}" for d in crossing))

    def test_remaining_duplicates_are_the_known_legacy_shape(self):
        """剩下的重复只允许在旧 tools/ 内部，且命中已登记名单。"""
        remaining = {d["name"] for d in self.duplicates}
        self.assertTrue(
            remaining <= KNOWN_LEGACY_SHAPE,
            f"出现未登记的新重复：{sorted(remaining - KNOWN_LEGACY_SHAPE)}；"
            "确认是「同形不同数据」后加进 KNOWN_LEGACY_SHAPE 并写理由")

    def test_same_name_is_not_treated_as_duplication(self):
        """`main` 出现在二十多个模块里，但它们不是同一份实现。"""
        self.assertGreaterEqual(self.report["by_name"].get("main", 0), 5)
        self.assertNotIn("main", {d["name"] for d in self.duplicates})


if __name__ == "__main__":
    unittest.main()
