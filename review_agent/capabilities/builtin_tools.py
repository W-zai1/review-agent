# -*- coding: utf-8 -*-
"""批次 1 的确定性工具：`BaseTool` 的真实实现（阶段 3）。

为什么不能直接把 `tools/*.py` 搬进来：
    那 42 个模块是「被 `review.py` import 的函数集合」，签名是
    `fn(session, text, rules, ctx)`——它们**知道自己在审查流程里**，会把副作用
    写进 Session。工具的定义正相反：给定入参就返回结果，不知道谁在调它
    （见 docs/TOOL_SPECIFICATION.md §1）。

    更要命的是依赖方向：`capabilities` 层不许 import `tools/`（ARCHITECTURE §3
    第 3 条，`checks/test_architecture_guard.py` 会扫出来）。所以这里的做法是
    **注入算法本体**：`MathCheckTool(arith_check.run_all)`。

    这样拆的好处是两条都不动：
        · 算法只有一份（仍是 `tools/arith_check.py`，行为不变）
        · 工具层不认识 `tools/`，能单独构造、单独测试、将来换实现只换注入的那个函数

本文件只收「纯函数、无 IO、无 Session」的工具。有 IO 的（lint 要读文件、
docx 要解压、检索要读索引）留到批次 2，理由与迁移批次表一致。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .tools import BaseTool, Permission, ToolContext, ToolResult

# `run_all(text, active_rule_ids) -> list[dict]`，与 tools/arith_check.py、
# tools/logic_checks.py 的现有签名一致。不在这里定义 Protocol：
# 一个 Callable 别名就够，多一层抽象没人维护。
RunAll = Callable[[str, "Sequence[str] | None"], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class TextCheckArgs:
    """所有「对一段文本跑确定性检查」的工具共用入参。"""

    text: str
    rule_ids: tuple[str, ...] | None = None


class _TextCheckTool(BaseTool):
    """把 `run_all` 形状的检查包成工具。子类只声明名字与描述。"""

    permission = Permission.ANALYZE
    arguments_type = TextCheckArgs

    def __init__(self, *, name: str, description: str, run_all: RunAll) -> None:
        self.name = name
        self.description = description
        self._run_all = run_all

    async def execute(self, arguments: TextCheckArgs,
                      context: ToolContext) -> ToolResult:
        text = arguments.text or ""
        if not text.strip():
            # 空输入不是「没问题」：把空当干净会让「审了个空文件」显示成 pass
            return ToolResult.failure("text 为空：没有可检查的内容（空输入不等于无问题）")
        # None = 不按规则过滤（跑全部）；空元组 = 一条都不跑。
        # 两者不能混：把 [] 归一成 None 会让「本次只启用 0 条规则」变成「跑全量」。
        rule_ids = (None if arguments.rule_ids is None
                    else tuple(arguments.rule_ids))
        findings = tuple(self._run_all(text, rule_ids))
        return ToolResult.success(
            data={"findings": findings, "count": len(findings)},
            # 命中的 quote 逐字来自待审文档：内容不可信，调用方只能当数据看
            untrusted=True,
        )


class MathCheckTool(_TextCheckTool):
    """数值一致性：合计、百分比、星期、时长、份额合计、单位换算。"""

    def __init__(self, run_all: RunAll) -> None:
        super().__init__(
            name="math.check",
            description=("对文本里的数字做确定性核算：合计是否等于分项之和、"
                         "百分比/份额是否自洽、星期与日期是否对得上、"
                         "时长与年限是否矛盾。纯规则，同输入同输出。"),
            run_all=run_all,
        )


class LogicCheckTool(_TextCheckTool):
    """逻辑矛盾：年龄与工龄、抽样与结论、绝对化断言、金额大小写。"""

    def __init__(self, run_all: RunAll) -> None:
        super().__init__(
            name="logic.check",
            description=("检测同一段文本内部的逻辑矛盾：年龄与工龄冲突、"
                         "抽样量不足以支撑结论、绝对化表述与限定条件并存、"
                         "金额大小写不一致。纯规则，同输入同输出。"),
            run_all=run_all,
        )


@dataclass(frozen=True, slots=True)
class RuleClassifyArgs:
    """规则执行方式分类的入参：把规则定义（YAML 读出来的 dict）传进来。"""

    rules: tuple[dict[str, Any], ...]


class RuleClassifyTool(BaseTool):
    """回答「这条规则是代码算的、还是声明式跑的、还是只在提示词里出现」。

    这个问题的答案决定了结论的可信度：`prompt_only` 的规则跨次可能漂移，
    报告里必须能让人看出差别（见 tools/rule_exec.py 的模块说明）。
    """

    name = "rules.classify"
    description = ("按规则的执行方式分类：deterministic（有专用执行器）/ "
                   "declarative（自带 pattern，由通用引擎跑）/ prompt_only（只在提示词里）。"
                   "纯查表，不读文件、不跑审查。")
    permission = Permission.READ
    arguments_type = RuleClassifyArgs

    def __init__(self, *, summarize: Callable[[Sequence[Mapping[str, Any]]], dict],
                 counts: Callable[[Sequence[Mapping[str, Any]]], dict]) -> None:
        self._summarize = summarize
        self._counts = counts

    async def execute(self, arguments: RuleClassifyArgs,
                      context: ToolContext) -> ToolResult:
        rules = tuple(arguments.rules or ())
        if not rules:
            return ToolResult.failure("rules 为空：没有可分类的规则")
        bad = [i for i, rule in enumerate(rules)
               if not isinstance(rule, Mapping) or not rule.get("rule_id")]
        if bad:
            return ToolResult.failure(
                f"第 {bad[:5]} 条规则缺少 rule_id：无法按执行方式归类")
        summary = self._summarize(list(rules))
        return ToolResult.success(data={
            "summary": summary,
            "counts": self._counts(list(rules)),
            # 未登记执行器的规则最需要被看见：它们是最不可复现的那批
            "prompt_only": tuple(summary.get("prompt_only") or ()),
        })


@dataclass(frozen=True, slots=True)
class IntegrityHashArgs:
    text: str


class IntegrityHashTool(BaseTool):
    """对一段内容算 SHA-256：引用去重、证据指纹、跨报告比对「同一段原文」。"""

    name = "integrity.hash"
    description = ("计算一段文本的 SHA-256（UTF-8 字节）。用于证据指纹、"
                   "引用去重、跨报告比对同一段原文。不读文件、不写盘。")
    permission = Permission.READ
    arguments_type = IntegrityHashArgs

    def __init__(self, *, sha256_bytes: Callable[[bytes], str]) -> None:
        self._sha256_bytes = sha256_bytes

    async def execute(self, arguments: IntegrityHashArgs,
                      context: ToolContext) -> ToolResult:
        payload = (arguments.text or "").encode("utf-8")
        return ToolResult.success(data={
            "sha256": self._sha256_bytes(payload),
            "size": len(payload),
        })


@dataclass(frozen=True, slots=True)
class PatternScanArgs:
    """声明式规则扫描：判定条件全在规则里，工具只做通用匹配。"""

    text: str
    rules: tuple[dict[str, Any], ...]
    dimension: str | None = None
    skip_comments: bool = True
    target_suffix: str = ""


class PatternScanTool(BaseTool):
    """执行带 `pattern` 的声明式规则（`require` / `guard` / `unless` 组合判断）。

    与旧 `pattern_rules.scan()` 共用同一个纯核心 `scan_hits()`：工具不造假 Session，
    但匹配语义必须完全一致——否则「流水线说有命中、工具说没有」无法解释。
    """

    name = "rules.pattern"
    description = ("按规则自带的 pattern 扫描文本，支持 require（同行必须同时出现）、"
                   "guard / unless（出现即放弃）与注释行去噪。"
                   "判定条件来自规则定义，工具本身不含业务规则。")
    permission = Permission.ANALYZE
    arguments_type = PatternScanArgs

    def __init__(self, *, scan_hits: Callable[..., tuple[int, list]]) -> None:
        self._scan_hits = scan_hits

    async def execute(self, arguments: PatternScanArgs,
                      context: ToolContext) -> ToolResult:
        text = arguments.text or ""
        if not text.strip():
            return ToolResult.failure("text 为空：没有可扫描的内容（空输入不等于无问题）")
        rules = tuple(arguments.rules or ())
        if not rules:
            return ToolResult.failure("rules 为空：没有可执行的规则")
        with_pattern = [r for r in rules if isinstance(r, Mapping) and r.get("pattern")]
        if not with_pattern:
            return ToolResult.failure(
                "rules 里没有任何带 pattern 的规则：本工具只执行声明式规则")
        bad = [r for r in with_pattern
               if not r.get("rule_id") or not r.get("dimension")]
        if bad:
            return ToolResult.failure(
                "带 pattern 的规则必须同时声明 rule_id 与 dimension，"
                f"缺失 {len(bad)} 条（否则结论无法归属到规则/维度）")

        total, hits = self._scan_hits(
            text, rules, dimension=arguments.dimension,
            skip_comments=bool(arguments.skip_comments),
            target_suffix=arguments.target_suffix or "")
        findings = tuple({
            "rule_id": hit["rule"]["rule_id"],
            "dimension": hit["rule"]["dimension"],
            "quote": hit["quote"],
            "location": f"第 {hit['lineno']} 行",
            "evidence": f"{hit['rule'].get('desc', '')}｜命中「{hit['matched']}」",
            "suggestion": hit["suggestion"],
            "confidence": hit["confidence"],
            "tool": "pattern_rule",
        } for hit in hits)
        return ToolResult.success(
            data={"findings": findings, "count": len(findings),
                  "rules_examined": total},
            untrusted=True,
        )


def batch1_tools(*, math_run_all: RunAll, logic_run_all: RunAll,
                 rule_summarize, rule_counts, sha256_bytes,
                 pattern_scan_hits: Callable[..., tuple[int, list]]) -> tuple[BaseTool, ...]:
    """批次 1 已实现的工具集合（算法本体由调用方注入）。

    为什么集中成一个函数：注册表怎么装配是入口的事，工具清单是能力层的事。
    补新工具时只在这个元组里加一项，注释里写清卡点的不往里塞。
    """
    return (
        MathCheckTool(math_run_all),
        LogicCheckTool(logic_run_all),
        RuleClassifyTool(summarize=rule_summarize, counts=rule_counts),
        IntegrityHashTool(sha256_bytes=sha256_bytes),
        PatternScanTool(scan_hits=pattern_scan_hits),
    )
