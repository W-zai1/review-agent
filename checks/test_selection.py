#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_selection.py —— 配置选择校验的公开不变量。

server 的 `_resolve_*` 与 CLI parser 必须共用同一份实现：空值归一、
未知 id 的候选列表、配置读取失败的前缀，都不能各写一套。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.profiles.selection import (          # noqa: E402
    resolve_agent,
    resolve_format_spec,
    resolve_scenario,
)


class SelectionError(Exception):
    """测试用的配置读取异常，对应 ScenarioError / FormatSpecError。"""


AGENTS = [{"id": "zeta"}, {"id": "alpha"}]
SCENARIOS = {"scenarios": [{"id": "general"}, {"id": "code"}]}
SPECS = {"specs": [{"id": "none"}, {"id": "course-report-v1"}]}


def _load(data):
    return lambda: data


class AgentSelectionTests(unittest.TestCase):
    def test_empty_and_auto_mean_auto_route(self):
        for raw in (None, "", "auto"):
            self.assertEqual(resolve_agent(raw, lambda: AGENTS), (None, None))

    def test_known_id_is_returned(self):
        self.assertEqual(resolve_agent("alpha", lambda: AGENTS), ("alpha", None))

    def test_unknown_id_lists_sorted_candidates(self):
        value, error = resolve_agent("beta", lambda: AGENTS)
        self.assertIsNone(value)
        self.assertIn("未知 Agent：beta", error)
        self.assertIn("alpha、zeta", error)


class ScenarioSelectionTests(unittest.TestCase):
    def test_empty_and_default_are_unspecified(self):
        for raw in (None, "", "default"):
            self.assertEqual(
                resolve_scenario(raw, _load(SCENARIOS), SelectionError),
                (None, None))

    def test_known_id_is_returned(self):
        self.assertEqual(
            resolve_scenario("code", _load(SCENARIOS), SelectionError),
            ("code", None))

    def test_unknown_id_lists_candidates(self):
        value, error = resolve_scenario("other", _load(SCENARIOS), SelectionError)
        self.assertIsNone(value)
        self.assertIn("未知审查情境：other", error)
        self.assertIn("general、code", error)

    def test_config_error_uses_caller_prefix(self):
        def broken():
            raise SelectionError("yaml 坏了")

        value, error = resolve_scenario(
            "code", broken, SelectionError, config_prefix="审查情境错误")
        self.assertIsNone(value)
        self.assertEqual(error, "审查情境错误：yaml 坏了")


class FormatSelectionTests(unittest.TestCase):
    def test_empty_and_default_are_unspecified(self):
        for raw in (None, "", "default"):
            self.assertEqual(
                resolve_format_spec(raw, _load(SPECS), SelectionError),
                (None, None))

    def test_known_id_is_returned(self):
        self.assertEqual(
            resolve_format_spec("none", _load(SPECS), SelectionError),
            ("none", None))

    def test_unknown_id_lists_candidates(self):
        value, error = resolve_format_spec("strict", _load(SPECS), SelectionError)
        self.assertIsNone(value)
        self.assertIn("未知格式规格：strict", error)
        self.assertIn("none、course-report-v1", error)

    def test_dict_is_passed_through_only_when_allowed(self):
        custom = {"check": {"body_font_east": "宋体"}}
        self.assertEqual(
            resolve_format_spec(
                custom, _load(SPECS), SelectionError, allow_dict=True),
            (custom, None))
        value, error = resolve_format_spec(custom, _load(SPECS), SelectionError)
        self.assertIsNone(value)
        self.assertIn("未知格式规格", error)

    def test_config_error_uses_caller_prefix(self):
        def broken():
            raise SelectionError("specs 坏了")

        value, error = resolve_format_spec(
            "none", broken, SelectionError, config_prefix="格式规格错误")
        self.assertIsNone(value)
        self.assertEqual(error, "格式规格错误：specs 坏了")


if __name__ == "__main__":
    unittest.main()
