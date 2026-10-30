# -*- coding: utf-8 -*-
"""文本相似度与句子定位（实现已上移到 `review_agent.text`）。

保留这个模块是为了不打断既有 import 路径：节点、旧流程都从这里取同一份实现。
"""
from __future__ import annotations

from ...text import (
    normalize_quote,
    quote_overlap,
    same_clause,
    sentence_key,
    sentence_no,
)

__all__ = ["normalize_quote", "quote_overlap", "same_clause",
           "sentence_key", "sentence_no"]
