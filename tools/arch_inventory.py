#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arch_inventory.py —— Phase 0「冻结与盘点」的生成器。

只读扫描整个仓库的 Python 源码，产出一份可复核的盘点表：

    python tools/arch_inventory.py                 # 写到 docs/PHASE0_INVENTORY.md
    python tools/arch_inventory.py --print         # 只打印，不落盘

盘点内容（对应 REFACTOR_PLAN 阶段 0 的验收项）：
  1. 规模：目录 / 文件 / 行数，单文件占比（monolith 指标）
  2. 内部依赖图：谁 import 谁，扇入 / 扇出
  3. 零引用模块候选：没有任何 Python 文件 import 它
     —— 不等于死代码（CLI 入口本来就不会被 import），所以只列"待人工确认"
  4. 重名函数：同名函数散落在三个以上模块 = 重复实现的信号
  5. 测试基线：tests/ 与 checks/ 的用例数、CI 实际执行的命令

设计约束：
  · 只用标准库（这是盘点工具，不能反过来依赖被盘点的代码）
  · 不改任何源文件；输出只写 docs/PHASE0_INVENTORY.md
  · 解析失败的文件不静默跳过，计入 "parse_errors" 并写进报告
"""
from __future__ import annotations

import argparse
import ast
import collections
import datetime
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "docs" / "PHASE0_INVENTORY.md"

# 运行产物 / 缓存 / 二进制目录：不参与盘点
SKIP_DIRS = {
    ".git", ".github", ".workbuddy", ".venv", "venv", "node_modules",
    "__pycache__", "cache", "outputs", "logs", "ledger", "state", "targets",
    "holdout", "uploads", ".pytest_cache", ".mypy_cache",
}

# 目录职责（盘点表用；未知目录留空，由人补）
DIR_ROLE = {
    "": "入口与主流程（review.py / server.py）",
    "tools": "能力实现（规则、检索、LLM、格式、学习…），平铺无注册表",
    "review_agent": "Architecture v1（domain / runtime / capabilities），尚未接管旧流程",
    "checks": "公开不变量测试（CI 必跑）",
    "tests": "全量回归与对抗测试（.gitignore 排除，仅本地）",
    "agents": "Agent manifest + 自带规则 / 检查器",
    "rules": "规则 YAML（单一事实源）",
    "knowledge": "事实库 / 法条库 / 政策库",
    "config": "运行配置（版本号、情境、LLM、学习配额…）",
    "web": "单页控制台（无框架）",
    "demo": "演示语料",
}

# 这些目录里的文件本身就是入口，不参与「零引用」判断
TEST_DIRS = {"tests", "checks"}


def iter_py_files() -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(".py"):
                out.append(Path(dirpath) / name)
    return out


def dotted(path: Path) -> str:
    """文件 → 点分模块名（__init__.py 归到包名）。"""
    parts = list(path.relative_to(ROOT).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def parse_file(path: Path):
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src, filename=str(path))
    loc = sum(1 for line in src.splitlines() if line.strip())
    funcs, classes = [], []
    imports: list[tuple[str, int, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            funcs.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, 0, ()))
        elif isinstance(node, ast.ImportFrom):
            imports.append((node.module or "", node.level or 0,
                            tuple(a.name for a in node.names)))
    return {"loc": loc, "funcs": funcs, "classes": classes,
            "imports": imports, "src": src}


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def top_dir(path: Path) -> str:
    r = path.relative_to(ROOT)
    return r.parts[0] if len(r.parts) > 1 else ""


def resolve_imports(info: dict[Path, dict], exact: dict[str, Path],
                    by_stem: dict[str, list[Path]]) -> tuple[dict, dict]:
    fan_in: dict[Path, set[Path]] = collections.defaultdict(set)
    fan_out: dict[Path, set[Path]] = collections.defaultdict(set)

    for path, meta in info.items():
        current = dotted(path)
        pkg = current if path.name == "__init__.py" else current.rsplit(".", 1)[0]

        def add(target: Path) -> None:
            if target != path:
                fan_out[path].add(target)
                fan_in[target].add(path)

        def lookup(name: str) -> None:
            if not name:
                return
            if name in exact:
                add(exact[name])
                return
            # 平铺目录（tools/）用 `import regex_check`：按文件主名匹配
            for target in by_stem.get(name.rsplit(".", 1)[-1], []):
                add(target)

        for module, level, names in meta["imports"]:
            if level:
                base = pkg.split(".")
                if level > 1:
                    base = base[: max(0, len(base) - (level - 1))]
                full = ".".join(base + ([module] if module else []))
            else:
                full = module
            lookup(full)
            for name in names:
                lookup(f"{full}.{name}" if full else name)

    return fan_in, fan_out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="只打印摘要，不写 docs/PHASE0_INVENTORY.md")
    args = ap.parse_args()

    files = iter_py_files()
    info, parse_errors = {}, []
    for p in files:
        try:
            info[p] = parse_file(p)
        except (SyntaxError, OSError) as e:
            parse_errors.append((p, f"{type(e).__name__}: {e}"))

    exact = {dotted(p): p for p in info}
    by_stem: dict[str, list[Path]] = collections.defaultdict(list)
    for p in info:
        by_stem[p.stem].append(p)
    fan_in, fan_out = resolve_imports(info, exact, by_stem)

    # 零扇入：没有任何 Python 文件 import 它。再用全仓文本引用区分
    # 「CLI 入口（正常）」/「只被测试或文档引用」/「疑似死文件」。
    all_src = {p: m["src"] for p, m in info.items()}
    zero = []
    for p, meta in info.items():
        if fan_in.get(p) or top_dir(p) in TEST_DIRS:
            continue
        stem = p.stem
        textual = [q for q in all_src if q != p and stem in all_src[q]]
        zero.append({
            "path": rel(p), "loc": meta["loc"], "main": "__main__" in meta["src"],
            "refs": [rel(q) for q in textual][:4], "ref_count": len(textual),
        })
    test_only = sum(1 for p in info if top_dir(p) in TEST_DIRS)

    # 重名函数：三个以上模块出现同名函数 → 重复实现信号
    fn_map: dict[str, set[str]] = collections.defaultdict(set)
    for p, meta in info.items():
        if top_dir(p) in TEST_DIRS:
            continue
        for fn in meta["funcs"]:
            if not fn.startswith("__"):
                fn_map[fn].add(rel(p))
    dup_fns = sorted(
        ((fn, paths) for fn, paths in fn_map.items() if len(paths) >= 3),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )

    by_dir = collections.Counter()
    loc_by_dir = collections.Counter()
    for p, meta in info.items():
        d = top_dir(p)
        by_dir[d] += 1
        loc_by_dir[d] += meta["loc"]

    total_loc = sum(m["loc"] for m in info.values())
    biggest = sorted(info.items(), key=lambda kv: -kv[1]["loc"])[:15]
    review_loc = info.get(ROOT / "review.py", {}).get("loc", 0)
    new_loc = sum(m["loc"] for p, m in info.items()
                  if top_dir(p) == "review_agent")

    def count_tests(sub: str) -> tuple[int, int]:
        d = ROOT / sub
        if not d.is_dir():
            return (0, 0)
        n_files = n_tests = 0
        for p in d.rglob("*.py"):
            src = all_src.get(p)
            if src is None:
                continue
            n_files += 1
            n_tests += src.count("def test_")
        return (n_files, n_tests)

    tests_n = count_tests("tests")
    checks_n = count_tests("checks")

    now = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    L = []
    L.append("# Phase 0 盘点：review-agent 现状（自动生成）\n")
    L.append(f"> 生成时间：{now}　生成命令：`python tools/arch_inventory.py`\n")
    L.append("> 本文件只做事实清点，不做评价。结论与取舍写在 "
             "[REFACTOR_PLAN.md](REFACTOR_PLAN.md)。\n")

    L.append("## 1. 规模\n")
    L.append(f"- Python 文件：**{len(info)}** 个，有效代码行（非空行）：**{total_loc:,}**")
    if total_loc:
        L.append(f"- 单文件最大：`review.py` **{review_loc:,}** 行，占全部 Python 代码的 "
                 f"**{review_loc / total_loc * 100:.1f}%**")
    L.append(f"- Architecture v1 新包：`review_agent/` **{new_loc:,}** 行"
             f"（占 {new_loc / total_loc * 100:.1f}%，尚未接管旧流程）" if total_loc else "")
    L.append(f"- 解析失败：{len(parse_errors)} 个" + (
        "（" + "、".join(f"`{rel(p)}` {e}" for p, e in parse_errors) + "）"
        if parse_errors else ""))
    L.append("")
    L.append("| 目录 | 职责 | Python 文件 | 代码行 |")
    L.append("| --- | --- | ---: | ---: |")
    for d, n in sorted(by_dir.items(), key=lambda kv: -loc_by_dir[kv[0]]):
        L.append(f"| `{d or '.'}` | {DIR_ROLE.get(d, '（未登记）')} | {n} | {loc_by_dir[d]:,} |")
    L.append("")

    L.append("## 2. 最大的 15 个文件\n")
    L.append("| 文件 | 代码行 | 被 import 次数 |")
    L.append("| --- | ---: | ---: |")
    for p, meta in biggest:
        L.append(f"| `{rel(p)}` | {meta['loc']:,} | {len(fan_in.get(p, ()))} |")
    L.append("")

    L.append("## 3. 扇入 / 扇出最高的模块\n")
    L.append("扇入高 = 事实上的公共底座（动它要先有测试）；扇出高 = 协调者（可能是上帝对象）。\n")
    L.append("| 模块 | 扇入（被谁依赖） | 扇出（依赖谁） |")
    L.append("| --- | ---: | ---: |")
    for p, _ in sorted(fan_in.items(), key=lambda kv: -len(kv[1]))[:12]:
        L.append(f"| `{rel(p)}` | {len(fan_in[p])} | {len(fan_out.get(p, ()))} |")
    L.append("| … | | |")
    for p, _ in sorted(fan_out.items(), key=lambda kv: -len(kv[1]))[:5]:
        L.append(f"| `{rel(p)}`（扇出 Top） | {len(fan_in.get(p, ()))} | {len(fan_out[p])} |")
    L.append("")

    L.append("## 4. 零扇入模块（待人工确认，不是死代码判决）\n")
    L.append(f"生产代码里共 **{len(zero)}** 个模块没有任何 Python 文件 import"
             f"（另有 {test_only} 个测试文件本身就是入口，未列入）。判断口径：\n")
    L.append("- `__main__` 存在且被文档 / .bat 调用 → **CLI 入口，合理**")
    L.append("- 只被测试或文档引用 → **测试夹具 / 待接线**")
    L.append("- 既无入口也无引用 → **疑似遗留，阶段 3 清理前需人工确认**\n")
    L.append("| 模块 | 行数 | 有 `__main__` | 被其他文件文本引用 |")
    L.append("| --- | ---: | :---: | ---: |")
    for z in sorted(zero, key=lambda z: -z["loc"]):
        L.append(f"| `{z['path']}` | {z['loc']:,} | {'是' if z['main'] else '否'} | {z['ref_count']} |")
    L.append("")
    L.append("<details><summary>零扇入明细（引用来源）</summary>\n")
    for z in sorted(zero, key=lambda z: -z["loc"]):
        refs = "、".join(f"`{r}`" for r in z["refs"]) or "—"
        L.append(f"- `{z['path']}` ← {refs}")
    L.append("\n</details>\n")

    L.append("## 5. 重名函数（≥3 个模块同名，重复实现信号）\n")
    if not dup_fns:
        L.append("（无）\n")
    else:
        L.append("| 函数名 | 出现模块数 | 模块 |")
        L.append("| --- | ---: | --- |")
        for fn, paths in dup_fns[:25]:
            shown = "、".join(f"`{p}`" for p in sorted(paths)[:6])
            more = "…" if len(paths) > 6 else ""
            L.append(f"| `{fn}` | {len(paths)} | {shown}{more} |")
        L.append("")
    L.append("> 同名不必然是重复代码（`main` / `load` / `_norm` 这类短名很常见）；"
             "这里是人工比对清单的入口，不是自动结论。\n")

    L.append("## 6. 测试与验收基线\n")
    L.append(f"- `tests/`：{tests_n[0]} 个文件，{tests_n[1]} 个 `test_*` 用例（.gitignore 排除，仅本地）")
    L.append(f"- `checks/`：{checks_n[0]} 个文件，{checks_n[1]} 个 `test_*` 用例（公开，CI 必跑）")
    L.append("- CI 实际命令（`.github/workflows/ci.yml`）：")
    L.append("  - `python -m unittest discover -s checks -v`")
    L.append("  - `python -m unittest discover -s tests -v`（tests/ 存在时）")
    L.append("  - `python tools/golden_eval.py`（金标准：召回与误报不许回退）")
    L.append("  - `python tools/changelog_guard.py --explain`（干净树必须通过）")
    L.append(f"- Python 版本：CI 3.12 / 3.13；本机 {sys.version.split()[0]}")
    L.append("")

    md = "\n".join(L) + "\n"
    if args.print_only:
        print(md)
    else:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUT_PATH.with_suffix(".md.tmp")
        tmp.write_text(md, encoding="utf-8")
        tmp.replace(OUT_PATH)
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:                                    # noqa: BLE001
            pass
        print(f"[inventory] {len(info)} files / {total_loc:,} loc / "
              f"{len(zero)} zero-fan-in -> {rel(OUT_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
