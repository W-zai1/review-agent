#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CHANGELOG 守门：改了代码却没登记账本就拦住。

为什么需要它：README 里写「每次增删改都要登记」只是**约定**，约定靠记性执行，
第三次改动就会被忘掉。本脚本把约定变成**退出码**，可以挂进 git pre-commit，
也可以手动跑一遍确认"这一轮我登记了吗"。

判定只有一条规则（故意简单，才不会误伤）：
    本次改动里存在「值得登记的文件」→ 就必须有 CHANGELOG.md 的改动。

不做的事（明确划界）：
- 不判断账本**内容对不对**（那是 `tests/test_api_contract.py` 的活）；
- 不检查日期新鲜度（跨天提交会误报）；
- 不要求每个文件一条（一次改动一条即可）。

用法：
    python tools/changelog_guard.py            # 看工作区（含未跟踪的新文件）
    python tools/changelog_guard.py --staged   # 看暂存区，pre-commit 用
    python tools/changelog_guard.py --explain  # 打印判定依据

退出码：0 通过 / 1 需要登记 / 2 环境不满足（不是 git 仓库等，宽容放行）
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = "CHANGELOG.md"

# 产物 / 缓存 / 记忆：它们是"跑出来的"，不是"写出来的"，不必逐条登记
IGNORE_PREFIXES = (
    "outputs/",
    "logs/",
    "ledger/",
    "state/",
    "cache/",
    "__pycache__/",
    ".workbuddy/",      # 那是给下一次开工的备忘，不是项目改动
    ".git/",
    "node_modules/",
    "targets/.trash/",
    "outputs/.trash/",
)
IGNORE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORE_SUFFIXES = (".pyc", ".pyo", ".log", ".tmp", ".bak", ".orig", ".rej", ".swp")
# 运行时状态文件（内容随时在变，不该逼着人每跑一次就登记）
IGNORE_EXACT = {
    "config/favorites.json",
    "config/report_order.json",
    "config/llm.local.json",
    "config/llm_history.json",
    "config/learning.json",
}
# 验证时随手生成的探针页 / 抓下来的 DOM / 截图，用完就删
IGNORE_TEMP_PREFIX = ("_probe", "_dom", "_shot", "_d2_", "_d3_")

CODE_SUFFIXES = (
    ".py", ".js", ".mjs", ".cjs", ".ts", ".html", ".htm", ".css", ".scss",
    ".yaml", ".yml", ".json", ".md", ".txt", ".ps1", ".sh", ".bat",
    ".cfg", ".toml", ".ini",
)
# 没有扩展名但改了会影响行为的（.gitignore 一改，别人的仓库就变样了）
ALWAYS_REGISTER_NAMES = {".gitignore", ".gitattributes", ".dockerignore",
                         "LICENSE", "Makefile", "Procfile"}


class GuardError(RuntimeError):
    """取不到改动列表（无 git / 无权限），不是"需要登记"，别混为一谈。"""


def normalize(path: str) -> str:
    """统一成正斜杠、去掉前导 ./，让 Windows 和 POSIX 判定一致。"""
    p = str(path).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def must_register(path: str) -> bool:
    """这个文件被改动后，是否必须新增一条 CHANGELOG。"""
    p = normalize(path)
    if not p:
        return False
    if p == LEDGER:
        return False
    if p in IGNORE_EXACT:
        return False
    base = p.rsplit("/", 1)[-1]
    if base in IGNORE_NAMES:
        return False
    if p.startswith(IGNORE_PREFIXES):
        return False
    if base.endswith(IGNORE_SUFFIXES):
        return False
    if base.startswith(IGNORE_TEMP_PREFIX):
        return False
    if base in ALWAYS_REGISTER_NAMES:
        return True
    return base.endswith(CODE_SUFFIXES)


def decide(changed):
    """纯函数，便于测试。

    返回 (ok, missing)：missing 是「改了但没登记」的文件清单。
    """
    files = [normalize(p) for p in changed if normalize(p)]
    if LEDGER in files:
        # 账本自身被改了：本条已登记。不再吹毛求疵地数条数——
        # 一次改动只写一条也合规，写多条也行。
        return True, []
    missing = [p for p in files if must_register(p)]
    return (not missing), missing


def _parse_porcelain(out: str):
    """`git status --porcelain` 每行前 3 个字符是状态位；重命名取新路径。"""
    files = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            files.append(path)
    return files


def git_changed(staged: bool):
    """取改动文件列表；取不到就抛 GuardError，调用方据此宽容放行。"""
    # core.quotepath=false：否则中文文件名会被转成 "\346\216\245" 八进制，
    # 既看不清也没法跟 IGNORE_EXACT 精确匹配（本仓库 docs/ 全是中文名）。
    base = ["git", "-c", "core.quotepath=false"]
    if staged:
        cmd = base + ["diff", "--cached", "--name-only", "--diff-filter=ACMRD"]
    else:
        # `-uall` 不能省：默认 -unormal 对**未跟踪的新目录**只输出目录名
        # （`knowledge/laws/`），展开不了就判成"没有扩展名 → 不必登记"。
        # 实测新增 knowledge/laws/（条文原文）与 tools/law_*.py 时前者被放行，
        # 等于"往判定依据里塞了新东西，账本可以不知道"。
        cmd = base + ["status", "--porcelain", "-uall"]
    try:
        res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:      # 没装 git
        raise GuardError(f"无法执行 git（{exc.__class__.__name__}）") from exc
    if res.returncode != 0:
        msg = (res.stderr or res.stdout or "").strip().splitlines()
        head = msg[0] if msg else f"exit {res.returncode}"
        raise GuardError(f"git 取改动失败：{head}")
    if staged:
        return [normalize(x) for x in res.stdout.splitlines() if x.strip()]
    return _parse_porcelain(res.stdout)


def check(staged: bool = False, explain: bool = False) -> int:
    try:
        changed = git_changed(staged)
    except GuardError as exc:
        print(f"[changelog-guard] 跳过检查 — {exc}")
        print("[changelog-guard] 无法枚举改动，不阻断流程；请人工确认 CHANGELOG.md 是否已登记。")
        return 2

    ok, missing = decide(changed)
    if explain:
        scope = "暂存区" if staged else "工作区"
        print(f"[changelog-guard] 范围={scope} 改动={len(changed)} 漏登记={len(missing)}")
        for p in changed:
            flag = "需要登记" if must_register(p) else "不登记"
            print(f"  [{flag}] {p}")

    if ok:
        if explain:
            print("[changelog-guard] 通过：账本已更新，或本次只动了产物/缓存。")
        return 0

    print("")
    print("=" * 68)
    print("[changelog-guard] 有代码/文档改动，但 CHANGELOG.md 没有对应条目。")
    print("=" * 68)
    print("请在本文件顶部（紧接 `# 2026-xx-xx` 日期标题下）新增一条，格式：")
    print("")
    print("### YYYY-MM-DD · 新增|修改|删除|修复 · 一句话标题")
    print("")
    print("- **文件**：改了哪些（含测试与 README 章节）")
    print("- **改了什么**：写到能照着复现")
    print("- **为什么**：触发它的现象 / 用户反馈 / 根因")
    print("- **验证**：跑了什么 + 具体数（测试项数 / 实测数值 / 报告 ID 对比）")
    print("")
    print(f"本次待登记（最多列 12 个，共 {len(missing)} 个）：")
    for p in missing[:12]:
        print(f"  - {p}")
    if len(missing) > 12:
        print(f"  ... 另有 {len(missing) - 12} 个")
    print("")
    print("确实不该登记的（如纯产物、临时文件），请补进本脚本的 IGNORE_* 规则，")
    print("而不是 --no-verify 跳过——跳过会把守门变成摆设。")
    print("=" * 68)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="检查是否漏登记 CHANGELOG.md")
    ap.add_argument("--staged", action="store_true", help="只检查暂存区（pre-commit 用）")
    ap.add_argument("--explain", action="store_true", help="逐个文件打印判定依据")
    args = ap.parse_args(argv)
    return check(staged=args.staged, explain=args.explain)


if __name__ == "__main__":
    sys.exit(main())
