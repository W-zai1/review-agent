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

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from .models import AuditEvent, Checkpoint, Review, ReviewRun


@dataclasses.dataclass(frozen=True, slots=True)
class ModelReply:
    """一次模型调用的结果，与供应商无关。

    为什么单独定义而不是直接复用 `tools/llm_client.LLMResult`：
        领域层不许 import `tools/`（ARCHITECTURE §3），而语义审需要读
        `content` / `model` / `finish_reason` / `usage` / `latency_ms` 五个字段。
        这里只登记「审查流程真正会用到」的字段；供应商特有的东西（原始 JSON、
        token 明细、重试次数）留在适配器里，不污染领域。

    `finish_reason == "length"` 是「输出没写完」的唯一可靠信号——结论完整性
    判定靠它，所以它是端口契约的一部分，不是可选装饰。
    """

    content: str
    model: str = ""
    finish_reason: str = ""
    usage: Mapping[str, int] = dataclasses.field(default_factory=dict)
    latency_ms: int = 0


@runtime_checkable
class ModelClient(Protocol):
    """模型调用端口：换供应商只换实现，审查节点与 prompt 不用改。

    契约刻意只有一个方法：`review_dimension` 只调 `client.chat(messages)`。
    把 temperature / max_tokens 放进端口会让每个实现都要处理一遍可选项，
    而实际调用点从来只传 messages。
    """

    def chat(self, messages: Sequence[Mapping[str, str]]) -> ModelReply:
        """发一次对话请求。失败抛异常，由调用方决定降级还是中止。"""


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
