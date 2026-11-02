#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_law_library_parity.py —— 法条库段「搬过去了」的验收（批次 2 / A1）。

`law_library` 以前在 v1 的 `unsupported_blocks` 里，理由是「新链路没算」。
搬过来最容易犯的错不是块不存在，而是**两条链路各算一份、结果不一样**：
旧链路的 cited 来自 `_h_law` 处理器写的 session，新链路的规则阶段也跑同一个
处理器——但"也跑"这件事没有任何测试守着，删错一个注册就会悄悄变成空列表，
而报告看起来完全正常（无非是"本次没有引用到条文"）。

所以这里用同一个真实目标跑两条链路，逐键比对 `law_library`：
    * 结构（available / articles_total / laws / cited / skipped）
    * `cited` 的条文集合（这是"引了哪几条"的实际内容）
    * limitations 里的"法律依据仅限已收录的法条库"那段必须都在
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import review                                            # noqa: E402
from review_agent.application import build_shadow_report  # noqa: E402

# 这份样本故意写错法定上限（"试用期最长不得超过 12 个月"），必定触发 LAW-001，
# 于是 cited 非空——空 cited 的比对是没意义的（两边都空也"相等"）。
TARGET = ROOT / "targets" / "selftest" / "st04_fact_law_trial.txt"
REVIEW_ID = "R-20261101-090"


class LawLibraryParityTests(unittest.TestCase):
    def setUp(self):
        if not TARGET.is_file():
            self.skipTest(f"缺测试语料：{TARGET.relative_to(ROOT)}")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.names = ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")
        self.saved = {name: getattr(review, name) for name in self.names}
        self.addCleanup(self._restore)
        for name in ("outputs", "logs", "ledger", "state"):
            (self.base / name).mkdir()
        review.OUTPUTS = self.base / "outputs"
        review.LOGS = self.base / "logs"
        review.LEDGER = self.base / "ledger"
        review.STATE = self.base / "state"
        review.GAP_LOG = self.base / "state" / "gap_log.jsonl"
        self.rules = review.load_rules()

    def _restore(self):
        for name, value in self.saved.items():
            setattr(review, name, value)

    def _legacy(self):
        return review.run_one(self._rel_target(), self.rules, use_llm=False)

    def _v1(self):
        state, outcome, session = review.run_typed_pipeline(
            self._rel_target(), self.rules, review_id=REVIEW_ID, use_llm=False)
        self.assertTrue(outcome.ok, outcome.errors)
        block = review.law_library_block(session)[0]
        report = build_shadow_report(
            state, target_meta=state.artifacts.get("target_meta"),
            events=outcome.events, checkpoints=outcome.checkpoints,
            logs_hash=None, law_library=block)
        return report, session

    @staticmethod
    def _rel_target():
        return Path(str(TARGET))

    def cited_keys(self, block):
        return sorted((c.get("law"), c.get("article")) for c in block.get("cited") or [])

    def test_shapes_match(self):
        legacy = self._legacy()
        report, _session = self._v1()
        old, new = legacy["law_library"], report["law_library"]
        self.assertIsNotNone(new, "v1 报告必须带 law_library 段")
        for key in ("available", "articles_total", "laws", "cited", "skipped"):
            self.assertIn(key, new, f"缺键 {key}")
        self.assertEqual(set(old), set(new), "键集合必须与旧报告一致")
        self.assertEqual(old["articles_total"], new["articles_total"])
        self.assertEqual([law["id"] for law in old["laws"]],
                         [law["id"] for law in new["laws"]])

    def test_cited_articles_match(self):
        """新旧链路引用到的条文必须逐条一致（这是"搬对了"的实质证据）。"""
        legacy = self._legacy()
        report, _session = self._v1()
        old = self.cited_keys(legacy["law_library"])
        new = self.cited_keys(report["law_library"])
        self.assertTrue(old, "语料没有触发 LAW-001，这条用例就退化成空比空，"
                             "请换一份能命中法条的样本")
        self.assertEqual(old, new)

    def test_limitations_carry_law_scope_note(self):
        report, _session = self._v1()
        self.assertTrue(any("法律依据仅限已收录的法条库" in item
                            for item in report["limitations"]))

    def test_unsupported_blocks_no_longer_lists_law_library(self):
        report, _session = self._v1()
        blocks = [item["block"] for item in report["unsupported_blocks"]]
        self.assertNotIn("law_library", blocks)


if __name__ == "__main__":
    unittest.main()
