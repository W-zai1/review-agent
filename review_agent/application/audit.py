# -*- coding: utf-8 -*-
"""审计事件流：把三套旧记录汇成一条可按序号回放的事件流。

旧实现把「发生了什么」记在三处，字段各不相同：

    task.json        steps[]：节点名、阶段、耗时、错误
    tools.jsonl      工具调用：工具名、结论、耗时
    decisions.jsonl  决策：什么时候决定了什么、依据是什么
    （另有 checkpoints，只在内存里，进程重启就没了）

后果是「审计中心」只能按文件分别展示，回答不了「第 7 步之前调了哪个工具」。
这里给出一条**有序**的 `AuditEvent` 流；旧的三个文件改成由这条流**投影**生成
（`to_legacy()`），所以文件格式不变、消费者不用改，但事实来源只剩一份。

顺序的诚实说明：
    跨来源没有统一时钟（旧记录里只有各自的时间戳，甚至没有），所以 `seq`
    是「来源优先级 + 来源内原始顺序」，**不是墙钟顺序**。
    优先级：节点事件 → 工具调用 → 决策 → 检查点。
    想知道真实先后，要看 `ts`（有的话）；这条限制写进 §已知边界，不装成时间轴。

不做什么：
    * 不改旧文件的路径与格式（UI / 历史报告都在读）
    * 不伪造 `ts`：没有时间的就给 `None`，不用「现在」冒充
"""
from __future__ import annotations

import datetime
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..domain import AuditEvent, AuditKind

ENGINE = "architecture-v1"


def _iso(value: datetime.datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def build_stream(*, run_id: str,
                 node_events: Iterable[Any] = (),
                 tool_logs: Iterable[Mapping[str, Any]] = (),
                 decisions: Iterable[Mapping[str, Any]] = (),
                 checkpoints: Iterable[Any] = ()) -> tuple[AuditEvent, ...]:
    """把四个来源汇成一条流。每一类在流里保持原始相对顺序。"""
    out: list[AuditEvent] = []

    def add(kind: AuditKind, *, name: str, status: str = "success",
            latency_ms: int = 0, detail: Mapping[str, Any] | None = None,
            ref: str | None = None) -> None:
        out.append(AuditEvent(
            seq=len(out) + 1, run_id=run_id, kind=kind, name=name,
            status=status, latency_ms=max(0, int(latency_ms or 0)),
            detail=dict(detail or {}), ref=ref,
        ))

    for event in node_events:
        detail = dict(event.as_dict()) if hasattr(event, "as_dict") else dict(event)
        add(AuditKind.STEP,
            name=f"{detail.get('stage')}/{detail.get('node')}",
            status="success" if detail.get("ok", True) else "failed",
            latency_ms=detail.get("latency_ms") or 0,
            detail=detail, ref=str(detail.get("node") or ""))

    for item in tool_logs:
        detail = dict(item)
        add(AuditKind.TOOL,
            name=str(detail.get("tool") or "tool"),
            status=str(detail.get("status") or "success"),
            latency_ms=detail.get("latency_ms") or 0,
            detail=detail)

    for item in decisions:
        detail = dict(item)
        add(AuditKind.DECISION,
            name=str(detail.get("step") or detail.get("kind") or "decision"),
            status="info", detail=detail)

    for checkpoint in checkpoints:
        add(AuditKind.STATE,
            name=f"checkpoint/{getattr(checkpoint, 'stage', '')}",
            status="info",
            detail={"sequence": getattr(checkpoint, "sequence", None),
                    "state_hash": getattr(checkpoint, "state_hash", None),
                    "summary": getattr(checkpoint, "summary", "")},
            ref=getattr(checkpoint, "run_id", None))

    return tuple(out)


def to_legacy(stream: Sequence[AuditEvent]) -> dict[str, list[dict[str, Any]]]:
    """由事件流反向投影出旧的三个文件内容（格式与旧实现逐字一致）。

    为什么还要投影：UI 与历史报告脚本读的是 `tools.jsonl` / `decisions.jsonl` /
    `task.json`。切流不能顺带改它们的格式——那是发布动作。
    """
    steps = [dict(e.detail) for e in stream if e.kind is AuditKind.STEP]
    tools = [dict(e.detail) for e in stream if e.kind is AuditKind.TOOL]
    decisions = [dict(e.detail) for e in stream if e.kind is AuditKind.DECISION]
    return {"steps": steps, "tools": tools, "decisions": decisions}


def write_stream(path: Path, stream: Sequence[AuditEvent]) -> int:
    """写成 jsonl（一行一个事件），返回条数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for event in stream:
            handle.write(json.dumps({
                "seq": event.seq,
                "run_id": event.run_id,
                "kind": event.kind.value,
                "name": event.name,
                "status": event.status,
                "latency_ms": event.latency_ms,
                "ts": _iso(event.ts),
                "ref": event.ref,
                "detail": dict(event.detail),
            }, ensure_ascii=False) + "\n")
    return len(stream)


def load_stream(path: Path) -> tuple[AuditEvent, ...]:
    """读回事件流（测试与审计中心用）。坏行跳过，不因为一行脏数据整份读不出来。"""
    if not path.exists():
        return ()
    events: list[AuditEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            events.append(AuditEvent(
                seq=int(data["seq"]), run_id=str(data["run_id"]),
                kind=AuditKind(data["kind"]), name=str(data["name"]),
                status=str(data.get("status") or "success"),
                latency_ms=int(data.get("latency_ms") or 0),
                detail=dict(data.get("detail") or {}),
                ref=data.get("ref"),
            ))
        except (ValueError, KeyError, TypeError):
            continue
    return tuple(events)
