#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dup_audit.py —— 区分「同名不同物」与「真的复制了一份」。

为什么需要它：`docs/PHASE0_INVENTORY.md` §5 列了 27 个 `main`、6 个 `summarize`……
这些**是信号，不是结论**。同名函数可能只是都叫 `main` 的 CLI 入口，也可能是
真的把一段逻辑抄了两份。只有比**函数体**才分得清。

判定方式（故意简单，避免误伤）：
    · 忽略缩进与空行，保留注释以外的语句序列；
    · 只有「去掉 docstring 后仍有 ≥3 条语句」且文本完全一致的才算**候选重复**；
    · 名字不同但体相同也算——复制粘贴时常顺手改名。

用法：
    python tools/dup_audit.py            # 打印全部同名组与候选重复
    python tools/dup_audit.py --json     # 机读（给 checks 用）
退出码：0 正常（发现重复也返回 0——这是审计工具，不是守门）；2 路径不存在。
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("review_agent", "tools")
SCAN_FILES = ("review.py", "server.py")
MIN_STATEMENTS = 3


def _body_signature(node: ast.AST) -> str:
    """函数体的规整签名：只留语句结构，去掉 docstring 与位置信息。"""
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant):
        body = body[1:]                      # 去掉 docstring
    dumped = ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)
    return " ".join(dumped.split())


def _statement_count(node: ast.AST) -> int:
    body = list(getattr(node, "body", []))
    if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    return len(body)


def sources() -> list[Path]:
    paths: list[Path] = []
    for name in SCAN_ROOTS:
        base = ROOT / name
        if base.is_dir():
            paths.extend(sorted(base.rglob("*.py")))
    for name in SCAN_FILES:
        path = ROOT / name
        if path.is_file():
            paths.append(path)
    return paths


def scan(min_statements: int = MIN_STATEMENTS) -> dict:
    """返回 {"by_name": {...}, "duplicates": [...]}。"""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for path in sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            count = _statement_count(node)
            if count < min_statements:
                continue
            by_name[node.name].append({
                "module": path.relative_to(ROOT).as_posix(),
                "name": node.name,
                "lineno": node.lineno,
                "statements": count,
                "signature": _body_signature(node),
            })

    duplicates = []
    for name, items in by_name.items():
        groups: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            groups[item["signature"]].append(item)
        for _sig, group in groups.items():
            modules = {g["module"] for g in group}
            if len(modules) >= 2:            # 跨模块才算「复制了一份」
                duplicates.append({
                    "name": name,
                    "modules": sorted(modules),
                    "statements": group[0]["statements"],
                })

    return {
        "by_name": {name: len({i["module"] for i in items})
                    for name, items in sorted(by_name.items(),
                                              key=lambda kv: (-len(kv[1]), kv[0]))},
        "duplicates": sorted(duplicates, key=lambda d: d["name"]),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="同名函数 / 重复实现审计")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--min-statements", type=int, default=MIN_STATEMENTS)
    args = ap.parse_args(argv)

    if not (ROOT / "review_agent").is_dir():
        print("[dup-audit] 找不到 review_agent/", file=sys.stderr)
        return 2
    report = scan(args.min_statements)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print(f"[dup-audit] 同名函数（≥2 个模块，按模块数排序，只统计 ≥{MIN_STATEMENTS} 条语句）")
    for name, count in list(report["by_name"].items())[:12]:
        print(f"  {name:<24} {count} 个模块")
    print("")
    if not report["duplicates"]:
        print("[dup-audit] 未发现跨模块的**函数体完全相同**：同名不等于重复。")
        return 0
    print(f"[dup-audit] 候选重复 {len(report['duplicates'])} 组（函数体逐字相同、跨模块）：")
    for item in report["duplicates"]:
        print(f"  {item['name']}（{item['statements']} 条语句）← {', '.join(item['modules'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
