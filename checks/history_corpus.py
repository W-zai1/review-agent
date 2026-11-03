# -*- coding: utf-8 -*-
"""显式加载历史报告评估语料。

公开检查不能隐式读取运行目录 ``outputs/``：同一份代码会因为用户保留的报告
数量不同而得到不同结果。历史回放测试只在设置 ``REVIEW_AGENT_HISTORY_DIR``
时运行；一旦设置了该变量，语料不完整或报告损坏都必须直接报错。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

CORPUS_ENV = "REVIEW_AGENT_HISTORY_DIR"
REVIEW_ID_RE = re.compile(r"^R-\d{8}-\d{3}$")

KNOWN_VERDICT_DEVIATIONS = frozenset({
    "R-20261028-045", "R-20261028-046", "R-20261028-047",
    "R-20261028-055", "R-20261028-071",
})


class HistoryCorpusError(RuntimeError):
    """历史语料不可用或内容不完整。"""


@dataclass(frozen=True)
class HistoricalReport:
    path: Path
    review_id: str
    data: dict


def configured() -> bool:
    """环境变量是否显式指向历史语料。"""
    return bool(os.environ.get(CORPUS_ENV, "").strip())


def configured_root() -> Path | None:
    raw = os.environ.get(CORPUS_ENV, "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def load_reports(
    root: Path | str | None = None,
    *,
    require_known: bool = False,
) -> tuple[HistoricalReport, ...]:
    """加载 ``R-*.json`` 历史报告；未配置环境变量时返回空元组。

    显式配置后采用 fail-closed：目录不存在、没有报告、坏 JSON、ID 与文件名
    不一致、重复报告或缺少已登记偏差样本，都会抛 ``HistoryCorpusError``。
    """
    base = Path(root).expanduser().resolve() if root is not None else configured_root()
    if base is None:
        return ()
    if not base.is_dir():
        raise HistoryCorpusError(f"历史语料目录不存在：{base}")

    reports: list[HistoricalReport] = []
    seen: set[str] = set()
    for path in sorted(base.rglob("R-*.json")):
        relative = path.relative_to(base)
        if any(part.startswith(".") for part in relative.parts):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HistoryCorpusError(f"历史报告不可读：{relative}: {exc}") from exc
        if not isinstance(data, dict):
            raise HistoryCorpusError(f"历史报告不是对象：{relative}")
        review_id = data.get("review_id")
        if not isinstance(review_id, str) or not REVIEW_ID_RE.fullmatch(review_id):
            raise HistoryCorpusError(f"历史报告 review_id 不合法：{relative}")
        if path.stem != review_id:
            raise HistoryCorpusError(
                f"历史报告文件名与 review_id 不一致：{relative} != {review_id}")
        if not isinstance(data.get("verdict"), str):
            raise HistoryCorpusError(f"历史报告缺少字符串 verdict：{relative}")
        if review_id in seen:
            raise HistoryCorpusError(f"历史报告 review_id 重复：{review_id}")
        seen.add(review_id)
        reports.append(HistoricalReport(path=path, review_id=review_id, data=data))

    if not reports:
        raise HistoryCorpusError(f"历史语料目录中没有 R-*.json：{base}")
    if require_known:
        missing = sorted(KNOWN_VERDICT_DEVIATIONS - seen)
        if missing:
            raise HistoryCorpusError(
                "历史语料缺少已登记偏差报告：" + "、".join(missing))
    return tuple(reports)
