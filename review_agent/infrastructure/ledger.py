# -*- coding: utf-8 -*-
"""台账（ledger）：哈希链 + 追加式墓碑 + 跨进程写锁。

台账是「这次审查产出过什么」的唯一凭据：每一行都接在上一行的 `report_hash` 后面，
所以删改任何一条都会让后续所有行对不上。基于这个性质，**删除报告不能改台账**，
只能追加一条墓碑（tombstone）说明「第 N 条对应的报告被谁在什么时候删了」。

旧实现（`server.py` 的 `_mark_ledger_deleted`）做的是后者的事、用前者的手法：
把整个 `chain.log` 读进内存、把某一行改成 `op=delete`、再 `os.replace` 整文件写回。
语义没错（`seq` / `report_hash` / `prev_hash` 都没动，链依然有效），但机制有三个洞：

    1. 不持锁。窗口期内若有审查进程在锁内追加，新行会被整文件重写**静默吞掉**；
       而 `seq` 是按「行数 + 1」推的，吞一行就意味着后续所有 seq 重复、链断。
    2. 临时文件名固定（`chain.tmp`），两个并发删除互相覆盖。
    3. Windows 上 `os.replace` 撞上别人正 append 打开的文件会 PermissionError，
       而调用方（`do_DELETE`）没有 try/except，用户看到的是连接被掐。

本模块把「写」收敛成两种原子操作，且都要求持同一把锁：

    append(path, entry)               追加一条报告记录（自动取 prev_hash / seq）
    tombstone(path, target_seq, ...)  追加一条墓碑行（**不碰**任何已有行）

行有两种形态，靠字段区分，不用 `op` 判断（老数据里 `op=delete` 是**原位**墓碑，
它自己就是一条报告记录，不能当成独立墓碑）：

    报告记录：有 `report_hash`（可能带 `op=delete`，那是旧版原位墓碑）
    独立墓碑：有 `target_seq`，没有 `report_hash`

这样 2026-10-31 之前产生的原位墓碑行继续可校验（`legacy_tombstone`），
新删除则一律追加，两种形态在 `verify()` 里都能过。
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

LOCK_NAME = ".chain.lock"
# 进程被强杀会留下锁文件：超龄视为陈旧，直接清掉，不能永久堵死发号
STALE_LOCK_SECONDS = 60.0
DEFAULT_LOCK_TIMEOUT = 30.0


class LedgerError(RuntimeError):
    """台账写入/校验失败（链会断、参数不对、锁超时都归这里）。"""


def now_iso() -> str:
    """本地时间、秒级。与既有台账行的 `ts` 格式一致（可直接字符串排序）。"""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 写锁（O_EXCL 锁文件；进程内线程与多进程共用一套）
# ---------------------------------------------------------------------------
def acquire_lock(ledger_dir: Path | str, *, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Path:
    """拿台账写锁，返回锁文件路径。

    为什么必须锁：追加一条台账 = 「读末行取 prev_hash → 算 report_hash → 追加」。
    这三步不是原子的，两个审查同时收尾会读到同一个 prev，写出两条同号记录，
    链条当场断裂（实测 6 路并发 → `序号不连续 @line2`），而**报告本身看起来都正常**。

    两种异常都按争用处理，不按「目录不可写」处理：
        FileExistsError   锁文件已存在
        PermissionError   Windows 上锁文件被别人打开时，O_EXCL 抛的往往是它
    先判断「锁还在不在」再决定要不要等是错的：持有者可能在两次系统调用之间就删了锁。
    """
    ledger_dir = Path(ledger_dir)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    lock = ledger_dir / LOCK_NAME
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return lock
        except (FileExistsError, PermissionError):
            _wait_for_lock(lock, deadline, timeout)


def _wait_for_lock(lock: Path, deadline: float, timeout: float) -> None:
    try:
        if time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
            lock.unlink()
            return
    except OSError:
        pass
    if time.time() > deadline:
        raise LedgerError(
            f"等待台账写锁超时（{timeout:.0f}s）：{lock}；"
            "如确认没有审查在跑，可手工删除该锁文件")
    time.sleep(0.01)


def release_lock(lock: Path | str) -> None:
    try:
        Path(lock).unlink()
    except OSError:
        pass


@contextlib.contextmanager
def ledger_lock(ledger_dir: Path | str, *,
                timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[Path]:
    """`with ledger_lock(LEDGER):` —— 异常也要放锁，否则一次崩就把台账锁死。"""
    lock = acquire_lock(ledger_dir, timeout=timeout)
    try:
        yield lock
    finally:
        release_lock(lock)


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def parse_line(raw: str) -> dict[str, Any] | None:
    """一行 → dict；空行返回 None。坏 JSON 抛 ValueError（**不静默跳过**掉链证据）。"""
    text = raw.strip()
    if not text:
        return None
    return json.loads(text)


def read_records(path: Path | str) -> list[dict[str, Any]]:
    """读全部行（含墓碑）。坏行抛 `LedgerError`——链断这件事不能靠"跳过"掩盖。

    要的是"能不能恢复/能不能证明"，所以这里选择硬失败；需要宽容读的场合
    用 `scan()`（把坏行数一并返回，交给调用方决定怎么展示）。
    """
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            rec = parse_line(raw)
        except ValueError as exc:
            raise LedgerError(f"{path.name} 第 {lineno} 行不是合法 JSON：{exc}") from exc
        if rec is not None:
            out.append(rec)
    return out


def scan(path: Path | str) -> tuple[list[dict[str, Any]], int]:
    """宽容读：返回 (记录, 坏行数)。坏行让调用方在界面上标红，而不是让接口 500。"""
    path = Path(path)
    if not path.exists():
        return [], 0
    records: list[dict[str, Any]] = []
    bad = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = parse_line(raw)
        except ValueError:
            bad += 1
            continue
        if rec is not None:
            records.append(rec)
    return records, bad


def is_tombstone(record: Mapping[str, Any]) -> bool:
    """独立墓碑行：指向某个 seq，自己不在链上。"""
    return "target_seq" in record and "report_hash" not in record


def is_report(record: Mapping[str, Any]) -> bool:
    return not is_tombstone(record)


def report_records(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """链上的记录（旧版原位墓碑 `op=delete` 也算，它保留了 seq/hash）。"""
    return [r for r in records if is_report(r)]


def tombstones(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [r for r in records if is_tombstone(r)]


def chain_state(path: Path | str) -> tuple[str, int]:
    """返回 (末条 report_hash, 下一条 seq)。没有记录时 = (全 0, 1)。

    seq 只数报告记录：墓碑占用老编号（`target_seq`），不占新号——
    否则「第 8 条被删」会让下一条报告变成第 9 条，历史报告的 seq 对不上。
    """
    reports = report_records(read_records(path))
    if not reports:
        return "0" * 64, 1
    last = reports[-1]
    seq = last.get("seq")
    next_seq = (int(seq) + 1) if isinstance(seq, int) else len(reports) + 1
    return str(last.get("report_hash") or "0" * 64), next_seq


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def append(path: Path | str, entry: Mapping[str, Any], *,
           locked: bool = False,
           expect_prev: str | None = None) -> dict[str, Any]:
    """追加一条报告记录，返回落盘后的记录。

    `locked=True` 表示调用方已持锁（例如它还要在锁内算 `report_hash`）。
    本函数会在锁内**重新核对** `prev_hash`：对不上就抛错，而不是写出一条断链的行。
    这条自检专抓"忘了拿锁"和"拿着锁却读错了位置"两类 bug。

    `expect_prev`：调用方先前读到的 prev。给了就要求与当前末条一致，
    不一致说明期间有别人写过（链会从调用方那一份算出的 hash 断开）。
    """
    path = Path(path)
    if locked:
        return _append_unlocked(path, entry, expect_prev=expect_prev)
    with ledger_lock(path.parent):
        return _append_unlocked(path, entry, expect_prev=expect_prev)


def _append_unlocked(path: Path, entry: Mapping[str, Any],
                     *, expect_prev: str | None) -> dict[str, Any]:
    prev, next_seq = chain_state(path)
    if expect_prev is not None and expect_prev != prev:
        raise LedgerError(
            f"台账末条已变化（期望 prev={expect_prev[:12]}…，实际 {prev[:12]}…）："
            "本次的 report_hash 是照着旧 prev 算的，写入会断链，已拒绝")
    payload = dict(entry)
    payload.setdefault("seq", next_seq)
    payload.setdefault("prev_hash", prev)
    payload.setdefault("ts", now_iso())
    if int(payload["seq"]) != next_seq:
        raise LedgerError(
            f"seq 不连续：应为 {next_seq}，给了 {payload['seq']}（会覆盖历史编号）")
    if str(payload["prev_hash"]) != prev:
        raise LedgerError(
            f"prev_hash 与末条不符（应为 {prev[:12]}…）")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return dict(payload)


def tombstone(path: Path | str, target_seq: int, *,
              actor: str = "web", reason: str = "",
              ts: str | None = None,
              timeout: float = DEFAULT_LOCK_TIMEOUT) -> dict[str, Any]:
    """追加一条墓碑行，**不改任何已有行**。

    `target_seq` 必须指向一条真实存在的报告记录：删一个不存在的号，
    要么是序号算错，要么是有人手改了台账——两种都该报错。
    """
    path = Path(path)
    target = int(target_seq)
    with ledger_lock(path.parent, timeout=timeout):
        records = read_records(path)
        seqs = {r.get("seq") for r in report_records(records)}
        if target not in seqs:
            raise LedgerError(
                f"墓碑指向不存在的序号 {target}（台账现有 {len(seqs)} 条报告记录）")
        latest = _latest_marker(records, target)
        if latest is not None and latest.get("op") == "delete":
            raise LedgerError(f"seq {target} 已有墓碑，重复删除不该再追加一次")
        record = {
            "op": "delete",
            "target_seq": target,
            "actor": str(actor or "web"),
            "reason": str(reason or ""),
            "ts": ts or now_iso(),
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return record


def restore(path: Path | str, target_seq: int, *,
            actor: str = "web", reason: str = "",
            ts: str | None = None,
            timeout: float = DEFAULT_LOCK_TIMEOUT) -> dict[str, Any]:
    """追加一条恢复事件，不改链上任何报告记录。

    删除与恢复都必须是**追加事件**：恢复报告时如果删掉墓碑，原先的删除痕迹
    就消失了；如果重写台账，后续整条链都会失去校验意义。最新事件决定当前
    状态，因此“删除 → 恢复 → 再删除”仍然可以完整审计。
    """
    path = Path(path)
    target = int(target_seq)
    with ledger_lock(path.parent, timeout=timeout):
        records = read_records(path)
        seqs = {r.get("seq") for r in report_records(records)}
        if target not in seqs:
            raise LedgerError(
                f"恢复事件指向不存在的序号 {target}（台账现有 {len(seqs)} 条报告记录）")
        latest = _latest_marker(records, target)
        if latest is None or latest.get("op") != "delete":
            raise LedgerError(f"seq {target} 当前不在删除状态，不能恢复")
        record = {
            "op": "restore",
            "target_seq": target,
            "actor": str(actor or "web"),
            "reason": str(reason or ""),
            "ts": ts or now_iso(),
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return record


def _latest_marker(records: Sequence[Mapping[str, Any]],
                   target_seq: int) -> Mapping[str, Any] | None:
    """返回目标报告最新的删除/恢复事件；历史原位墓碑也算初始删除。"""
    latest: Mapping[str, Any] | None = None
    for rec in records:
        if is_tombstone(rec):
            target = int(rec.get("target_seq") or -1)
        elif rec.get("op") == "delete":
            target = int(rec.get("seq") or -1)
        else:
            continue
        if target == int(target_seq):
            latest = rec
    return latest


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def verify(path: Path | str) -> tuple[bool, str, dict[str, Any]]:
    """校验一条 ledger：链完整 + 序号连续 + 墓碑指向有效。

    返回 (ok, 一句话结论, 统计)。结论文案与旧版保持同一句式
    （`ledger 校验通过，共 N 条记录，链完整`），只在不含墓碑时完全一致，
    含墓碑时追加 `（含 M 条墓碑）`——旧调用方按 `ok` 判断，不解析这句话。
    """
    path = Path(path)
    stats: dict[str, Any] = {"records": 0, "tombstones": 0,
                             "legacy_tombstones": 0, "restores": 0,
                             "events": 0, "deleted": [], "restored": []}
    if not path.exists():
        return False, "ledger 不存在", stats
    records = read_records(path)
    prev = "0" * 64
    expected_seq = 1
    states: dict[int, str] = {}
    for rec in records:
        if is_tombstone(rec):
            op = rec.get("op")
            if op not in ("delete", "restore"):
                return (False, f"未知台账事件 {op!r}", stats)
            target = int(rec.get("target_seq") or -1)
            if op == "delete":
                stats["tombstones"] += 1
            else:
                if states.get(target) != "delete":
                    return (False, f"恢复事件没有对应的删除记录 @seq{target}", stats)
                stats["restores"] += 1
            states[target] = op
            stats["events"] += 1
            continue
        seq = rec.get("seq")
        if seq != expected_seq:
            return (False, f"序号不连续 @line{expected_seq}", stats)
        if rec.get("prev_hash") != prev:
            return (False, f"链条断裂 @seq{seq}: prev_hash 不匹配", stats)
        if rec.get("op") == "delete":
            stats["legacy_tombstones"] += 1
            states[int(seq)] = "delete"
        prev = str(rec.get("report_hash") or "")
        expected_seq += 1
    stats["records"] = expected_seq - 1
    for target in states:
        if not 1 <= target <= stats["records"]:
            return (False, f"删除/恢复事件指向不存在的序号 {target}", stats)
    stats["deleted"] = sorted(seq for seq, op in states.items() if op == "delete")
    stats["restored"] = sorted(seq for seq, op in states.items() if op == "restore")
    message = f"ledger 校验通过，共 {stats['records']} 条记录"
    marks_total = (stats["tombstones"] + stats["legacy_tombstones"]
                   + stats["restores"])
    if marks_total:
        if stats["restores"]:
            message += f"（含 {marks_total} 条删除/恢复事件）"
        else:
            # 两种墓碑都要报出来：只报独立行会让"原位墓碑 63 条"看起来像没删过东西
            message += f"（含 {marks_total} 条墓碑）"
    return True, message + "，链完整", stats


def entries_for_api(path: Path | str) -> dict[str, Any]:
    """给 `/api/integrity` 用的视图：链上条目 + 已删除标记。

    与旧实现的差别只有一个：墓碑不再被当成"一行坏数据"（它以前会让 `chain_ok`
    变成 False，于是删过报告的用户永远看到红叉）。墓碑现在只用来给目标行打
    `deleted` 标记，并单独列出。
    """
    path = Path(path)
    records, bad = scan(path)
    applied: dict[int, Mapping[str, Any]] = {}
    marks: list[Mapping[str, Any]] = []
    for rec in records:
        if is_tombstone(rec):
            marks.append(rec)
            applied[int(rec.get("target_seq") or -1)] = rec
    entries: list[dict[str, Any]] = []
    chain_ok = bad == 0
    broken_at: int | None = None if chain_ok else 1
    prev = "0" * 64
    expected_seq = 1
    for rec in records:
        if is_tombstone(rec):
            continue
        row_ok = rec.get("seq") == expected_seq and rec.get("prev_hash") == prev
        if not row_ok:
            chain_ok = False
            if broken_at is None:
                broken_at = expected_seq
        seq = rec.get("seq")
        mark = applied.get(int(seq)) if isinstance(seq, int) else None
        legacy = rec.get("op") == "delete"
        latest = mark or (rec if legacy else None)
        deleted = bool(latest) and latest.get("op") == "delete"
        restored = bool(mark) and mark.get("op") == "restore"
        entries.append({
            "seq": seq,
            "review_id": rec.get("review_id"),
            "report_hash": rec.get("report_hash", ""),
            "report_hash_short": (rec.get("report_hash") or "")[:12],
            "prev_hash": rec.get("prev_hash", ""),
            "prev_hash_short": (rec.get("prev_hash") or "")[:12],
            "ts": rec.get("ts", ""),
            "ok": row_ok,
            # 最新事件决定当前状态；历史原位墓碑作为没有独立事件时的初始状态。
            "op": (latest or {}).get("op") or "report",
            "deleted": deleted,
            "restored": restored,
            "deleted_at": ((mark or {}).get("ts") if mark
                           else rec.get("deleted_at")) if deleted else None,
            "delete_reason": ((mark or {}).get("reason") if mark
                              else rec.get("delete_reason")) if deleted else None,
            "restored_at": mark.get("ts") if restored else None,
            "restore_reason": mark.get("reason") if restored else None,
            "legacy_tombstone": legacy and not mark,
        })
        prev = str(rec.get("report_hash") or "")
        expected_seq += 1
    return {
        "entries": entries,
        "tombstones": [dict(t) for t in marks],
        "chain_ok": chain_ok,
        "broken_at": broken_at,
        "bad_lines": bad,
        # 与 `verify()` 的口径一致：两种墓碑（独立行 + 历史原位行）都算"已删除"
        "deleted": sorted(e["seq"] for e in entries if e["deleted"]),
    }
