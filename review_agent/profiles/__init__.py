# -*- coding: utf-8 -*-
"""Profile 层：把 Agent manifest + 情境 + 格式规格合成 ReviewProfile 快照。

阶段 1 只有纯函数 `resolve_profile()`；读取配置文件的加载器在阶段 2 接入
Application（届时复用 `tools/agent_spec.py`、`tools/scenario.py`、`tools/format_spec.py`
已有的解析与校验，不另写一套）。
"""
from __future__ import annotations

from .resolver import LEGACY_AGENT_ID, resolve_profile

__all__ = ["LEGACY_AGENT_ID", "resolve_profile"]
