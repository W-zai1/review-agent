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

from collections.abc import Callable, Sequence
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


def batch1_tools(*, math_run_all: RunAll, logic_run_all: RunAll) -> tuple[BaseTool, ...]:
    """批次 1 已实现的工具集合（算法本体由调用方注入）。

    为什么集中成一个函数：注册表怎么装配是入口的事，工具清单是能力层的事。
    将来补上 `text.regex` / `rules.pattern` 等，只在这个元组里加一项。
    """
    return (MathCheckTool(math_run_all), LogicCheckTool(logic_run_all))
