# -*- coding: utf-8 -*-
"""保留期：把「删哪些运行数据」变成一份可审的计划，而不是一句 `del`。

为什么要单独一个模块：
    审查系统的运行产物里**每一类都带原文或原文片段**——报告里有 `target_text`，
    检查点里有 `artifacts`，`cache/llm/` 里有模型返回的引用，`gap_log` 里有缺口句。
    这些东西没有保留期就意味着「用户以为删了、其实永远在」。而清理逻辑一旦写错，
    删掉的可能是**审计凭据**（台账）或**还没跑完的任务状态**。

所以本模块只做两件事，且两件事分开：

    plan(...)   纯函数：算出「按配置该删哪些」，不改磁盘
    apply(...)  执行计划：默认 dry-run，只有显式 apply 才真删

三条硬规则（都有用例守着）：
    1. **台账永不删**（`ledger/` 整个目录不在任何类别里）：它是「发生过什么」的凭据；
    2. **未结束的任务永不删**（`state/tasks/` 里非终态记录一律跳过）：删了就没有
       「这个号存在过」的证据，恢复逻辑也会把可续任务判成废任务；
    3. **只删过期的**：按文件 mtime 与配置的天数比较，不吃「全清」这种选项。

配置（`config/retention.yaml`，缺省值写在本文件里，缺文件也能跑）：

    outputs: 365        # 正式报告（含原文）—— 删了等于删结论，默认一年
    trash: 30           # 回收站
    logs: 180           # 每次审查的日志目录
    runs: 90            # 检查点状态（可续跑用；删了就只能整单重跑）
    text: 90            # 原文快照（state/text/<sha256>.txt）：按内容寻址，多份报告共用
    tasks: 365          # 任务记录（非终态永不删）
    jobs: 7             # 进度事件文件
    cache: 30           # 模型缓存 / 检索缓存 / 双跑差异
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DAYS: Mapping[str, int] = {
    "outputs": 365,
    "trash": 30,
    "logs": 180,
    "runs": 90,
    "text": 90,
    "tasks": 365,
    "jobs": 7,
    "cache": 30,
}

CONFIG_NAME = "config/retention.yaml"


@dataclass(frozen=True, slots=True)
class PurgeItem:
    """一条待删项。`reason` 会原样出现在 dry-run 输出里——计划必须能被人读懂。"""

    path: Path
    category: str
    age_days: int
    reason: str
    is_dir: bool = False
    keep: bool = False

    def as_dict(self) -> dict:
        return {
            "path": str(self.path).replace("\\", "/"),
            "category": self.category,
            "age_days": self.age_days,
            "reason": self.reason,
            "is_dir": self.is_dir,
            "keep": self.keep,
        }


@dataclass(frozen=True, slots=True)
class PurgePlan:
    items: tuple[PurgeItem, ...] = ()
    skipped: tuple[PurgeItem, ...] = ()
    config: Mapping[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def bytes(self) -> int:
        total = 0
        for item in self.items:
            try:
                if item.is_dir:
                    total += sum(p.stat().st_size for p in item.path.rglob("*")
                                 if p.is_file())
                else:
                    total += item.path.stat().st_size
            except OSError:
                continue
        return total

    def by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.category] = counts.get(item.category, 0) + 1
        return counts


def load_config(path: Path | str | None = None) -> dict[str, int]:
    """读 `config/retention.yaml`（缺文件/坏文件都退回代码内缺省）。

    为什么坏文件不报错：保留期是**清理**，配置坏了应当"少删"而不是"不跑"。
    返回值只包含认识的键，且天数必须是非负整数（0 = 只删今天的，负值视为 0）。
    """
    config = dict(DEFAULT_DAYS)
    if path is None:
        return config
    path = Path(path)
    if not path.is_file():
        return config
    try:
        import yaml                                   # 运行期唯一第三方依赖
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:                                 # noqa: BLE001 - 配置坏了不该崩
        return config
    if not isinstance(data, dict):
        return config
    for key, raw in data.items():
        if key not in DEFAULT_DAYS:
            continue
        try:
            config[str(key)] = max(0, int(raw))
        except (TypeError, ValueError):
            continue
    return config


def _age_days(path: Path, now: datetime.datetime) -> int:
    """这个路径「有多旧」。

    目录取**内部最新文件**的年龄，而不是目录自身的 mtime：往目录里放一个文件就会
    刷新目录 mtime，用它只会得出「这个日志目录昨天刚动过」——而实际是三个月前的
    审查目录里多了一行日志。清理工具宁可不删，也不能因为一个 mtime 就让整套日志
    永远留在磁盘上。
    """
    if path.is_dir():
        ages: list[int] = []
        try:
            for child in path.rglob("*"):
                if child.is_file():
                    ages.append(_age_days(child, now))
        except OSError:
            ages = []
        return min(ages) if ages else _mtime_age(path, now)
    return _mtime_age(path, now)


def _mtime_age(path: Path, now: datetime.datetime) -> int:
    try:
        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return 0
    return max(0, int((now - mtime).total_seconds() // 86400))


def _iter_files(root: Path, pattern: str) -> Iterator[Path]:
    if not root.is_dir():
        return
    yield from sorted(root.glob(pattern))


def _claimable_task(path: Path) -> bool:
    """任务记录只有「终态」才可以进清理计划。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return str(data.get("status") or "") in {"completed", "failed", "aborted",
                                             "cancelled"}


def plan(root: Path | str, *, config: Mapping[str, int] | None = None,
         now: datetime.datetime | None = None) -> PurgePlan:
    """算出该删什么（纯函数：不删任何东西）。

    `root` 是项目根；依次看 outputs / outputs/.trash / logs / logs/.jobs /
    state/runs / state/tasks / cache。**不看 `ledger/`**——它不在任何类别里。
    """
    root = Path(root)
    cfg = dict(DEFAULT_DAYS)
    if config:
        cfg.update({k: int(v) for k, v in config.items() if k in DEFAULT_DAYS})
    moment = now or datetime.datetime.now()
    items: list[PurgeItem] = []
    skipped: list[PurgeItem] = []

    def consider(path: Path, category: str, reason: str, *,
                 is_dir: bool = False, guard=None) -> None:
        if not path.exists():
            return
        age = _age_days(path, moment)
        limit = cfg.get(category, 0)
        if guard is not None and not guard(path):
            skipped.append(PurgeItem(path, category, age,
                                     "未结束的任务记录，不参与清理",
                                     is_dir=is_dir, keep=True))
            return
        if age >= limit:
            items.append(PurgeItem(path, category, age, reason, is_dir=is_dir))
        else:
            skipped.append(PurgeItem(path, category, age,
                                     f"未到期（{age} < {limit} 天）",
                                     is_dir=is_dir, keep=True))

    # 正式报告：删了等于删结论，所以期限最长（默认 365 天）
    for path in _iter_files(root / "outputs", "R-*.json"):
        consider(path, "outputs", "超过保留期的正式报告（JSON）")
    for path in _iter_files(root / "outputs", "R-*.md"):
        consider(path, "outputs", "超过保留期的正式报告（Markdown）")
    for path in _iter_files(root / "outputs" / "v1", "*"):
        if path.is_file():
            consider(path, "outputs", "超过保留期的影子报告（v1）")

    # 回收站：删报告时搬进来的副本，过期即真删
    trash = root / "outputs" / ".trash"
    if trash.is_dir():
        for path in sorted(trash.iterdir()):
            consider(path, "trash", "回收站内超过保留期", is_dir=path.is_dir())

    # 每次审查的日志目录
    log_root = root / "logs"
    if log_root.is_dir():
        for path in sorted(log_root.iterdir()):
            if path.is_dir() and path.name.startswith("R-"):
                consider(path, "logs", "超过保留期的审查日志目录", is_dir=True)

    for path in _iter_files(root / "logs" / ".jobs", "*.jsonl"):
        consider(path, "jobs", "超过保留期的进度事件文件")

    for path in _iter_files(root / "state" / "runs", "*.state.json"):
        consider(path, "runs", "超过保留期的检查点状态（之后只能整单重跑）")

    # 原文快照（批次 3 / B1）：报告里的 `target.snapshot_ref` 指向这里。
    # 按内容寻址 → 同一份材料的多份报告共用一份快照，所以它只能按"文件年龄"
    # 清理，**不能**跟着某份报告一起删（删了别的报告就复核不了 quote）。
    for path in _iter_files(root / "state" / "text", "*.txt"):
        consider(path, "text",
                 "超过保留期的原文快照（对应报告仍可用自身 target_text 复核）")

    for path in _iter_files(root / "state" / "tasks", "R-*.json"):
        consider(path, "tasks", "超过保留期的任务记录",
                 guard=_claimable_task)

    for sub in ("cache/llm", "state/research_cache", "state/double_run",
                "state/shadow", "state/gap_log.jsonl"):
        target = root / sub
        if not target.exists():
            continue
        if target.is_dir():
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    consider(path, "cache", f"{sub} 超过保留期")
        else:
            consider(target, "cache", f"{sub} 超过保留期")

    return PurgePlan(items=tuple(items), skipped=tuple(skipped), config=cfg)


def apply(plan_obj: PurgePlan, *, dry_run: bool = True,
          log: Callable[[str], None] | None = None) -> dict[str, int]:
    """执行计划。**默认 dry-run**：调用方必须显式 `dry_run=False` 才真删。"""
    removed = 0
    failed = 0
    say = log or (lambda msg: None)
    for item in plan_obj.items:
        if dry_run:
            say(f"[dry-run] 将删除 {item.category}: {item.path}（{item.age_days} 天）")
            continue
        try:
            if item.is_dir:
                shutil.rmtree(item.path)
            else:
                item.path.unlink()
            removed += 1
            say(f"[purge] 已删除 {item.category}: {item.path}")
        except OSError as exc:
            failed += 1
            say(f"[purge] 删除失败 {item.path}：{exc}")
    return {"removed": removed, "failed": failed,
            "planned": plan_obj.total, "bytes": plan_obj.bytes}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="运行数据保留期：默认只打印计划，--apply 才真删")
    parser.add_argument("--root", default=None, help="项目根（缺省按本文件位置推）")
    parser.add_argument("--config", default=None, help="保留期配置（缺省 config/retention.yaml）")
    parser.add_argument("--apply", action="store_true", help="真的删除（默认 dry-run）")
    parser.add_argument("--json", action="store_true", help="输出计划 JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[2]
    cfg_path = Path(args.config) if args.config else root / CONFIG_NAME
    cfg = load_config(cfg_path)
    result = plan(root, config=cfg)
    if args.json:
        print(json.dumps({
            "config": result.config,
            "total": result.total,
            "bytes": result.bytes,
            "by_category": result.by_category(),
            "items": [i.as_dict() for i in result.items],
            "skipped": len(result.skipped),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"[retention] 配置：{cfg}（来源 {cfg_path}）")
        print(f"[retention] 计划删除 {result.total} 项，"
              f"约 {result.bytes / 1024:.1f} KiB；保留 {len(result.skipped)} 项")
        for category, count in sorted(result.by_category().items()):
            print(f"  - {category}: {count} 项")
        for item in result.items[:20]:
            print(f"      {item.category}/{item.path.name}（{item.age_days} 天）")
        if result.total > 20:
            print(f"      …另有 {result.total - 20} 项")
    outcome = apply(result, dry_run=not args.apply,
                    log=lambda msg: print(msg) if args.apply else None)
    if args.apply:
        print(f"[retention] 已删除 {outcome['removed']} 项，失败 {outcome['failed']} 项")
    else:
        print("[retention] 这是 dry-run；确认无误后加 --apply 执行")
    return 0 if outcome["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
