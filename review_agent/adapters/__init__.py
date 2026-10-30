# -*- coding: utf-8 -*-
"""迁移期适配器（边界层）。

不是长期架构的一部分：阶段 3 完成后，读取历史报告只保留「导入」用途，
运行期不再经过这里。

放在 `review_agent/adapters/` 而不是 domain 内部，是因为它同时依赖领域模型
与 Profile 解析器（上层）；让它住在 domain 里会逼着领域层反向依赖上层。
"""
from __future__ import annotations

from .legacy_report import CONSUMED_KEYS, AdaptedReview, adapt_report

__all__ = ["CONSUMED_KEYS", "AdaptedReview", "adapt_report"]
