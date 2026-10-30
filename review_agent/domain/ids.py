# -*- coding: utf-8 -*-
"""标识符：格式、解析、分配。

沿用现有产物的格式，不发明新 ID —— 历史报告、ledger、logs/ 目录名都已经在用：

    Review   R-20261030-001        （review.py allocate_review_id）
    Run      R-20261030-001-RUN01  （新增：一次 Review 可以有多次执行）
    Finding  ISS-0001              （review.py make_issue，运行内序号）
    Evidence EV-0001               （新增：旧报告只有 quote 字符串，没有证据对象）
    Dispute  DSP-0001              （新增：旧报告 disputes[] 无 id）
    Decision DEC-0001              （新增：旧报告只有 verdict + core_conclusion）

为什么 Review 与 Run 要分开：
    旧实现一次审查 = 一个 R-id，重跑就换 id，于是「同一任务重跑后结论变了」
    在数据上完全看不出来。Review 稳定、Run 递增，才能回答「这是第几次审、为什么重审」。
"""
from __future__ import annotations

import datetime
import re
from collections.abc import Iterable

from .errors import IdFormatError

_REVIEW_RE = re.compile(r"^R-(\d{8})-(\d{3})$")
_RUN_RE = re.compile(r"^(R-\d{8}-\d{3})-RUN(\d{2})$")
_FINDING_RE = re.compile(r"^ISS-(\d{4})$")
_EVIDENCE_RE = re.compile(r"^EV-(\d{4})$")
_DISPUTE_RE = re.compile(r"^DSP-(\d{4})$")
_DECISION_RE = re.compile(r"^DEC-(\d{4})$")


def _check_seq(seq: int, *, limit: int, what: str) -> int:
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise IdFormatError(f"{what} 序号必须是整数，实际 {seq!r}")
    if not 1 <= seq <= limit:
        raise IdFormatError(f"{what} 序号必须在 1..{limit}，实际 {seq}")
    return seq


# ---------------------------------------------------------------------------
# Review / Run
# ---------------------------------------------------------------------------
def format_review_id(day: datetime.date, seq: int) -> str:
    """R-YYYYMMDD-NNN。seq 为当日内序号，从 1 开始。"""
    if not isinstance(day, datetime.date):
        raise IdFormatError(f"日期必须是 datetime.date，实际 {type(day).__name__}")
    _check_seq(seq, limit=999, what="review")
    return f"R-{day:%Y%m%d}-{seq:03d}"


def parse_review_id(value: str) -> tuple[datetime.date, int]:
    m = _REVIEW_RE.match(str(value or ""))
    if not m:
        raise IdFormatError(f"不是合法的 review_id：{value!r}（期望 R-YYYYMMDD-NNN）")
    try:
        day = datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError as e:
        raise IdFormatError(f"review_id 里的日期不合法：{value!r}") from e
    seq = int(m.group(2))
    if seq < 1:
        raise IdFormatError(f"review_id 序号必须从 001 开始：{value!r}")
    return day, seq


def next_review_seq(existing: Iterable[str]) -> int:
    """给定已有 review_id 集合，返回下一个可用日序号。

    只认合法格式；脏数据（旧版本/手工改名）不参与计算，避免一条坏记录把
    整天的序号顶到 999。
    """
    best = 0
    for value in existing:
        m = _REVIEW_RE.match(str(value or ""))
        if m:
            best = max(best, int(m.group(2)))
    return best + 1


def format_run_id(review_id: str, seq: int = 1) -> str:
    parse_review_id(review_id)          # 先校验，错误信息指向根因
    _check_seq(seq, limit=99, what="run")
    return f"{review_id}-RUN{seq:02d}"


def parse_run_id(value: str) -> tuple[str, int]:
    m = _RUN_RE.match(str(value or ""))
    if not m:
        raise IdFormatError(f"不是合法的 run_id：{value!r}（期望 R-YYYYMMDD-NNN-RUNNN）")
    return m.group(1), _check_seq(int(m.group(2)), limit=99, what="run")


# ---------------------------------------------------------------------------
# 运行内实体：格式固定，序号由调用方（Run 内递增计数器）给出
# ---------------------------------------------------------------------------
def _seq_id(prefix: str, seq: int, *, width: int) -> str:
    _check_seq(seq, limit=10 ** width - 1, what=prefix)
    return f"{prefix}-{seq:0{width}d}"


def _parse_seq(value: str, regex: re.Pattern[str], label: str) -> int:
    m = regex.match(str(value or ""))
    if not m:
        raise IdFormatError(f"不是合法的 {label}：{value!r}")
    return int(m.group(1))


def format_finding_id(seq: int) -> str:
    return _seq_id("ISS", seq, width=4)


def parse_finding_id(value: str) -> int:
    return _parse_seq(value, _FINDING_RE, "finding_id")


def format_evidence_id(seq: int) -> str:
    return _seq_id("EV", seq, width=4)


def parse_evidence_id(value: str) -> int:
    return _parse_seq(value, _EVIDENCE_RE, "evidence_id")


def format_dispute_id(seq: int) -> str:
    return _seq_id("DSP", seq, width=4)


def parse_dispute_id(value: str) -> int:
    return _parse_seq(value, _DISPUTE_RE, "dispute_id")


def format_decision_id(seq: int) -> str:
    return _seq_id("DEC", seq, width=4)


def parse_decision_id(value: str) -> int:
    return _parse_seq(value, _DECISION_RE, "decision_id")
