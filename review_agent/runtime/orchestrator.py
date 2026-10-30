# -*- coding: utf-8 -*-
"""编排器：按 Workflow 推进 RunState，每个阶段推进一次，事后落 Checkpoint。

它只做四件事（与 AGENT_RUNTIME §2 对 Supervisor 的定义一致）：

    挑下一个阶段 → 按声明顺序跑该阶段的节点 → 合成结果并推进一次 → 落检查点

不做什么：
    * 不产生结论（结论属于节点）
    * 不解析输入（属于 Document Analyzer）
    * 不决定重试策略以外的业务规则（重试次数是运行配置）

为什么「一个阶段只推进一次」很重要：
    阶段是状态机的边，`completed_stages` 必须是 STAGE_ORDER 的连续前缀。
    如果同一阶段的每个节点各推进一次，前缀里就会出现重复阶段，
    「现在到哪了」和「还能不能恢复」都失去意义。所以同阶段的多个节点结果
    先由 `merge_results()` 合成一个 NodeResult，再交给 `advance()`。

中断恢复的最小语义（阶段 4 才做完整版）：
    `run(state)` 总是从 `state.stage` 继续，不重跑已完成阶段。
    幂等由调用方保证：同一份 state 跑两次得到同样的状态（节点本身必须确定性；
    语义审节点例外，它的幂等靠模型缓存，见 AGENT_RUNTIME §7）。

超时（阶段 4）：
    `timeout` 是**单节点**上限，不是整次审查的上限。理由：整次超时只能告诉你
    「跑太久了」，单节点超时能告诉你「卡在哪个节点的第几次尝试」。
    超时被记为 `retryable=True` 的失败——模型慢、IO 抖动都是可重试的；
    而节点自己报出的确定性错误（入参不对、状态机拒绝）不可重试。
    默认 `None`（不限制）：迁移期先不改变既有行为，由入口显式打开。
"""
from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..domain import Checkpoint, Stage, ids
from .node import (
    Node,
    NodeContext,
    NodeResult,
    RunState,
    Workflow,
    advance,
    merge_results,
    stage_requirements,
)

COMPLETED = "completed"
FAILED = "failed"
INCOMPLETE = "incomplete"


def state_hash(state: RunState) -> str:
    """状态摘要哈希：判断「恢复出来的状态」与「断点记录」是否一致。

    只摘会让结论变化的东西（阶段、结论身份与定级、证据/争议/裁定身份、产物摘要），
    不把整个对象序列化进去——哈希要能跨版本比对，而不是换个 Python 版本就失效。
    """
    payload = {
        "review_id": state.review_id,
        "run_id": state.run_id,
        "stage": state.stage.value,
        "completed": [s.value for s in state.completed_stages],
        "findings": [[f.id, f.severity.value, round(float(f.confidence), 4),
                      f.status.value] for f in state.findings],
        "evidence": [e.id for e in state.evidence],
        "disputes": [d.id for d in state.disputes],
        "decision": state.decision.id if state.decision else None,
        "artifacts": {key: _digest(value) for key, value in sorted(state.artifacts.items())},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def next_finding_seq(state: RunState) -> int:
    """本次 Run 里下一个可用的发现序号（从 1 开始）。

    编号是 Run 级资源：不从「已有发现的最大号 + 1」接着发，两个节点就会撞号。
    """
    return _max_seq(state.findings) + 1


def _max_seq(findings: Any) -> int:
    best = 0
    for finding in findings:
        try:
            best = max(best, ids.parse_finding_id(finding.id))
        except Exception:                              # noqa: BLE001 - 脏 id 不参与编号
            continue
    return best


class CheckpointStore(Protocol):
    def append(self, checkpoint: Checkpoint) -> None: ...

    def for_run(self, run_id: str) -> tuple[Checkpoint, ...]: ...


class MemoryCheckpointStore:
    """内存实现：测试与单进程用；进程退出即丢。"""

    def __init__(self) -> None:
        self._items: dict[str, list[Checkpoint]] = {}

    def append(self, checkpoint: Checkpoint) -> None:
        self._items.setdefault(checkpoint.run_id, []).append(checkpoint)

    def for_run(self, run_id: str) -> tuple[Checkpoint, ...]:
        return tuple(self._items.get(run_id, ()))


class FileCheckpointStore:
    """JSONL 实现：一行一个检查点，进程被杀也不丢已完成的阶段。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, run_id: str) -> Path:
        return self.root / f"{run_id}.jsonl"

    def append(self, checkpoint: Checkpoint) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps({
            "run_id": checkpoint.run_id,
            "stage": checkpoint.stage.value,
            "sequence": checkpoint.sequence,
            "state_hash": checkpoint.state_hash,
            "created_at": checkpoint.created_at.isoformat(),
            "summary": checkpoint.summary,
        }, ensure_ascii=False)
        with open(self._path(checkpoint.run_id), "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    def for_run(self, run_id: str) -> tuple[Checkpoint, ...]:
        path = self._path(run_id)
        if not path.exists():
            return ()
        out: list[Checkpoint] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
                out.append(Checkpoint(
                    run_id=item["run_id"],
                    stage=Stage(item["stage"]),
                    sequence=int(item["sequence"]),
                    state_hash=str(item["state_hash"]),
                    created_at=datetime.datetime.fromisoformat(item["created_at"]),
                    summary=str(item.get("summary") or ""),
                ))
            except (KeyError, ValueError, TypeError):
                # 坏行不该让「还能恢复吗」这个问题变成异常：跳过并按剩余记录判断
                continue
        return tuple(out)


@dataclass(frozen=True, slots=True)
class NodeEvent:
    """一次节点执行（审计流的最小单元）。"""

    stage: Stage
    node: str
    ok: bool
    attempt: int
    latency_ms: int = 0
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"stage": self.stage.value, "node": self.node, "ok": self.ok,
                "attempt": self.attempt, "latency_ms": self.latency_ms,
                "errors": list(self.errors), "warnings": list(self.warnings)}


@dataclass(frozen=True, slots=True)
class RunOutcome:
    state: RunState
    status: str
    events: tuple[NodeEvent, ...] = ()
    errors: tuple[str, ...] = ()
    checkpoints: tuple[Checkpoint, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == COMPLETED


@dataclass(frozen=True, slots=True)
class Orchestrator:
    """按 Workflow 推进状态。"""

    workflow: Workflow
    checkpoints: CheckpointStore | None = None
    max_attempts: int = 1
    config: Mapping[str, Any] = field(default_factory=dict)
    timeout: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts 从 1 开始")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"timeout 必须为正数（秒），实际 {self.timeout!r}")

    async def run(self, state: RunState) -> RunOutcome:
        events: list[NodeEvent] = []
        while state.stage not in state.completed_stages:
            stage = state.stage
            nodes = self.workflow.nodes_for(stage)
            if not nodes:
                return RunOutcome(
                    state=state, status=INCOMPLETE, events=tuple(events),
                    errors=(f"workflow 未覆盖阶段 {stage.value}；"
                            "不允许悄悄跳过阶段出结论",),
                    checkpoints=self._history(state),
                )
            results: list[NodeResult] = []
            next_seq = next_finding_seq(state)
            for node in nodes:
                result, node_events = await self._run_node(node, state, next_seq)
                events.extend(node_events)
                if not result.success:
                    return RunOutcome(
                        state=state, status=FAILED, events=tuple(events),
                        errors=tuple(result.errors) or (f"{node.name} 失败",),
                        checkpoints=self._history(state),
                    )
                results.append(result)
                # 同阶段的后一个节点接着前一个的编号往后发，避免重复
                next_seq = max(next_seq, _max_seq(result.findings) + 1)
            merged = merge_results(results)
            state = advance(state, stage, merged,
                            required=stage_requirements(nodes))
            self._save(state, stage)
        return RunOutcome(state=state, status=COMPLETED, events=tuple(events),
                          checkpoints=self._history(state))

    async def _run_node(self, node: Node, state: RunState,
                        next_seq: int) -> tuple[NodeResult, list[NodeEvent]]:
        events: list[NodeEvent] = []
        result = NodeResult.fail(f"{node.name} 从未执行")
        for attempt in range(1, self.max_attempts + 1):
            started = time.perf_counter()
            try:
                call = node.run(NodeContext(
                    state=state, config=self.config, next_finding_seq=next_seq))
                if self.timeout is None:
                    result = await call
                else:
                    result = await asyncio.wait_for(call, timeout=self.timeout)
            except TimeoutError:
                # 3.11+ 的内建 TimeoutError 就是 asyncio.TimeoutError
                result = NodeResult.fail(
                    f"{node.name} 超时（超过 {self.timeout:g}s 未返回）",
                    retryable=True)
            except Exception as e:                      # noqa: BLE001 - 编排边界
                result = NodeResult.fail(
                    f"{node.name} 抛异常：{type(e).__name__}: {e}")
            events.append(NodeEvent(
                stage=node.stage, node=node.name, ok=result.success, attempt=attempt,
                latency_ms=int((time.perf_counter() - started) * 1000),
                errors=result.errors, warnings=result.warnings,
            ))
            if result.success or not result.retryable or attempt == self.max_attempts:
                break
        return result, events

    def _save(self, state: RunState, stage: Stage) -> None:
        if self.checkpoints is None:
            return
        history = self.checkpoints.for_run(state.run_id)
        self.checkpoints.append(Checkpoint(
            run_id=state.run_id,
            stage=stage,
            sequence=len(history),
            state_hash=state_hash(state),
            created_at=datetime.datetime.now().astimezone(),
            summary=(f"{stage.value} 完成：findings={len(state.findings)} "
                     f"evidence={len(state.evidence)} disputes={len(state.disputes)}"),
        ))

    def _history(self, state: RunState) -> tuple[Checkpoint, ...]:
        return self.checkpoints.for_run(state.run_id) if self.checkpoints else ()
