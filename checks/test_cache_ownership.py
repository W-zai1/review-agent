#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_cache_ownership.py —— 缓存与审查号的归属关系。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import llm_client  # noqa: E402
import llm_review  # noqa: E402
import research  # noqa: E402


class _Client:
    def chat(self, messages):
        return SimpleNamespace(
            content="[]", model="m", finish_reason="stop",
            usage={"input": 1, "output": 1}, latency_ms=1)


def _claim():
    return {"entity": "小米", "metric": "出货量", "kind": "number",
            "value": 5.0, "unit": "亿", "year": 2024,
            "raw": "5 亿", "quote": "小米 2024 年出货量 5 亿台",
            "sentence_index": 1}


class LlmCacheOwnershipTests(unittest.TestCase):
    def test_owner_is_written_and_touched_on_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            cfg = llm_client.LLMConfig(
                base_url="https://api.example.com/v1", model="m",
                api_key="x", max_input_chars=1000)
            rules = [{"rule_id": "FACT-001", "dimension": "fact",
                      "severity": "major", "desc": "事实"}]
            with mock.patch.object(llm_review, "CACHE_DIR", cache):
                first = llm_review.review_dimension(
                    _Client(), "fact", rules, "小米 2024 年出货量 5 亿台",
                    cfg, owner="R-ONE")
                second = llm_review.review_dimension(
                    _Client(), "fact", rules, "小米 2024 年出货量 5 亿台",
                    cfg, owner="R-TWO")
            payload = json.loads(
                (cache / f"{first['cache_key']}.json").read_text(encoding="utf-8"))
            self.assertTrue(second["cached"])
            self.assertEqual(payload["_owners"], ["R-ONE", "R-TWO"])


class ResearchCacheOwnershipTests(unittest.TestCase):
    def test_owners_merge_across_writes_and_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            claim = _claim()
            verdict = {"status": "unverified", "confidence": 0.0,
                       "reason": "x", "sources": []}
            with mock.patch.object(research, "CACHE_DIR", cache):
                research.cache_put(claim, verdict, 1, owner="R-ONE")
                research.cache_put(claim, verdict, 1, owner="R-TWO")
                research.cache_get(claim, owner="R-THREE")
                path = research._cache_path(claim)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["_owners"], ["R-ONE", "R-TWO", "R-THREE"])


if __name__ == "__main__":
    unittest.main()
