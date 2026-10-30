# -*- coding: utf-8 -*-
"""文本工具：同片段判定、句子定位。

为什么要放在包根而不是某个层里：这几条规则被三层同时用到——
能力层（语义审候选去重）、运行时（交叉复核）、旧流程（折叠/合并）。
放进任何一层，另外两层都得反向依赖；放在包根则只有「被依赖」。

口径与旧实现逐字一致（`review.py` 早期版本的同名函数）：
去标点后互相包含 → 1.0；否则字符二元组 Jaccard。
"""
from __future__ import annotations

import re
from typing import Final

_SENT_PUNCT: Final = set("。！？\n")
_STRIP_RE: Final = re.compile(
    r"[\s\u3000，。！？、；：\"'“”‘’（）()\[\]【】《》〈〉…—\-·]+")
_SENT_SPLIT_RE: Final = re.compile(r"[。！？\n]")
_SENT_KEY_STRIP_RE: Final = re.compile(r"[\s，,、：:（）()]")


def normalize_quote(quote: str | None) -> str:
    return _STRIP_RE.sub("", str(quote or ""))


def quote_overlap(a: str | None, b: str | None, *, threshold: float = 0.6) -> float:
    """同片段判定：去标点后互相包含，或字符二元组 Jaccard。"""
    na, nb = normalize_quote(a), normalize_quote(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    ga = {na[i:i + 2] for i in range(len(na) - 1)}
    gb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def same_clause(a: str | None, b: str | None, *, threshold: float = 0.6) -> bool:
    return quote_overlap(a, b) >= threshold


def sentence_no(text: str, index: int) -> int:
    """index 之前已经完整结束了几个句子（1 起算）。"""
    segment = (text or "")[:index]
    sentences = [s for s in _SENT_SPLIT_RE.split(segment) if s.strip()]
    return len(sentences) + 1


def sentence_key(quote: str | None, *, width: int = 20) -> str:
    """句级指纹：取首句前 width 个有效字符。

    分块审查时相邻块有重叠，模型会在两块里对同一句给出长度不同的引用
    （整段 vs 单句），单纯按整段重合度去重挡不住，会重复计数。
    """
    head = _SENT_SPLIT_RE.split(str(quote or ""))[0]
    return _SENT_KEY_STRIP_RE.sub("", head)[:width]
