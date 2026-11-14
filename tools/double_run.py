#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""double_run.py —— 双跑观测：切流前唯一靠得住的证据。

为什么需要它：`--engine shadow` 每跑一次就往 `state/double_run/<review_id>.json`
写一份差异，但**没有人会把 20 份 JSON 一份份点开看**。切流的判据是
「连续 N 天零未解释差异」，而这条判据只有能被一条命令回答时才成立。

判据口径（与 REFACTOR_PLAN 阶段 2 一致，这里不另立标准）：
    * `diff.unexplained` —— 新流水线报了、旧报告没有（也不是折叠）→ **新实现的 bug**
    * `diff.missing`     —— 旧报告报了、新流水线没有 → **新实现漏了**
    * verdict 不一致     —— 同一份材料两个裁定

用法：
    python tools/double_run.py                     # 全量汇总，有未解释差异则退出 1
    python tools/double_run.py --since 2026-11-01  # 只看某天之后
    python tools/double_run.py --require 20 --days 7  # 代表样本/连续天数不足都不通过
    python tools/double_run.py --json              # 给脚本/看板用
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOUBLE_RUN_DIR = ROOT / "state" / "double_run"


def load_payloads(root: Path | str | None = None) -> list[dict]:
    """读全部双跑差异文件。坏文件跳过——一份坏文件不该让观测整体失败。"""
    base = Path(root) if root is not None else DOUBLE_RUN_DIR
    if not base.is_dir():
        return []
    out = []
    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data.setdefault("_path", path.name)
            out.append(data)
    return out


def _review_date(payload: dict) -> str:
    """从 review_id（R-YYYYMMDD-NNN）取日期，取不到就空串。"""
    rid = str(payload.get("review_id") or "")
    parts = rid.split("-")
    if len(parts) >= 2 and parts[1].isdigit() and len(parts[1]) == 8:
        raw = parts[1]
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return ""


def _sample_key(payload: dict) -> str:
    """代表性样本身份：目标 + Profile + 模型；老数据缺元信息时按 review 保底。"""
    sample = payload.get("sample") or {}
    state = payload.get("state") or {}
    artifacts = state.get("artifacts") or {}
    profile = state.get("profile") or {}
    target_hash = (sample.get("target_hash")
                   or artifacts.get("target_hash")
                   or payload.get("target_hash"))
    profile_id = sample.get("profile_id") or profile.get("id") or ""
    profile_version = (sample.get("profile_version")
                       or profile.get("version") or "")
    model = sample.get("model_version") or payload.get("model_version") or ""
    if target_hash:
        return f"{target_hash}|{profile_id}|{profile_version}|{model}"
    return f"legacy:{payload.get('review_id') or payload.get('_path') or ''}"


def _max_consecutive_days(dates: list[str]) -> int:
    parsed = sorted({
        datetime.date.fromisoformat(value) for value in dates if value
    })
    best = current = 0
    previous = None
    for day in parsed:
        current = current + 1 if previous and (day - previous).days == 1 else 1
        best = max(best, current)
        previous = day
    return best


def summarize(payloads: list[dict], *, since: str | None = None) -> dict:
    """把一堆双跑结果折成一份结论（纯函数，便于单测）。"""
    rows = []
    for payload in payloads:
        date = _review_date(payload)
        if since and date and date < since:
            continue
        diff = payload.get("diff") or {}
        rows.append({
            "review_id": payload.get("review_id"),
            "date": date,
            "status": payload.get("status"),
            "ok": bool(diff.get("ok")),
            "unexplained": len(diff.get("unexplained") or ()),
            "missing": len(diff.get("missing") or ()),
            "folded": len(diff.get("folded") or ()),
            "verdict_new": diff.get("verdict_new"),
            "verdict_legacy": diff.get("verdict_legacy"),
            "errors": list(payload.get("errors") or ()),
        })
    unexplained = sum(r["unexplained"] for r in rows)
    missing = sum(r["missing"] for r in rows)
    verdict_conflict = sum(
        1 for r in rows if r["verdict_new"] != r["verdict_legacy"])
    failed = [r for r in rows if not r["ok"]]
    filtered = [
        payload for payload in payloads
        if not since or not _review_date(payload)
        or _review_date(payload) >= since
    ]
    dates = [_review_date(payload) for payload in filtered]
    unique_dates = {date for date in dates if date}
    return {
        "samples": len(rows),
        "unique_samples": len({_sample_key(payload) for payload in filtered}),
        "observed_days": len(unique_dates),
        "continuous_days": _max_consecutive_days(dates),
        "ok_samples": len(rows) - len(failed),
        "failed_samples": len(failed),
        "unexplained": unexplained,
        "missing": missing,
        "verdict_conflict": verdict_conflict,
        "folded": sum(r["folded"] for r in rows),
        "first_date": min((r["date"] for r in rows if r["date"]), default=""),
        "last_date": max((r["date"] for r in rows if r["date"]), default=""),
        "failed": failed,
        "rows": rows,
    }


def check(summary: dict, *, require: int = 0, days: int = 0
          ) -> tuple[bool, list[str]]:
    """判据：差异归零 + 代表性样本够 + 连续观测天数够。"""
    reasons = []
    if summary["unexplained"]:
        reasons.append(f"未解释差异 {summary['unexplained']} 条"
                       "（新流水线报了、旧报告没有——按新实现 bug 处理）")
    if summary["missing"]:
        reasons.append(f"缺失 {summary['missing']} 条"
                       "（旧报告报了、新流水线没有）")
    if summary["verdict_conflict"]:
        reasons.append(f"裁定不一致 {summary['verdict_conflict']} 份")
    if require and summary["unique_samples"] < require:
        reasons.append(
            f"代表性样本不足（{summary['unique_samples']} < {require}）")
    if days and summary["continuous_days"] < days:
        reasons.append(
            f"连续观察不足（{summary['continuous_days']} < {days} 天）")
    return (not reasons), reasons


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="双跑差异汇总与切流判据")
    parser.add_argument("--root", default=None, help="项目根（缺省按本文件位置推）")
    parser.add_argument("--since", default=None, help="只看该日期之后（YYYY-MM-DD）")
    parser.add_argument("--require", type=int, default=0, help="要求至少多少份样本")
    parser.add_argument("--days", type=int, default=0,
                        help="要求至少连续多少天有双跑样本")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--all", action="store_true", help="逐份列出（含通过的）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.root) if args.root else ROOT
    payloads = load_payloads(root / "state" / "double_run")
    summary = summarize(payloads, since=args.since)
    passed, reasons = check(summary, require=args.require, days=args.days)

    if args.json:
        print(json.dumps({**summary, "passed": passed, "reasons": reasons},
                         ensure_ascii=False, indent=2))
        return 0 if passed else 1

    print(f"[double-run] 样本 {summary['samples']} 份"
          f" / 代表样本 {summary['unique_samples']} 份"
          f"（通过 {summary['ok_samples']} / 未通过 {summary['failed_samples']}）")
    print(f"[double-run] 连续观测 {summary['continuous_days']} 天"
          f"（有样本日期 {summary['observed_days']} 天）")
    if summary["samples"]:
        print(f"[double-run] 日期范围 {summary['first_date'] or '-'} ~ "
              f"{summary['last_date'] or '-'}")
    print(f"[double-run] 未解释差异 {summary['unexplained']} 条 | "
          f"缺失 {summary['missing']} 条 | 裁定不一致 {summary['verdict_conflict']} 份 | "
          f"折叠 {summary['folded']} 条")
    show = summary["rows"] if args.all else summary["failed"]
    for row in show:
        flag = "OK  " if row["ok"] else "FAIL"
        print(f"  [{flag}] {row['review_id']} {row['date']} "
              f"verdict {row['verdict_new']} vs {row['verdict_legacy']} "
              f"未解释 {row['unexplained']} 缺失 {row['missing']}")
        if not row["ok"] and row["errors"]:
            print(f"         错误：{'；'.join(row['errors'])[:160]}")
    if not summary["samples"]:
        print("[double-run] 还没有双跑样本：先用 REVIEW_ENGINE=shadow 跑几次真实审查")
    if passed:
        print("[double-run] 结论：通过（可以继续积累样本，直到满足切流的天数要求）")
        return 0
    for reason in reasons:
        print(f"[double-run] 不通过：{reason}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
