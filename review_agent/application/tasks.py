# -*- coding: utf-8 -*-
"""TaskService：任务身份、状态与查询（阶段 2 收尾）。

为什么需要它：
    六节点流水线能跑之后，「跑一次」仍只存在于 CLI 的一次函数调用里——
    没有任务身份、没有状态可查、没有「这台机器上现在有哪些审查」。
    server 的 `JOBS` 是内存字典，进程一重启就失忆：报告文件还在，
    但「那次跑完没有、卡在哪、错在哪」没人知道。

    这一层把任务生命周期变成**落盘记录**：

        start_review(target, profile, purpose) -> TaskRecord（含 review_id / run_id）
        get_task(review_id)                    -> 读记录，不重跑
        list_runs()                            -> 按时间倒序列出
        mark()                                 -> 外部状态变更（取消 / 人工复核）

边界（与 docs/ARCHITECTURE.md 的依赖方向一致）：
    * **不 import `review.py` / `tools/`**：真正的执行由 `runner` 注入，
      CLI 和 server 各自装配自己那一份。这样任务层能被单测、能换实现。
    * **不建数据库**：迁移期一次一份 JSON 足够；将来换 SQLite / PostgreSQL
      只换 `TaskStore` 的实现，`TaskService` 不改。
    * **不复制恢复语义**：续跑由 `ReviewApplication.resume(run_id)` +
      `FileRunStore` 负责（见 runtime/serialization.py）。这里只记状态，
      不自己再实现一套「从哪继续」。
    * **不做并发调度与幂等去重**：阶段 4 的活。当前同目标重复提交会拿到
      两个 review_id，这是已知且被文档承认的缺口，不在这里偷偷「合并」。
"""
from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..domain import NotFoundError, RunStatus, Stage
from ..domain.ids import format_run_id, parse_run_id

TASK_SCHEMA = 1


def _now_iso() -> str:
    """任务时间戳：本地时间、秒级。排序按字符串即可（同格式、同长度）。"""
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# runner 契约
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """runner 的返回值：只描述「跑完了什么」，任务身份由 TaskService 给。

    为什么 status 用领域枚举而不是裸字符串：任务状态最终要进 `RunStatus`
    那张表（`created/running/awaiting_decision/completed/failed/aborted`），
    两套取值迟早对不上。运行时自己的 `completed/failed/incomplete` 由
    runner 在**边界处**翻译成领域枚举，翻译规则写在一处。
    """

    status: RunStatus
    stage: Stage
    verdict: str | None = None
    findings: int = 0
    evidence: int = 0
    report_path: str | None = None
    error: str | None = None


class Runner(Protocol):
    """一次真实执行。由 CLI / server 装配（这里不 import 旧脚本）。"""

    def __call__(self, target: Path, *, review_id: str,
                 profile_id: str | None, purpose: str | None,
                 engine: str, use_llm: bool) -> TaskOutcome: ...


# ---------------------------------------------------------------------------
# 任务记录
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TaskRecord:
    """一条任务记录。落盘即事实：进程重启后以它为准。"""

    review_id: str
    run_id: str
    status: str
    stage: str
    target: str
    engine: str = "v1"
    profile_id: str | None = None
    purpose: str | None = None
    verdict: str | None = None
    findings: int = 0
    evidence: int = 0
    report_path: str | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        review_id, _seq = parse_run_id(self.run_id)
        if review_id != self.review_id:
            raise ValueError(
                f"run_id {self.run_id!r} 不属于 review_id {self.review_id!r}")
        RunStatus(self.status)          # 非法状态立刻报错，不静默降级
        Stage(self.stage)

    # ---- 序列化 ----
    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema"] = TASK_SCHEMA
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskRecord":
        version = int(data.get("schema") or 0)
        if version != TASK_SCHEMA:
            raise ValueError(
                f"不认识的 task schema={version}（期望 {TASK_SCHEMA}）")
        return cls(
            review_id=str(data["review_id"]),
            run_id=str(data["run_id"]),
            status=str(data["status"]),
            stage=str(data["stage"]),
            target=str(data.get("target") or ""),
            engine=str(data.get("engine") or "v1"),
            profile_id=data.get("profile_id"),
            purpose=data.get("purpose"),
            verdict=data.get("verdict"),
            findings=int(data.get("findings") or 0),
            evidence=int(data.get("evidence") or 0),
            report_path=data.get("report_path"),
            error=data.get("error"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
@runtime_checkable
class TaskStore(Protocol):
    def save(self, record: TaskRecord) -> None: ...

    def get(self, review_id: str) -> TaskRecord | None: ...

    def all(self) -> list[TaskRecord]: ...


class FileTaskStore:
    """`<root>/<review_id>.json`，原子写（tmp + replace），失败不留半个文件。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, review_id: str) -> Path:
        return self.root / f"{review_id}.json"

    def save(self, record: TaskRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.review_id)
        payload = json.dumps(record.to_dict(), ensure_ascii=False,
                             sort_keys=True, indent=2)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    def get(self, review_id: str) -> TaskRecord | None:
        path = self._path(review_id)
        if not path.exists():
            return None
        return TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def all(self) -> list[TaskRecord]:
        """列出全部记录。坏文件跳过——一条脏记录不该让「列任务」整个失败。"""
        if not self.root.is_dir():
            return []
        records: list[TaskRecord] = []
        for path in sorted(self.root.glob("R-*.json")):
            try:
                records.append(
                    TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
        return records


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
class TaskService:
    """建任务 / 查任务 / 列任务。执行本身由 `runner` 完成。"""

    def __init__(self, *, runner: Runner, store: TaskStore,
                 allocate: Callable[[datetime.date], str],
                 root: Path | str | None = None) -> None:
        self._runner = runner
        self._store = store
        self._allocate = allocate
        # 给了根目录就把 target 记成相对路径（换台机器也能对上），否则存绝对值
        self._root = Path(root) if root is not None else None

    # ---- 写 ----
    def start_review(self, target: Path | str, *, profile_id: str | None = None,
                     purpose: str | None = None, engine: str = "v1",
                     use_llm: bool = False) -> TaskRecord:
        """建任务并同步跑完，返回最终记录。

        为什么同步：迁移期一次只跑一个任务，异步队列（线程/进程池）会先把
        「状态机、取消、超时」三件事一起引进来，那是阶段 4 的范围。
        接口形状（返回 record、含 review_id/run_id）在改成异步时不用变。
        """
        target_path = Path(target)
        if not target_path.exists():
            raise NotFoundError(f"找不到待审文件：{target_path}")

        created = _now_iso()
        review_id = self._allocate(datetime.date.today())
        record = TaskRecord(
            review_id=review_id,
            run_id=format_run_id(review_id, 1),
            status=RunStatus.RUNNING.value,
            stage=Stage.INTAKE.value,
            target=self._rel(target_path),
            engine=engine,
            profile_id=profile_id,
            purpose=purpose,
            created_at=created,
            updated_at=created,
        )
        # 先落「running」再执行：进程如果在执行中被杀，至少留下「有这个任务」
        self._store.save(record)

        try:
            outcome = self._runner(
                target_path, review_id=review_id, profile_id=profile_id,
                purpose=purpose, engine=engine, use_llm=use_llm)
        except BaseException as exc:                     # noqa: BLE001 - 失败必须留痕
            self._store.save(replace(
                record,
                status=RunStatus.FAILED.value,
                error=f"{type(exc).__name__}: {exc}",
                updated_at=_now_iso()))
            raise

        return self._finish(record, outcome)

    def _finish(self, record: TaskRecord, outcome: TaskOutcome) -> TaskRecord:
        done = replace(
            record,
            status=outcome.status.value,
            stage=outcome.stage.value,
            verdict=outcome.verdict,
            findings=outcome.findings,
            evidence=outcome.evidence,
            report_path=outcome.report_path,
            error=outcome.error,
            updated_at=_now_iso(),
        )
        self._store.save(done)
        return done

    def mark(self, review_id: str, status: RunStatus, *,
             error: str | None = None) -> TaskRecord:
        """外部状态变更（人工取消 / 复核完成）。状态值仍受领域枚举约束。"""
        record = self._require(review_id)
        updated = replace(record, status=status.value,
                          error=error or record.error, updated_at=_now_iso())
        self._store.save(updated)
        return updated

    # ---- 读 ----
    def get_task(self, review_id: str) -> TaskRecord | None:
        return self._store.get(review_id)

    def list_runs(self, *, limit: int | None = None) -> list[TaskRecord]:
        """按更新时间倒序。同一秒内创建的两条按 review_id 倒序，结果稳定。"""
        records = sorted(self._store.all(),
                         key=lambda r: (r.updated_at, r.review_id),
                         reverse=True)
        return records[:limit] if limit else records

    # ---- 内部 ----
    def _require(self, review_id: str) -> TaskRecord:
        record = self._store.get(review_id)
        if record is None:
            raise NotFoundError(f"找不到任务：{review_id}")
        return record

    def _rel(self, path: Path) -> str:
        if self._root is not None:
            try:
                return path.resolve().relative_to(
                    self._root.resolve()).as_posix()
            except ValueError:
                pass
        return path.resolve().as_posix()
