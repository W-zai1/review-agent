# -*- coding: utf-8 -*-
"""TaskService：任务身份、状态与生命周期（阶段 2 收尾 + 批次 1 补齐）。

为什么需要它：
    六节点流水线能跑之后，「跑一次」仍只存在于 CLI 的一次函数调用里——
    没有任务身份、没有状态可查、没有「这台机器上现在有哪些审查」。
    server 的 `JOBS` 是内存字典，进程一重启就失忆：报告文件还在，
    但「那次跑完没有、卡在哪、错在哪」没人知道。

    这一层把任务生命周期变成**落盘记录**：

        start_review(target, ...)  -> TaskRecord（含 review_id / run_id），同步跑完
        start_async(target, ...)   -> 立刻返回 QUEUED 记录，后台线程执行
        cancel(review_id)          -> 置位取消令牌 + 落 CANCELLED
        recover()                  -> 重启后体检：哪些能续、哪些已成废任务
        wait(review_id)            -> 等后台任务落终态
        get_task / list_runs / mark

边界（与 docs/ARCHITECTURE.md 的依赖方向一致）：
    * **不 import `review.py` / `tools/`**：真正的执行由 `runner` 注入，
      CLI 和 server 各自装配自己那一份。这样任务层能被单测、能换实现。
    * **不建数据库**：迁移期一次一份 JSON 足够；将来换 SQLite / PostgreSQL
      只换 `TaskStore` 的实现，`TaskService` 不改。
    * **不复制恢复语义**：续跑由 `ReviewApplication.resume(run_id)` +
      `FileRunStore` 负责（见 runtime/serialization.py）。`recover()` 只回答
      「该不该续」，不自己再实现一套「从哪继续」。
    * **取消信号借任务记录跨进程传播**：令牌仍是进程内对象，但执行中的
      `TaskService` 会观察同一份持久记录；外部 CLI 写入 CANCELLED 后，
      执行侧在下一个节点/阶段边界协作式退出。
"""
from __future__ import annotations

import concurrent.futures
import datetime
import inspect
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..domain import InvalidTransition, NotFoundError, RunStatus, Stage
from ..domain.ids import format_run_id, parse_run_id
from ..runtime.cancel import CancellationToken

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
    """一次真实执行。由 CLI / server 装配（这里不 import 旧脚本）。

    `token` 是**可选**参数：接了它就能在阶段/节点边界被取消；不接就退回
    "取消只体现在任务状态上"（`TaskService` 用 `inspect.signature` 判断，
    见 `_runner_accepts_token`）。不强制升级签名，是为了不逼所有既有 runner
    同时改造——迁移期一次改全仓最容易出事。
    """

    def __call__(self, target: Path, *, review_id: str,
                 profile_id: str | None, purpose: str | None,
                 engine: str, use_llm: bool,
                 token: CancellationToken | None = None) -> TaskOutcome: ...


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
    # 幂等键：同一份材料 + 同一套参数被重复提交时，用它认出"这是同一个任务"。
    # 存在记录里而不是内存里——进程重启后要能继续去重，否则重启就等于开了后门。
    idempotency_key: str | None = None

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
            # 老记录没有这个键：None 就是"没声明幂等性"，语义正确
            idempotency_key=data.get("idempotency_key"),
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
    """`<root>/<review_id>.json`，原子写（唯一临时名 + replace），失败不留半个文件。

    临时名必须**逐次唯一**（`<rid>.<pid>.<uuid>.tmp`），不能固定成 `<rid>.tmp`：
    取消与状态推进是并发的（HTTP 线程 `cancel()` 写 CANCELLED，执行线程写 RUNNING），
    固定临时名会让两次写互相覆盖对方正在 rename 的文件——实测在 Windows 上
    直接是 `PermissionError: [WinError 5] 拒绝访问`，任务状态当场卡死。
    这与台账墓碑那次是同一个坑，所以用同一种解法。
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _path(self, review_id: str) -> Path:
        return self.root / f"{review_id}.json"

    def save(self, record: TaskRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.review_id)
        payload = json.dumps(record.to_dict(), ensure_ascii=False,
                             sort_keys=True, indent=2)
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            # 目标文件可能正被另一个线程读取（`get()`），Windows 上偶发共享冲突：
            # 重试几次即可，仍失败就保留临时文件并把错误抛出去（不静默丢状态）。
            for attempt in range(5):
                try:
                    os.replace(str(tmp), str(path))
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def get(self, review_id: str) -> TaskRecord | None:
        path = self._path(review_id)
        if not path.exists():
            return None
        return TaskRecord.from_dict(self._read_json(path))

    def all(self) -> list[TaskRecord]:
        """列出全部记录。坏文件跳过——一条脏记录不该让「列任务」整个失败。

        `PermissionError` 也跳过：Windows 上正在 `os.replace` 的目标文件会被独占
        一瞬，这时读会抛共享冲突。跳过它比抛出去好（一次列表少一条，不会让
        「查任务」接口 500），但要重试——否则并发提交时 `find_active` 会漏掉
        刚写下的那条记录，于是同一个幂等键建出两个任务。
        """
        if not self.root.is_dir():
            return []
        records: list[TaskRecord] = []
        for path in sorted(self.root.glob("R-*.json")):
            try:
                records.append(TaskRecord.from_dict(self._read_json(path)))
            except (ValueError, KeyError, TypeError, PermissionError):
                continue
        return records

    @staticmethod
    def _read_json(path: Path) -> Any:
        """读一条记录；被 replace 撞到时重试（Windows 的共享冲突是瞬时的）。"""
        last: Exception | None = None
        for attempt in range(6):
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except PermissionError as exc:      # 目标正被另一个线程替换
                last = exc
                time.sleep(0.01 * (attempt + 1))
        raise last or PermissionError(str(path))


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """进程重启后的任务体检结果。

    `resumable` 是「还该接着跑」的：状态 running + 有检查点 + 本进程里没有执行者。
    是否真去续跑由调用方决定（续跑要用同一套 ports 重建 Application，见
    `ReviewApplication.resume`）——任务层只回答「该不该续」，不假装能自己续。

    `orphaned` 是「已经不可能跑完」的：状态 running 但没有检查点，或超龄未更新。
    它们被就地标成 FAILED 并写明原因，否则用户会看到一个永远「运行中」的任务。
    """

    resumable: tuple[TaskRecord, ...] = ()
    orphaned: tuple[TaskRecord, ...] = ()

    @property
    def total(self) -> int:
        return len(self.resumable) + len(self.orphaned)


class TaskService:
    """建任务 / 跑任务 / 取消 / 恢复 / 查任务。执行本身由 `runner` 完成。"""

    def __init__(self, *, runner: Runner, store: TaskStore,
                 allocate: Callable[[datetime.date], str],
                 root: Path | str | None = None,
                 stale_after: datetime.timedelta = datetime.timedelta(hours=1),
                 max_workers: int = 2) -> None:
        self._runner = runner
        self._store = store
        self._allocate = allocate
        # 给了根目录就把 target 记成相对路径（换台机器也能对上），否则存绝对值
        self._root = Path(root) if root is not None else None
        self._stale_after = stale_after
        self._max_workers = max(1, int(max_workers))
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        # 进程内活着的执行者：review_id → 令牌。**不落盘**——令牌是运行时对象，
        # 「谁还活着」这个问题在进程重启后只能靠检查点与时间判断（见 recover）。
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, concurrent.futures.Future] = {}
        self._lock = threading.Lock()
        # 状态迁移锁：`cancel()` 与 `_async_body()`（入队→开跑）必须互斥。
        # 这两件事在不同线程：不互斥就会出现「取消写 CANCELLED、执行线程写 RUNNING」
        # 的覆盖，用户看到取消了却照样跑完。
        self._state_lock = threading.RLock()
        # 外部 CLI 与服务端可能是不同进程，不能直接拿到这里的 token。
        # 轮询周期保持短于常见节点耗时；测试可覆盖此私有值加速。
        self._cancel_poll = 0.2

    # ---- 写 ----
    def start_review(self, target: Path | str, *, profile_id: str | None = None,
                     purpose: str | None = None, engine: str = "v1",
                     use_llm: bool = False, timeout: float | None = None,
                     idempotency_key: str | None = None) -> TaskRecord:
        """建任务并**同步**跑完，返回最终记录。

        三个可选能力，缺省全部关闭（= 改造前的行为，一行都不变）：
          `timeout`          秒；超时置位取消令牌并落 FAILED，不让线程无限占着
          `idempotency_key`  同一个键 + 未结束的任务 → 直接返回已有记录，不建新任务
          （取消能力见 `cancel`，异步见 `start_async`）

        为什么保留同步入口：迁移期一次只跑一个任务，把同步路径也改成队列会让
        所有既有调用点被迫处理「还没跑完」的中间态，而它换不来可观测性。
        """
        return self.start_review_with(
            self._runner, target, profile_id=profile_id, purpose=purpose,
            engine=engine, use_llm=use_llm, timeout=timeout,
            idempotency_key=idempotency_key)

    def start_review_with(self, runner: Runner, target: Path | str, *,
                          profile_id: str | None = None,
                          purpose: str | None = None, engine: str = "v1",
                          use_llm: bool = False, timeout: float | None = None,
                          idempotency_key: str | None = None) -> TaskRecord:
        """同 `start_review`，但本次执行用传入的 runner。

        服务端用的是子进程执行器，它的参数（research / 场景 / 格式要求 / 进度文件）
        比 `Runner` 协议里的通用字段多，所以由入口把「这一次怎么跑」封在 runner 里；
        任务层仍只负责建任务、落状态、超时与取消，不 import `review.py` 或 `subprocess`。
        """
        record = self._open(target, profile_id=profile_id, purpose=purpose,
                            engine=engine, status=RunStatus.RUNNING,
                            idempotency_key=idempotency_key)
        return self._execute(record, timeout=timeout, use_llm=use_llm,
                             runner=runner)

    def start_async(self, target: Path | str, *, profile_id: str | None = None,
                    purpose: str | None = None, engine: str = "v1",
                    use_llm: bool = False, timeout: float | None = None,
                    idempotency_key: str | None = None) -> TaskRecord:
        """建任务后**立刻返回**（状态 QUEUED），执行在后台线程里跑。

        QUEUED 是真实状态而非占位：用户能看到「已提交、还没开始」，
        而 RUNNING 只在后台线程真正拿到执行权后才写。
        """
        return self.start_async_with(
            self._runner, target, profile_id=profile_id, purpose=purpose,
            engine=engine, use_llm=use_llm, timeout=timeout,
            idempotency_key=idempotency_key)

    def start_async_with(self, runner: Runner, target: Path | str, *,
                         profile_id: str | None = None,
                         purpose: str | None = None, engine: str = "v1",
                         use_llm: bool = False, timeout: float | None = None,
                         idempotency_key: str | None = None) -> TaskRecord:
        """同 `start_async`，但本次执行用传入的 runner（见 `start_review_with`）。"""
        record = self._open(target, profile_id=profile_id, purpose=purpose,
                            engine=engine, status=RunStatus.QUEUED,
                            idempotency_key=idempotency_key)
        if record.status != RunStatus.QUEUED.value:
            return record          # 幂等命中：已有任务在跑，不再起线程
        future = self._executor().submit(self._async_body_with, runner, record,
                                         timeout, use_llm)
        with self._lock:
            self._futures[record.review_id] = future
        return record

    def _async_body(self, record: TaskRecord, timeout: float | None,
                    use_llm: bool) -> TaskRecord:
        return self._async_body_with(self._runner, record, timeout, use_llm)

    def _async_body_with(self, runner: Runner, record: TaskRecord,
                         timeout: float | None, use_llm: bool) -> TaskRecord:
        # 「入队 → 开跑」与「用户点取消」是两个线程，必须在同一把锁里做状态迁移，
        # 否则 cancel 写下的 CANCELLED 会被这里的 RUNNING 覆盖（实测就是这么发生的：
        # 取消后任务照样跑完并写了报告）。
        with self._state_lock:
            current = self._store.get(record.review_id)
            if current is not None and RunStatus(current.status) is RunStatus.CANCELLED:
                return current
            running = replace(record, status=RunStatus.RUNNING.value,
                              updated_at=_now_iso())
            self._store.save(running)
        try:
            return self._execute(running, timeout=timeout, use_llm=use_llm,
                                 runner=runner)
        finally:
            with self._lock:
                self._futures.pop(record.review_id, None)

    def wait(self, review_id: str, *, timeout: float | None = None) -> TaskRecord:
        """等后台任务落终态（测试与同步接口复用）。

        没有活着的执行者时直接返回当前记录：叫"等"却立刻返回，比抛错有用——
        调用方通常只是想要最新状态。
        """
        with self._lock:
            future = self._futures.get(review_id)
        if future is not None:
            future.result(timeout=timeout)
        return self._require(review_id)

    def cancel(self, review_id: str, *, reason: str = "") -> TaskRecord:
        """取消任务：置位令牌 + 落 CANCELLED。

        语义边界（写清楚，免得以后有人加"强制终止"）：
          * 已 CANCELLED → 原样返回（幂等：用户连点两下不该报错）
          * 已终态（completed / failed / aborted）→ 抛 InvalidTransition
            （改历史结论不是"取消"，那是另一件事）
          * 本进程有令牌时直接置位；外部 CLI 进程只能写这条持久记录，
            执行侧的观察器发现 CANCELLED 后会在节点/阶段边界协作式退出。
        """
        with self._state_lock:
            record = self._require(review_id)
            status = RunStatus(record.status)
            if status is RunStatus.CANCELLED:
                return record
            if status.is_terminal:
                raise InvalidTransition(
                    f"任务 {review_id} 已是终态 {status.value}，不能取消")
            token = self._token_for(review_id, create=False)
            if token is not None:
                token.cancel(reason or "用户取消")
            updated = replace(record, status=RunStatus.CANCELLED.value,
                              error=reason or record.error or "已取消",
                              updated_at=_now_iso())
            self._store.save(updated)
            return updated

    def recover(self, *, has_checkpoint: Callable[[str], bool] | None = None,
                now: datetime.datetime | None = None) -> RecoveryReport:
        """进程重启后的体检：哪些能续、哪些已经废了。

        `has_checkpoint(run_id)` 由入口注入（任务层不 import runtime 的存储实现，
        否则它会连带依赖 state/ 的布局）。不给就一律按「没有检查点」判断——
        保守方向是"别把废任务说成可恢复"，因为一个假的"可恢复"会让用户等下去。
        """
        moment = now or datetime.datetime.now()
        resumable: list[TaskRecord] = []
        orphaned: list[TaskRecord] = []
        for record in self._store.all():
            status = RunStatus(record.status)
            if status.is_terminal or status is RunStatus.AWAITING_DECISION:
                continue
            live = self.is_live(record.review_id)
            has_ckpt = bool(has_checkpoint and has_checkpoint(record.run_id))
            stale = self._is_stale(record, moment)
            if status is RunStatus.QUEUED and not live:
                # 入队但没人接手：重启把队列丢了。重新入队即可，不算废任务。
                if stale:
                    orphaned.append(self._orphan(record, "排队中丢失（进程重启）"))
                else:
                    resumable.append(record)
                continue
            if live:
                continue                      # 本进程正在跑，别碰
            if has_ckpt and not stale:
                resumable.append(record)
            else:
                reason = ("检查点缺失，无法从断点继续" if not has_ckpt
                          else "超过 {} 未更新，执行者已失联".format(self._stale_after))
                orphaned.append(self._orphan(record, reason))
        return RecoveryReport(resumable=tuple(resumable), orphaned=tuple(orphaned))

    # ---- 读 ----
    def get_task(self, review_id: str) -> TaskRecord | None:
        return self._store.get(review_id)

    def list_runs(self, *, limit: int | None = None) -> list[TaskRecord]:
        """按更新时间倒序。同一秒内创建的两条按 review_id 倒序，结果稳定。"""
        records = sorted(self._store.all(),
                         key=lambda r: (r.updated_at, r.review_id),
                         reverse=True)
        return records[:limit] if limit else records

    def is_live(self, review_id: str) -> bool:
        """本进程里这个任务是否还有执行者（令牌未被清理）。"""
        with self._lock:
            token = self._tokens.get(review_id)
            future = self._futures.get(review_id)
        if token is not None and not token.cancelled:
            return True
        return bool(future is not None and not future.done())

    def active_review_ids(self) -> tuple[str, ...]:
        """本进程正在跑的任务（给 server 的并发闸门用，替代内存里的布尔判断）。"""
        return tuple(sorted(rid for rid in set(self._tokens) | set(self._futures)
                            if self.is_live(rid)))

    def token_for(self, review_id: str) -> CancellationToken | None:
        """取现有令牌（不存在则 None）。runner 侧可用它做更细的检查。"""
        return self._token_for(review_id, create=False)

    def mark(self, review_id: str, status: RunStatus, *,
             error: str | None = None) -> TaskRecord:
        """外部状态变更（人工复核完成等）。状态值仍受领域枚举约束。"""
        record = self._require(review_id)
        updated = replace(record, status=status.value,
                          error=error or record.error, updated_at=_now_iso())
        self._store.save(updated)
        return updated

    def close(self) -> None:
        """放掉线程池（测试清理用）。不影响已落盘的任务记录。"""
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # ---- 内部 ----
    def _finish(self, record: TaskRecord, outcome: TaskOutcome) -> TaskRecord:
        """把执行结果落成任务记录。

        `report_path` 用 `outcome.report_path or record.report_path`：
        恢复/重跑时 runner 可能不返回路径（例如它只补完了后半段），
        这时**不能**把已知的报告路径抹成 None——那会让任务看起来"跑完了没报告"。
        """
        done = replace(
            record,
            status=outcome.status.value,
            stage=outcome.stage.value,
            verdict=outcome.verdict,
            findings=outcome.findings,
            evidence=outcome.evidence,
            report_path=outcome.report_path or record.report_path,
            error=outcome.error,
            updated_at=_now_iso(),
        )
        self._store.save(done)
        return done

    def _open(self, target: Path | str, *, profile_id: str | None,
              purpose: str | None, engine: str, status: RunStatus,
              idempotency_key: str | None) -> TaskRecord:
        target_path = Path(target)
        if not target_path.exists():
            raise NotFoundError(f"找不到待审文件：{target_path}")
        if idempotency_key:
            existing = self._find_by_key(idempotency_key)
            if existing is not None and not RunStatus(existing.status).is_terminal:
                return existing
        created = _now_iso()
        review_id = self._allocate(datetime.date.today())
        record = TaskRecord(
            review_id=review_id,
            run_id=format_run_id(review_id, 1),
            status=status.value,
            stage=Stage.INTAKE.value,
            target=self._rel(target_path),
            engine=engine,
            profile_id=profile_id,
            purpose=purpose,
            idempotency_key=idempotency_key,
            created_at=created,
            updated_at=created,
        )
        # 先落状态再执行：进程如果在执行中被杀，至少留下「有这个任务」
        self._store.save(record)
        return record

    def _execute(self, record: TaskRecord, *, timeout: float | None,
                 use_llm: bool, runner: Runner | None = None) -> TaskRecord:
        runner = runner or self._runner
        # 取消可能发生在「记录已建、线程还没开始执行」之间（`start_async` 立刻返回，
        # 用户马上点取消）。此时不能开跑：否则跑完会把 CANCELLED 覆写成 COMPLETED，
        # 用户看到"取消了但又完成了"，而台账里已经多了一份报告。
        persisted = self._store.get(record.review_id)
        if persisted is not None and RunStatus(persisted.status) is RunStatus.CANCELLED:
            return persisted
        token = self._token_for(record.review_id, create=True)
        target_path = self._abs(record.target)
        call = lambda: self._call_runner(runner, record, target_path, token,  # noqa: E731
                                         use_llm)
        cancelled_by_user = False
        stop_watch = threading.Event()
        watcher = None
        if self._runner_accepts_token(runner):
            watcher = threading.Thread(
                target=self._watch_persisted_cancel,
                args=(record.review_id, token, stop_watch),
                name=f"cancel-watch-{record.review_id}",
                daemon=True,
            )
            watcher.start()
        try:
            if timeout is None:
                outcome = call()
            else:
                future = self._executor().submit(call)
                try:
                    outcome = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    # 线程没法硬杀：置位令牌，让它在下一个阶段/节点边界自己退出。
                    # 报告只在 FINALIZE 之后写，所以被取消的运行不会留下半份报告。
                    token.cancel(f"超过 {timeout:g}s 未完成")
                    outcome = TaskOutcome(
                        status=RunStatus.FAILED,
                        stage=Stage(record.stage),
                        error=f"任务超时（{timeout:g}s）")
        except BaseException as exc:                     # noqa: BLE001 - 失败必须留痕
            self._store.save(replace(
                record,
                status=RunStatus.FAILED.value,
                error=f"{type(exc).__name__}: {exc}",
                updated_at=_now_iso()))
            self._forget(record.review_id)
            raise
        finally:
            stop_watch.set()
            if watcher is not None:
                watcher.join(timeout=max(1.0, self._cancel_poll * 2))
            # 记下"跑之前是不是已被取消"，再作废令牌：
            # `is_live()` 靠令牌存在与否判断本进程还有没有执行者，
            # 所以运行一结束就必须清掉，否则恢复逻辑会以为它还在跑。
            cancelled_by_user = token.cancelled and timeout is None
            token.cancel(token.reason)

        current = self._store.get(record.review_id) or record
        if RunStatus(current.status) is RunStatus.CANCELLED:
            # 用户取消已经落了 CANCELLED，不被执行结果覆盖（CANCELLED 是终态）。
            # 若结论恰好已算出，把报告路径补上——取消不等于丢掉已产出的东西。
            if outcome.report_path and not current.report_path:
                current = replace(current, report_path=outcome.report_path,
                                  updated_at=_now_iso())
                self._store.save(current)
            self._forget(record.review_id)
            return current
        if cancelled_by_user and outcome.status is RunStatus.COMPLETED:
            # 取消请求在最后一个节点之后才到：结论已算完，如实记录而不是降级成取消
            outcome = replace(
                outcome,
                error=outcome.error or "运行结束时收到取消请求（结论已产出，按完成记录）")
        record = self._finish(record, outcome)
        self._forget(record.review_id)
        return record

    def _watch_persisted_cancel(self, review_id: str, token: CancellationToken,
                                stop: threading.Event) -> None:
        """把其他进程写下的 CANCELLED 转成本进程的协作式取消令牌。"""
        while not stop.wait(self._cancel_poll):
            try:
                current = self._store.get(review_id)
            except (OSError, ValueError):
                # 原子 replace 期间可能出现瞬时读失败；下一轮继续观察。
                continue
            if current is None or current.status != RunStatus.CANCELLED.value:
                continue
            token.cancel(current.error or "任务已被外部进程取消")
            return

    def _call_runner(self, runner: Runner, record: TaskRecord, target_path: Path,
                     token: CancellationToken, use_llm: bool) -> TaskOutcome:
        kwargs = dict(review_id=record.review_id, profile_id=record.profile_id,
                      purpose=record.purpose, engine=record.engine,
                      use_llm=use_llm)
        if self._runner_accepts_token(runner):
            kwargs["token"] = token
        return runner(target_path, **kwargs)

    def _runner_accepts_token(self, runner: Runner) -> bool:
        """老 runner 不接 `token` 就照旧调用。

        为什么允许两种形状：`Runner` 协议是公开的（CLI / server / 测试各自装配）。
        为了加取消而强制所有实现改签名，等于让**所有**调用点同时升级——
        迁移期最不该做的就是这种"一次改全仓"的改动。新的 runner 接 token 就传，
        不接就退回今天的行为（取消退化为"只在任务状态上体现"）。
        """
        try:
            params = inspect.signature(runner).parameters
        except (TypeError, ValueError):
            return False
        return "token" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())

    def _token_for(self, review_id: str, *, create: bool) -> CancellationToken | None:
        with self._lock:
            token = self._tokens.get(review_id)
            if token is None and create:
                token = CancellationToken()
                self._tokens[review_id] = token
            return token

    def _forget(self, review_id: str) -> None:
        with self._lock:
            self._tokens.pop(review_id, None)

    def _find_by_key(self, key: str) -> TaskRecord | None:
        for record in self._store.all():
            if record.idempotency_key == key:
                return record
        return None

    def _is_stale(self, record: TaskRecord, now: datetime.datetime) -> bool:
        try:
            updated = datetime.datetime.fromisoformat(record.updated_at)
        except (TypeError, ValueError):
            return True
        return (now - updated) > self._stale_after

    def _orphan(self, record: TaskRecord, reason: str) -> TaskRecord:
        updated = replace(record, status=RunStatus.FAILED.value,
                          error=reason, updated_at=_now_iso())
        self._store.save(updated)
        return updated

    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                # 单机一次只跑一个审查；留 2 个位是因为「超时被放弃的线程」
                # 要等它走到下一个阶段边界才退出，那期间不能把新任务堵死。
                self._pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix="review-task")
            return self._pool

    def _require(self, review_id: str) -> TaskRecord:
        record = self._store.get(review_id)
        if record is None:
            raise NotFoundError(f"找不到任务：{review_id}")
        return record

    def _abs(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute() or self._root is None:
            return path if path.is_absolute() else (self._root or Path.cwd()) / path
        return self._root / path

    def _rel(self, path: Path) -> str:
        if self._root is not None:
            try:
                return path.resolve().relative_to(
                    self._root.resolve()).as_posix()
            except ValueError:
                pass
        return path.resolve().as_posix()


# ---------------------------------------------------------------------------
# 给「外部执行器」用的函数（server 的子进程执行就是这一类）
# ---------------------------------------------------------------------------
# 为什么要有它们：`TaskService` 把「建任务 → 调 runner → 落结果」绑在一起，
# 而 server 的执行器是**子进程**，不在这个进程里跑 runner。硬要让 server 用
# TaskService，就得给它塞一个假的 runner（骗人的 API）。这几个函数只操作
# 「存储 + 记录」，谁执行都能用，语义与 `TaskService` 内部完全一致。
def find_active(store: TaskStore, idempotency_key: str) -> TaskRecord | None:
    """按幂等键找**未结束**的任务（跨进程有效：键在记录里）。"""
    if not idempotency_key:
        return None
    for record in store.all():
        if record.idempotency_key != idempotency_key:
            continue
        if not RunStatus(record.status).is_terminal:
            return record
    return None


def active_tasks(store: TaskStore, *, stale_after: datetime.timedelta,
                 now: datetime.datetime | None = None) -> list[TaskRecord]:
    """当前「看起来还在跑」的任务（并发闸门用）。

    超龄的不算：一份 `running` 记录如果没有执行者在推进，它只会永远挡住新任务。
    超龄的判定与 `TaskService.recover` 同源（`updated_at` + `stale_after`）。
    """
    moment = now or datetime.datetime.now()
    live: list[TaskRecord] = []
    for record in store.all():
        if RunStatus(record.status).is_terminal:
            continue
        if _is_stale_record(record, moment, stale_after):
            continue
        live.append(record)
    return live


def finish_task(store: TaskStore, review_id: str,
                outcome: TaskOutcome) -> TaskRecord:
    """把外部执行器的结果落成任务记录（与 `TaskService._finish` 同一套规则）。"""
    record = store.get(review_id)
    if record is None:
        raise NotFoundError(f"找不到任务：{review_id}")
    done = replace(
        record,
        status=outcome.status.value,
        stage=outcome.stage.value,
        verdict=outcome.verdict,
        findings=outcome.findings,
        evidence=outcome.evidence,
        report_path=outcome.report_path or record.report_path,
        error=outcome.error,
        updated_at=_now_iso(),
    )
    store.save(done)
    return done


def mark_task(store: TaskStore, review_id: str, status: RunStatus, *,
              error: str | None = None) -> TaskRecord:
    """状态变更（取消 / 人工复核），走同一套非法值校验。"""
    record = store.get(review_id)
    if record is None:
        raise NotFoundError(f"找不到任务：{review_id}")
    updated = replace(record, status=status.value,
                      error=error or record.error, updated_at=_now_iso())
    store.save(updated)
    return updated


def recover_tasks(store: TaskStore, *, has_checkpoint: Callable[[str], bool] | None = None,
                  stale_after: datetime.timedelta = datetime.timedelta(hours=1),
                  now: datetime.datetime | None = None) -> RecoveryReport:
    """与 `TaskService.recover` 同一套判断，供不需要 runner 的调用方使用。"""
    moment = now or datetime.datetime.now()
    resumable: list[TaskRecord] = []
    orphaned: list[TaskRecord] = []
    for record in store.all():
        status = RunStatus(record.status)
        if status.is_terminal or status is RunStatus.AWAITING_DECISION:
            continue
        has_ckpt = bool(has_checkpoint and has_checkpoint(record.run_id))
        stale = _is_stale_record(record, moment, stale_after)
        if status is RunStatus.QUEUED and stale:
            orphaned.append(_orphan_record(store, record, "排队中丢失（进程重启）"))
        elif status is RunStatus.QUEUED:
            resumable.append(record)
        elif has_ckpt and not stale:
            resumable.append(record)
        else:
            reason = ("检查点缺失，无法从断点继续" if not has_ckpt
                      else f"超过 {stale_after} 未更新，执行者已失联")
            orphaned.append(_orphan_record(store, record, reason))
    return RecoveryReport(resumable=tuple(resumable), orphaned=tuple(orphaned))


def _is_stale_record(record: TaskRecord, now: datetime.datetime,
                     stale_after: datetime.timedelta) -> bool:
    try:
        updated = datetime.datetime.fromisoformat(record.updated_at)
    except (TypeError, ValueError):
        return True
    return (now - updated) > stale_after


def _orphan_record(store: TaskStore, record: TaskRecord, reason: str) -> TaskRecord:
    updated = replace(record, status=RunStatus.FAILED.value,
                      error=reason, updated_at=_now_iso())
    store.save(updated)
    return updated
