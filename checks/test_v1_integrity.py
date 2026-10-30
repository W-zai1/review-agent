#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_v1_integrity.py —— 影子报告的完整性：哈希 + 独立链。

报告是交付物，读者必须能验证「这份结论没被改过」。v1 之前没有这一层，
是 `unsupported_blocks` 里最要命的一项（比缺 purpose 更严重：结论可被篡改而无人察觉）。

验收四条：
    1. `integrity.report_hash` 可由文件本身复算出来（同一套 `compute_report_hash`）
    2. 改一个字段，哈希就对不上（能证伪）
    3. `ledger/v1-chain.log` 是可校验的链：序号连续、prev 接得上
    4. 独立成链：不污染官方 `ledger/chain.log`
"""
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import integrity  # noqa: E402
import review  # noqa: E402


class V1ChainTests(unittest.TestCase):
    def setUp(self):
        self._saved = {name: getattr(review, name)
                       for name in ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")}
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger", "state"):
            (self.base / name).mkdir()
        review.OUTPUTS = self.base / "outputs"
        review.LOGS = self.base / "logs"
        review.LEDGER = self.base / "ledger"
        review.STATE = self.base / "state"
        review.GAP_LOG = self.base / "state" / "gap_log.jsonl"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(review, name, value)
        self.tmp.cleanup()

    def run_v1(self):
        with mock.patch.object(sys, "argv", [
                "review.py", "targets/contract_001.md", "--no-llm",
                "--engine", "v1"]), redirect_stdout(_null()):
            review.main()

    def reports(self):
        return sorted((self.base / "outputs" / "v1").glob("*.json"))

    def chain(self):
        return self.base / "ledger" / "v1-chain.log"

    def test_report_hash_is_reproducible(self):
        self.run_v1()
        report = json.loads(self.reports()[-1].read_text(encoding="utf-8"))
        self.assertEqual(integrity.compute_report_hash(report),
                         report["integrity"]["report_hash"])

    def test_tampering_breaks_the_hash(self):
        self.run_v1()
        report = json.loads(self.reports()[-1].read_text(encoding="utf-8"))
        report["verdict"] = "pass"
        self.assertNotEqual(integrity.compute_report_hash(report),
                            report["integrity"]["report_hash"])

    def test_chain_is_verifiable_and_sequential(self):
        self.run_v1()
        self.run_v1()
        ok, message = integrity.verify_ledger(str(self.chain()))
        self.assertTrue(ok, message)
        records = [json.loads(line) for line in
                   self.chain().read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([r["seq"] for r in records], [1, 2])
        self.assertEqual(records[1]["prev_hash"], records[0]["report_hash"])
        self.assertEqual(records[0]["engine"], "architecture-v1")

    def test_official_chain_is_untouched(self):
        self.run_v1()
        self.assertFalse((self.base / "ledger" / "chain.log").exists(),
                         "v1 不得写官方链")

    def test_integrity_block_has_all_fingerprints(self):
        self.run_v1()
        report = json.loads(self.reports()[-1].read_text(encoding="utf-8"))
        block = report["integrity"]
        for key in ("target_hash", "ruleset_hash", "rules_dir_hash",
                    "rules_version", "knowledge_hash", "knowledge_version",
                    "report_hash", "prev_report_hash", "logs_hash"):
            self.assertIn(key, block)
        self.assertTrue(block["target_hash"])
        self.assertTrue(block["logs_hash"], "v1 现在会写日志，logs_hash 不该为空")

    def test_logs_are_written_and_hashed(self):
        self.run_v1()
        review_id = self.reports()[-1].stem
        log_dir = self.base / "logs" / "v1" / review_id
        self.assertTrue((log_dir / "decisions.jsonl").exists())
        self.assertTrue((log_dir / "tools.jsonl").exists())
        self.assertTrue((log_dir / "task.json").exists())
        report = json.loads(self.reports()[-1].read_text(encoding="utf-8"))
        self.assertEqual(integrity.sha256_dir(str(log_dir)),
                         report["integrity"]["logs_hash"])
        # 改日志 → 目录哈希变化（证明「过程也能被校验」）
        (log_dir / "task.json").write_text("{}", encoding="utf-8")
        self.assertNotEqual(integrity.sha256_dir(str(log_dir)),
                            report["integrity"]["logs_hash"])


class _null:
    def write(self, *_args):
        return 0

    def flush(self):
        pass


if __name__ == "__main__":
    unittest.main()
