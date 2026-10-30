# -*- coding: utf-8 -*-
"""ReportService：按 run_id 取报告、列报告、验证报告没被改过。

为什么需要它：
    「报告在哪」此前是**入口自己知道的事**——CLI 直接拼 `outputs/<id>.json`，
    v1 拼 `outputs/v1/<id>.json`，server 又拼一遍。三处各写一份路径规则，
    加一种引擎就多一处漏。这里把「run_id → 报告」收敛成一个服务。

两级来源（顺序即优先级）：
    v1      `outputs/v1/<review_id>.json`   新链路的影子报告（自描述、带 unsupported_blocks）
    legacy  `outputs/<review_id>.json`      旧链路正式报告

为什么不是「新报告优先就永远读新的」：切流前后同一 review_id 可能两份都在。
    这里**按来源顺序取第一份存在**，并把 `source` 一起返回——调用方看得到
    「你现在读的是哪条链路的结论」，而不是被悄悄换了来源。

完整性校验是**注入**的：本层不 import `tools/integrity.py`（依赖方向见
ARCHITECTURE §3）。没注入校验函数时 `verify()` 返回 `None`（= 没验证），
不返回 `True` —— 没验证不等于通过。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain import Verdict
from ..domain.ids import parse_run_id

# 报告来源，顺序即查找优先级
SOURCE_V1 = "v1"
SOURCE_LEGACY = "legacy"
DEFAULT_ORDER: tuple[str, ...] = (SOURCE_V1, SOURCE_LEGACY)


@dataclass(frozen=True, slots=True)
class ReportRef:
    """一份报告的位置与来源（不读内容，给列表用）。"""

    review_id: str
    source: str
    path: Path

    def as_dict(self) -> dict[str, Any]:
        return {"review_id": self.review_id, "source": self.source,
                "path": self.path.as_posix()}


class ReportService:
    """读报告。`roots` 形如 `{"v1": OUTPUTS/"v1", "legacy": OUTPUTS}`。"""

    def __init__(self, roots: Mapping[str, Path],
                 *, compute_hash: Callable[[Mapping[str, Any]], str] | None = None,
                 order: tuple[str, ...] = DEFAULT_ORDER) -> None:
        self._roots = {name: Path(path) for name, path in roots.items()}
        self._compute_hash = compute_hash
        self._order = tuple(name for name in order if name in self._roots)

    # ---- 定位 ----
    def locate(self, run_id: str) -> ReportRef | None:
        """按 run_id 找报告。run_id 必须是 `R-YYYYMMDD-NNN-RUNNN`。"""
        review_id, _seq = parse_run_id(run_id)
        for source in self._order:
            path = self._roots[source] / f"{review_id}.json"
            if path.is_file():
                return ReportRef(review_id=review_id, source=source, path=path)
        return None

    def locate_review(self, review_id: str) -> ReportRef | None:
        """按 review_id 找报告（调用方只有 R-id 时用）。"""
        for source in self._order:
            path = self._roots[source] / f"{review_id}.json"
            if path.is_file():
                return ReportRef(review_id=review_id, source=source, path=path)
        return None

    # ---- 读 ----
    def get_report(self, run_id: str) -> dict[str, Any] | None:
        ref = self.locate(run_id)
        if ref is None:
            return None
        return json.loads(ref.path.read_text(encoding="utf-8"))

    def get_report_with_source(self, run_id: str) -> tuple[dict[str, Any], str] | None:
        """返回 (报告, 来源)。来源必须一起给出去，否则「读的是哪条链路」无从判断。"""
        ref = self.locate(run_id)
        if ref is None:
            return None
        return json.loads(ref.path.read_text(encoding="utf-8")), ref.source

    # ---- 校验 ----
    def verify(self, run_id: str) -> bool | None:
        """复算 report_hash。

        `None` = 没注入校验函数，**没验证**；`True/False` = 验过且结论明确。
        报告缺 `integrity.report_hash` 时返回 False（不能证明没被改，就不算通过）。
        """
        if self._compute_hash is None:
            return None
        report = self.get_report(run_id)
        if report is None:
            return None
        stored = (report.get("integrity") or {}).get("report_hash")
        if not stored:
            return False
        return self._compute_hash(report) == stored

    # ---- 列表 ----
    def list_reports(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """汇总视图：每份报告一行。按 review_id 倒序（新的在前）。"""
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in self._order:              # 先 v1 再 legacy：同一 id 只出现一次
            root = self._roots[source]
            if not root.is_dir():
                continue
            for path in sorted(root.glob("R-*.json"), reverse=True):
                review_id = path.stem
                if review_id in seen:
                    continue
                seen.add(review_id)
                row = {"review_id": review_id, "source": source,
                       "path": path.as_posix()}
                row.update(self._summary(path))
                rows.append(row)
        rows.sort(key=lambda r: r["review_id"], reverse=True)
        return rows[:limit] if limit else rows

    def _summary(self, path: Path) -> dict[str, Any]:
        """从报告里摘「列表要显示的东西」。坏报告给 `readable=False`，不抛。"""
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"readable": False}
        verdict = report.get("verdict")
        issues = report.get("issues")
        return {
            "readable": True,
            "verdict": verdict if verdict in {v.value for v in Verdict} else None,
            "issues": len(issues) if isinstance(issues, list) else 0,
            "verdict_basis": report.get("core_conclusion"),
        }
