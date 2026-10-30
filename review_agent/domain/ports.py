# -*- coding: utf-8 -*-
"""端口（Protocol）：领域层依赖的「外部能力」抽象。

依赖方向（见 docs/ARCHITECTURE.md）：
    Application → Runtime → Capabilities → Domain
    Domain 不 import 任何上层，也不 import 数据库 / 模型 / Web 框架。

Domain 需要外部能力时，只声明 Protocol，由 infrastructure 实现：
    · 时钟          —— 测试要能注入固定时间，报告才可复现
    · ID 分配       —— 旧实现靠扫 outputs/ + ledger 算下一个号（并发要锁文件）
    · 仓储          —— Review / Run 的持久化
    · Checkpoint    —— 中断恢复
    · 审计出口      —— 事件写到哪里（jsonl / DB / 遥测）

这里全部用 typing.Protocol，不做继承：实现方不需要 import 领域包，
测试里用 5 行的假对象就能替换掉 PostgreSQL / 真实时钟。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from .models import AuditEvent, Checkpoint, Review, ReviewRun


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """当前时间（带时区）。"""


@runtime_checkable
class ReviewIdAllocator(Protocol):
    def allocate(self, day: date) -> str:
        """分配当日内下一个合法 review_id（R-YYYYMMDD-NNN）。

        实现必须保证并发下不重号；旧实现用 outputs/ 里的 .lock 文件 + O_EXCL。
        """


@runtime_checkable
class ReviewRepository(Protocol):
    def get(self, review_id: str) -> Review | None: ...

    def save(self, review: Review) -> None:
        """整体保存（含 runs）。实现自行决定事务边界。"""

    def add_run(self, review_id: str, run: ReviewRun) -> None:
        """追加一次执行。与 save 分开是为了让「新建 Run」成为一次原子事件。"""


@runtime_checkable
class CheckpointStore(Protocol):
    def save(self, checkpoint: Checkpoint) -> None: ...

    def latest(self, run_id: str) -> Checkpoint | None:
        """返回最近一次可恢复点；None = 没有可恢复状态，只能重跑。"""


@runtime_checkable
class AuditSink(Protocol):
    def emit(self, event: AuditEvent) -> None:
        """审计事件出口。实现必须保证「先落盘再返回」，进程崩溃不丢已发生事件。"""


@runtime_checkable
class ReportStore(Protocol):
    def write(self, review_id: str, run_id: str, report: dict[str, Any]) -> str:
        """落盘一份报告，返回可定位的引用（路径或 URL）。"""
