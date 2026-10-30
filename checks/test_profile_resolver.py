#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_profile_resolver.py —— Profile 解析的公开不变量。

Profile 回答的是「这次按哪套标准审」：维度、规则集、情境、格式规格。
旧实现把答案拆在四处（manifest / scenarios / format_specs / charter），
合成动作埋在 run_one 里；这里守的是「合成规则只有一份实现」。

运行：
    python -m unittest discover -s checks -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import ReviewProfile          # noqa: E402
from review_agent.profiles import LEGACY_AGENT_ID, resolve_profile  # noqa: E402

MANIFEST = {
    "id": "doc-review",
    "name": "文档审查 Agent",
    "version": "1.4.0",
    "description": "面向文本 / 合同 / LLM 输出的独立审查",
    "dimensions": [
        {"id": "fact"}, {"id": "safety"}, {"id": "light"},
    ],
    "dims_by_type": {
        "code": ["safety", "light"],
        "text": ["fact", "safety", "light"],
    },
    "default_dims": ["fact", "safety"],
}

RULES = [
    {"rule_id": "FACT-001", "dimension": "fact"},
    {"rule_id": "SAFE-001", "dimension": "safety"},
]

SCENARIO = {"id": "general", "name": "通用文档审查", "version": "v1-2026-10-27"}
FORMAT = {"id": "strict-a", "name": "客户 A 版式", "version": "1.0"}


class ProfileResolutionTests(unittest.TestCase):
    def test_manifest_drives_identity_and_dimensions(self):
        profile = resolve_profile(MANIFEST, target_type="code")
        self.assertIsInstance(profile, ReviewProfile)
        self.assertEqual(profile.id, "doc-review")
        self.assertEqual(profile.name, "文档审查 Agent")
        self.assertEqual(profile.version, "1.4.0")
        self.assertEqual(profile.dimensions, ("safety", "light"))
        self.assertEqual(set(profile.target_types), {"code", "text"})

    def test_unknown_type_falls_back_to_default_dims(self):
        profile = resolve_profile(MANIFEST, target_type="audio")
        self.assertEqual(profile.dimensions, ("fact", "safety"))

    def test_manifest_without_routing_falls_back_to_declared_dimensions(self):
        profile = resolve_profile(
            {"id": "security-audit", "name": "安全审查", "version": "1.0.1",
             "dimensions": [{"id": "secret"}, {"id": "injection"}]},
            target_type="code")
        self.assertEqual(profile.dimensions, ("secret", "injection"))

    def test_missing_agent_is_marked_legacy_not_disguised_as_default(self):
        profile = resolve_profile(
            None, target_type="text",
            fallback_dimensions=["fact", "compliance"], source="legacy:report")
        self.assertEqual(profile.id, LEGACY_AGENT_ID)
        self.assertIn("unversioned", profile.version)
        self.assertEqual(profile.dimensions, ("fact", "compliance"))
        self.assertEqual(profile.source, "legacy:report")

    def test_scenario_and_format_are_recorded_in_version_and_rationale(self):
        profile = resolve_profile(
            MANIFEST, target_type="text", scenario=SCENARIO, format_spec=FORMAT)
        self.assertIn("scenario:general@v1-2026-10-27", profile.version)
        self.assertIn("format:strict-a@1.0", profile.version)
        self.assertIn("情境 通用文档审查", profile.rationale)
        self.assertIn("格式规格 客户 A 版式", profile.rationale)

    def test_rule_ids_come_from_active_rules(self):
        profile = resolve_profile(MANIFEST, target_type="text", active_rules=RULES)
        self.assertEqual(profile.rule_ids, ("FACT-001", "SAFE-001"))

    def test_malformed_rule_entries_are_ignored(self):
        profile = resolve_profile(
            MANIFEST, target_type="text",
            active_rules=[{"rule_id": "FACT-001"}, {}, {"dimension": "fact"}] + [1])
        self.assertEqual(profile.rule_ids, ("FACT-001",))

    def test_fingerprint_is_stable_and_rule_sensitive(self):
        a = resolve_profile(MANIFEST, target_type="text", active_rules=RULES)
        b = resolve_profile(MANIFEST, target_type="text", active_rules=RULES)
        c = resolve_profile(MANIFEST, target_type="text", active_rules=RULES[:1])
        self.assertEqual(a.fingerprint(), b.fingerprint())
        self.assertNotEqual(a.fingerprint(), c.fingerprint())


if __name__ == "__main__":
    unittest.main()
