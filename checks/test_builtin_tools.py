#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_builtin_tools.py —— 批次 1 确定性工具的 `BaseTool` 实现（阶段 3）。

阶段 3 的验收标准是「注册表里有它、`review.py` 通过注册表调用、`checks/` 有测试」。
这里钉住的是工具**作为独立能力**的行为，而不是它在规则流水线里的副作用：

    · 入参出参都是数据：不传 Session、不传运行上下文也能跑
    · 算法只有一份：工具调用的是 `tools/` 里的同一函数（注入），不复制实现
    · `rule_ids` 的三态语义：None = 跑全部，() = 一条都不跑，列表 = 只跑这些
    · 命中的 quote 来自待审文档 → `untrusted=True`（调用方只当数据看）
    · 空输入是失败，不是「没问题」
    · 同输入同输出（确定性工具的基本承诺）

用真算法、真文本，不 mock：这两个工具的价值就在于「结论不依赖模型」，
mock 掉算术层等于把要验证的东西换成自己写的期望值。
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402
from review_agent.capabilities import Permission, ToolContext  # noqa: E402
from review_agent.capabilities.tools import ToolPermissionError  # noqa: E402

# 分项 360+210+480=1050，文中写 1080 —— arith_check:sum 会命中
SUM_TEXT = "销售费用 360 万，管理费用 210 万，研发费用 480 万，三项加总 1080 万元。"
# 30 岁、从业 25 年 —— LOGIC-004 年龄工龄矛盾
AGE_TEXT = "候选人现年 30 岁，从业 25 年，管理经验丰富。"
CLEAN_TEXT = "本次会议于上午九点开始，共三项议题。"


def context(*, permissions=None, actor="test") -> ToolContext:
    return ToolContext(
        review_id="R-20261030-001", run_id="R-20261030-001-RUN01",
        permissions=frozenset(permissions) if permissions is not None
        else frozenset({Permission.READ, Permission.ANALYZE}),
        actor=actor,
    )


def run_tool(registry, name, arguments, ctx=None):
    return asyncio.run(registry.execute(name, arguments, ctx or context()))


class CapabilityRegistryTests(unittest.TestCase):
    def setUp(self):
        self.registry = review.build_capability_registry()

    def test_batch1_tools_are_registered(self):
        self.assertEqual(self.registry.names(),
                         ("integrity.hash", "law.cite", "logic.check",
                          "math.check", "matrix.match", "rules.classify",
                          "rules.pattern", "text.regex"))

    def test_describe_exposes_permission_and_arguments(self):
        described = {item["name"]: item for item in self.registry.describe()}
        for name in ("math.check", "logic.check", "rules.classify"):
            self.assertEqual(described[name]["permission"], "analyze"
                             if name != "rules.classify" else "read")
        for item in described.values():
            self.assertTrue(item["description"], "每个工具都必须有可读的说明")
            self.assertTrue(item["arguments"], "每个工具都必须声明入参")
        self.assertIn("text", described["math.check"]["arguments"])
        self.assertIn("rules", described["rules.classify"]["arguments"])

    def test_read_only_context_cannot_analyze(self):
        with self.assertRaises(ToolPermissionError):
            run_tool(self.registry, "math.check", {"text": SUM_TEXT},
                     context(permissions=[Permission.READ]))

    def test_unknown_argument_is_rejected_before_execution(self):
        from review_agent.capabilities.tools import ToolArgumentError
        with self.assertRaises(ToolArgumentError):
            run_tool(self.registry, "math.check", {"txt": SUM_TEXT})

    def test_invocation_is_audited(self):
        _result, invocation = run_tool(self.registry, "math.check",
                                       {"text": SUM_TEXT})
        self.assertEqual(invocation.tool, "math.check")
        self.assertEqual(invocation.actor, "test")
        self.assertTrue(invocation.ok)
        self.assertEqual(invocation.error_count, 0)


class MathCheckToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = review.build_capability_registry()

    def test_detects_sum_mismatch(self):
        result, _ = run_tool(self.registry, "math.check", {"text": SUM_TEXT})
        self.assertTrue(result.ok)
        rule_ids = {f["rule_id"] for f in result.data["findings"]}
        self.assertIn("LOGIC-001", rule_ids)
        self.assertEqual(result.data["count"], len(result.data["findings"]))
        evidence = " ".join(f["evidence"] for f in result.data["findings"])
        self.assertIn("1050", evidence)          # 结论可复算，不是模型说的

    def test_clean_text_has_no_findings(self):
        result, _ = run_tool(self.registry, "math.check", {"text": CLEAN_TEXT})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 0)

    def test_rule_filter_selects(self):
        result, _ = run_tool(self.registry, "math.check",
                             {"text": SUM_TEXT, "rule_ids": ["LOGIC-001"]})
        self.assertEqual(result.data["count"], 1)
        result, _ = run_tool(self.registry, "math.check",
                             {"text": SUM_TEXT, "rule_ids": ["LOGIC-999"]})
        self.assertEqual(result.data["count"], 0)

    def test_empty_rule_list_runs_nothing(self):
        """[] 与 None 不能混：把「本次只启用 0 条规则」当成「跑全量」会凭空出结论。"""
        result, _ = run_tool(self.registry, "math.check",
                             {"text": SUM_TEXT, "rule_ids": []})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 0)

    def test_omitted_rule_ids_runs_everything(self):
        result, _ = run_tool(self.registry, "math.check", {"text": SUM_TEXT})
        self.assertGreaterEqual(result.data["count"], 1)

    def test_empty_text_is_a_failure_not_a_pass(self):
        result, _ = run_tool(self.registry, "math.check", {"text": "   "})
        self.assertFalse(result.ok)
        self.assertIn("空", result.errors[0])


class LogicCheckToolTests(unittest.TestCase):
    def setUp(self):
        self.registry = review.build_capability_registry()

    def test_detects_age_tenure_conflict(self):
        result, _ = run_tool(self.registry, "logic.check", {"text": AGE_TEXT})
        self.assertTrue(result.ok)
        self.assertEqual([f["rule_id"] for f in result.data["findings"]],
                         ["LOGIC-004"])

    def test_clean_text_has_no_findings(self):
        result, _ = run_tool(self.registry, "logic.check", {"text": CLEAN_TEXT})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 0)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.registry = review.build_capability_registry()

    def test_findings_are_marked_untrusted(self):
        result, _ = run_tool(self.registry, "logic.check", {"text": AGE_TEXT})
        self.assertTrue(result.untrusted,
                        "命中的 quote 逐字来自待审文档，必须标成不可信输入")

    def test_same_input_same_output(self):
        first, _ = run_tool(self.registry, "math.check", {"text": SUM_TEXT})
        second, _ = run_tool(self.registry, "math.check", {"text": SUM_TEXT})
        self.assertEqual(first.data["findings"], second.data["findings"])

    def test_findings_keep_the_legacy_shape(self):
        result, _ = run_tool(self.registry, "math.check", {"text": SUM_TEXT})
        finding = result.data["findings"][0]
        for key in ("rule_id", "dimension", "quote", "evidence", "suggestion",
                    "confidence", "tool"):
            self.assertIn(key, finding, f"工具输出丢了字段 {key}")

    def test_does_not_need_a_session(self):
        """工具能脱离规则流水线独立调用——这正是「工具从 Agent 中独立」的验收点。"""
        registry = review.build_capability_registry()
        result, _ = run_tool(registry, "math.check", {"text": SUM_TEXT})
        self.assertTrue(result.ok)


class RuleClassifyToolTests(unittest.TestCase):
    """规则执行方式分类：决定「这条结论能不能复现」。"""

    def setUp(self):
        self.registry = review.build_capability_registry()

    def _rules(self):
        return [dict(r) for r in review.load_rules()]

    def test_real_rules_are_all_counted(self):
        rules = self._rules()
        result, _ = run_tool(self.registry, "rules.classify", {"rules": rules})
        self.assertTrue(result.ok)
        counts = result.data["counts"]
        self.assertEqual(sum(counts.values()), len(rules),
                         "每条规则必须落进且只落进一个执行方式")

    def test_builtin_rule_is_deterministic(self):
        rules = [{"rule_id": "LOGIC-001", "dimension": "logic", "desc": "加总"}]
        result, _ = run_tool(self.registry, "rules.classify", {"rules": rules})
        self.assertEqual(result.data["counts"]["deterministic"], 1)

    def test_pattern_only_rule_is_declarative(self):
        rules = [{"rule_id": "CUSTOM-900", "dimension": "custom",
                  "pattern": r"必须.{0,6}审批"}]
        result, _ = run_tool(self.registry, "rules.classify", {"rules": rules})
        self.assertEqual(result.data["counts"]["declarative"], 1)

    def test_unregistered_rule_is_prompt_only_and_listed(self):
        rules = [{"rule_id": "NOPE-001", "dimension": "custom"}]
        result, _ = run_tool(self.registry, "rules.classify", {"rules": rules})
        self.assertEqual(result.data["counts"]["prompt_only"], 1)
        self.assertEqual(result.data["prompt_only"], ("NOPE-001",),
                         "没有执行器的规则要显式列出来，不能只给个计数")

    def test_read_permission_is_enough(self):
        """分类只查表，不读材料——READ 就该能用。"""
        result, _ = run_tool(self.registry, "rules.classify",
                             {"rules": [{"rule_id": "LOGIC-001"}]},
                             context(permissions=[Permission.READ]))
        self.assertTrue(result.ok)

    def test_empty_rules_is_a_failure(self):
        result, _ = run_tool(self.registry, "rules.classify", {"rules": []})
        self.assertFalse(result.ok)
        self.assertIn("为空", result.errors[0])

    def test_rule_without_id_is_rejected(self):
        result, _ = run_tool(self.registry, "rules.classify",
                             {"rules": [{"dimension": "logic"}]})
        self.assertFalse(result.ok)
        self.assertIn("rule_id", result.errors[0])


class IntegrityHashToolTests(unittest.TestCase):
    """内容指纹：用来判断「两处引用是不是同一段原文」。"""

    def setUp(self):
        self.registry = review.build_capability_registry()

    def test_known_digest(self):
        import hashlib
        result, _ = run_tool(self.registry, "integrity.hash", {"text": "abc"})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["sha256"],
                         hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(result.data["size"], 3)

    def test_empty_text_has_a_digest(self):
        import hashlib
        result, _ = run_tool(self.registry, "integrity.hash", {"text": ""})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["sha256"],
                         hashlib.sha256(b"").hexdigest())
        self.assertEqual(result.data["size"], 0)

    def test_utf8_bytes_are_hashed(self):
        import hashlib
        result, _ = run_tool(self.registry, "integrity.hash", {"text": "中文"})
        self.assertEqual(result.data["sha256"],
                         hashlib.sha256("中文".encode("utf-8")).hexdigest())
        self.assertEqual(result.data["size"], 6)

    def test_read_permission_is_enough(self):
        result, _ = run_tool(self.registry, "integrity.hash",
                             {"text": "abc"},
                             context(permissions=[Permission.READ]))
        self.assertTrue(result.ok)


PATTERN_RULES = [
    {"rule_id": "TEST-001", "dimension": "custom", "desc": "禁止 os.system",
     "pattern": r"os\.system\s*\(", "confidence": 0.9,
     "suggestion": "改用 subprocess"},
    {"rule_id": "TEST-002", "dimension": "custom", "desc": "shell=True 需 subprocess",
     "pattern": r"shell\s*=\s*True", "require": r"subprocess"},
    {"rule_id": "TEST-003", "dimension": "custom", "desc": "禁止 TODO",
     "pattern": r"TODO"},
]

PATTERN_TEXT = (
    "import os\n"
    "os.system('ls')\n"
    "# TODO 这只是注释\n"
    "x = run(cmd, shell=True)\n"
    "y = subprocess.run(cmd, shell=True)\n"
)


class _FakeSession:
    """只实现 `scan()` 用到的那几个方法：用来做「工具 vs 旧函数」的差分。"""

    def __init__(self):
        self.issues = []
        self.tool_logs = []

    def make_issue(self, **kwargs):
        return kwargs

    def add_issue(self, issue):
        key = (issue.get("rule_id"), issue.get("quote"))
        if any((i.get("rule_id"), i.get("quote")) == key for i in self.issues):
            return False
        self.issues.append(issue)
        return True

    def tool(self, *args, **kwargs):
        self.tool_logs.append((args, kwargs))


class PatternScanToolTests(unittest.TestCase):
    """声明式规则：工具与旧 `scan()` 必须给出**同一批**命中。"""

    def setUp(self):
        self.registry = review.build_capability_registry()

    def _run(self, **over):
        args = {"text": PATTERN_TEXT, "rules": PATTERN_RULES}
        args.update(over)
        return run_tool(self.registry, "rules.pattern", args)

    def test_require_and_comment_semantics(self):
        result, _ = self._run()
        self.assertTrue(result.ok)
        rule_ids = [f["rule_id"] for f in result.data["findings"]]
        self.assertEqual(rule_ids, ["TEST-001", "TEST-002"],
                         "注释行不该命中；require 不满足的第一行 shell=True 也不该命中")

    def test_matches_the_legacy_scan_exactly(self):
        """差分：同一份文本、同一批规则，工具与 `pattern_rules.scan()` 命中一致。"""
        import pattern_rules

        result, _ = self._run()
        own = [(f["rule_id"], f["quote"], f["location"], f["evidence"])
               for f in result.data["findings"]]

        session = _FakeSession()
        pattern_rules.scan(session, PATTERN_TEXT, PATTERN_RULES)
        legacy = [(i["rule_id"], i["quote"], i["location"], i["evidence"])
                  for i in session.issues]
        self.assertEqual(own, legacy)

    def test_rules_examined_counts_only_pattern_rules(self):
        result, _ = self._run()
        self.assertEqual(result.data["rules_examined"], 3)

    def test_dimension_filter(self):
        result, _ = self._run(dimension="other")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 0)
        self.assertEqual(result.data["rules_examined"], 0)

    def test_findings_are_untrusted(self):
        result, _ = self._run()
        self.assertTrue(result.untrusted)

    def test_rules_without_pattern_are_rejected(self):
        result, _ = self._run(rules=[{"rule_id": "X-1", "dimension": "custom"}])
        self.assertFalse(result.ok)
        self.assertIn("pattern", result.errors[0])

    def test_rule_missing_dimension_is_rejected(self):
        result, _ = self._run(rules=[{"rule_id": "X-2", "pattern": r"foo"}])
        self.assertFalse(result.ok)
        self.assertIn("dimension", result.errors[0])

    def test_real_ruleset_is_handled_honestly(self):
        """真实规则集当前**没有**声明式规则：工具必须明说，而不是假装扫过。

        这条用例盯的是「能力边界要暴露」——如果哪天规则集加了 pattern 规则，
        它会变成正向用例；在此之前，它证明工具不会把「没规则可跑」当成 success。
        """
        real = [dict(r) for r in review.load_rules()]
        self.assertTrue(real, "规则集不该为空")
        declarative = [r for r in real if r.get("pattern")]
        result, _ = run_tool(self.registry, "rules.pattern",
                             {"text": "任意一段待审文本", "rules": real})
        if declarative:
            self.assertTrue(result.ok)
            self.assertEqual(result.data["rules_examined"], len(declarative))
        else:
            self.assertFalse(result.ok)
            self.assertIn("pattern", result.errors[0])


ZERO_WIDTH = "\u200b"
INVISIBLE_TEXT = f"市{ZERO_WIDTH}场{ZERO_WIDTH}占有率 90%"


class TextRegexToolTests(unittest.TestCase):
    """隐形字符剥离 + 原文坐标映射。"""

    def setUp(self):
        self.registry = review.build_capability_registry()

    def _run(self, **over):
        args = {"text": INVISIBLE_TEXT, "pattern": "市场占有率"}
        args.update(over)
        return run_tool(self.registry, "text.regex", args)

    def test_zero_width_chars_do_not_hide_a_match(self):
        result, _ = self._run()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 1,
                         "零宽字符不该让「市场占有率」整段失配")
        self.assertTrue(result.data["matched_on_cleaned_text"])

    def test_offsets_point_at_the_original_text(self):
        result, _ = self._run()
        match = result.data["matches"][0]
        original = INVISIBLE_TEXT[match["start"]:match["end"]]
        self.assertEqual(original, match["quote"], "证据必须能按偏移回查原文")
        self.assertEqual(original.replace(ZERO_WIDTH, ""), "市场占有率")
        self.assertGreater(match["end"], 5, "原文坐标要含被剥掉的零宽字符")

    def test_no_match_is_an_empty_success(self):
        result, _ = self._run(pattern="不存在的词")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["count"], 0)
        self.assertEqual(result.warnings, ())

    def test_illegal_pattern_is_a_failure(self):
        result, _ = self._run(pattern="(")
        self.assertFalse(result.ok)
        self.assertIn("正则", result.errors[0])

    def test_ignore_case(self):
        result, _ = self._run(text="ABC abc", pattern="abc", ignore_case=True)
        self.assertEqual(result.data["count"], 2)
        result, _ = self._run(text="ABC abc", pattern="abc")
        self.assertEqual(result.data["count"], 1)

    def test_max_matches_truncates_with_warning(self):
        result, _ = self._run(text="a a a a", pattern="a", max_matches=2)
        self.assertEqual(result.data["count"], 2)
        self.assertEqual(len(result.warnings), 1)

    def test_empty_text_is_a_failure(self):
        result, _ = self._run(text="   ")
        self.assertFalse(result.ok)

    def test_results_are_untrusted(self):
        result, _ = self._run()
        self.assertTrue(result.untrusted)


PURPOSE_TEXT = (
    "【目的文件：岗位要求】\n"
    "1. 本科及以上学历\n"
    "2. 三年以上相关经验\n"
)
CANDIDATE_TEXT = "候选人：硕士学历，五年 Python 开发经验。"


class MatrixMatchToolTests(unittest.TestCase):
    """要求侧 vs 候选侧的逐项对照（配置由注入的标准决定，不读磁盘）。"""

    def setUp(self):
        self.registry = review.build_capability_registry()

    def _run(self, **over):
        args = {"purpose_text": PURPOSE_TEXT, "target_text": CANDIDATE_TEXT}
        args.update(over)
        return run_tool(self.registry, "matrix.match", args)

    def test_matrix_is_built_from_real_config(self):
        result, _ = self._run()
        self.assertTrue(result.ok)
        self.assertTrue(result.data["available"])
        coverage = result.data["coverage"]
        self.assertGreater(coverage.get("total", 0), 0,
                           "学历/经验两维至少应生成一项对照")
        self.assertEqual(result.warnings, ())

    def test_injected_config_wins(self):
        """给了 config 就不该去读文件——版本号能证明用的是哪一份。"""
        result, _ = self._run(config={"version": "v-injected", "max_items": 10,
                                      "dims": []})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["matrix"]["config_version"], "v-injected")

    def test_missing_purpose_is_reported_not_faked(self):
        result, _ = self._run(purpose_text="这是一段没有要求块标记的普通文本")
        self.assertTrue(result.ok, "没有要求文件不是错误")
        self.assertFalse(result.data["available"])
        self.assertEqual(len(result.warnings), 1,
                         "「没对照过」必须说出来，否则会被当成「全满足」")

    def test_empty_inputs_are_failures(self):
        for field in ("purpose_text", "target_text"):
            with self.subTest(field=field):
                result, _ = self._run(**{field: "   "})
                self.assertFalse(result.ok)
                self.assertIn("为空", result.errors[0])

    def test_results_are_untrusted(self):
        result, _ = self._run()
        self.assertTrue(result.untrusted)


ART19 = {
    "law_short": "劳动合同法", "law_name": "中华人民共和国劳动合同法",
    "no_cn": "第十九条", "no": 19, "text": "试用期最长不得超过六个月。",
    "chapter": "第二章", "law_status": "现行有效",
}
PROBATION_TEXT = "本合同的试用期为 9 个月。"


class LawCiteToolTests(unittest.TestCase):
    """法条比对：条文可注入，缺条文时明说「没查」。"""

    def setUp(self):
        self.registry = review.build_capability_registry()

    def _run(self, **over):
        args = {"text": PROBATION_TEXT}
        args.update(over)
        return run_tool(self.registry, "law.cite", args)

    def test_injected_article_fires_the_check(self):
        result, _ = self._run(articles={"labor-contract:19": ART19})
        self.assertTrue(result.ok)
        self.assertEqual(len(result.data["issues"]), 1)
        self.assertEqual(result.data["issues"][0]["rule_id"], "LAW-001")
        self.assertEqual(result.data["issues"][0]["citation"]["article"], "第十九条")
        # 缺 art20 / art24 时另外两项本就该报「未执行」
        self.assertEqual(len(result.data["skipped"]), 2)

    def test_missing_articles_are_reported_not_silently_passed(self):
        result, _ = self._run(articles={})
        self.assertTrue(result.ok, "缺条文不是崩溃，也不是「没问题」")
        self.assertEqual(tuple(result.data["issues"]), ())
        self.assertEqual(len(result.data["skipped"]), 3)
        self.assertEqual(len(result.warnings), 3,
                         "「该项未执行」必须提成 warning，不能被当成已核验")

    def test_loader_is_used_when_articles_are_not_injected(self):
        result, _ = self._run()
        self.assertTrue(result.ok, "真实法条库应可用（knowledge/laws 随仓库走）")
        self.assertIn("issues", result.data)

    def test_empty_text_is_a_failure(self):
        result, _ = self._run(text="   ")
        self.assertFalse(result.ok)
        self.assertIn("为空", result.errors[0])

    def test_results_are_untrusted(self):
        result, _ = self._run(articles={"labor-contract:19": ART19})
        self.assertTrue(result.untrusted)


if __name__ == "__main__":
    unittest.main()
