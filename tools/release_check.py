#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布前检查：验证“即将提交的内容”本身是可运行、可交付的。

它解决的是工作区测试看不到的问题：
    - 新文件忘了 `git add`，本机能 import，干净 clone 却 ImportError；
    - 整个目录打包时把 `config/llm.local.json`、运行产物一起带走；
    - CI 只跑公开测试，私有回归没有被纳入发布判断。

做法：
    1. 要求工作区没有未暂存/未跟踪文件；
    2. 从 Git 暂存区 `write-tree`，再 `git archive` 到临时目录；
    3. 在临时目录里检查关键文件、密钥、编译和 `checks/` 测试；
    4. 可选地在工作区再跑 `tests/` 私有回归（它不在公开归档里）。

不会提交、不会改分支，也不会把归档留在工作区。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import secret_scan  # noqa: E402

# 这些文件曾被“只提交了一部分改动”漏掉，导致干净 clone 起不来。
REQUIRED_PATHS = (
    "review.py",
    "server.py",
    "agents/doc-review/manifest.json",
    "agents/security-audit/manifest.json",
    "review_agent/infrastructure/__init__.py",
    "review_agent/infrastructure/ledger.py",
    "review_agent/infrastructure/retention.py",
    "review_agent/capabilities/research.py",
    "review_agent/runtime/cancel.py",
    "review_agent/runtime/nodes/research.py",
    "config/retention.yaml",
    "tools/double_run.py",
    "tools/secret_scan.py",
)


class ReleaseCheckError(RuntimeError):
    """发布条件不满足；消息可直接给使用者看。"""


def _run(root: Path, cmd: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=env)


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", "core.quotepath=false", *args]
    try:
        return _run(root, cmd)
    except (OSError, ValueError) as exc:
        raise ReleaseCheckError(f"无法执行 Git：{exc}") from exc


def require_clean(root: Path) -> None:
    unstaged = git(root, "diff", "--name-only", "--diff-filter=ACMRD")
    untracked = git(root, "ls-files", "--others", "--exclude-standard")
    if unstaged.returncode != 0 or untracked.returncode != 0:
        msg = (unstaged.stderr or untracked.stderr or "git 状态检查失败").strip()
        raise ReleaseCheckError(msg)
    changed = [x for x in (unstaged.stdout, untracked.stdout) if x.strip()]
    if changed:
        detail = "\n".join("\n".join(changed).strip().splitlines()[:12])
        raise ReleaseCheckError(
            "工作区仍有未暂存/未跟踪文件。先补齐并暂存，再运行本检查：\n" + detail)


def missing_required(root: Path) -> list[str]:
    return [p for p in REQUIRED_PATHS if not (root / p).is_file()]


def _safe_members(root: Path, members: Iterable[tarfile.TarInfo]) -> None:
    base = root.resolve()
    for member in members:
        target = (root / member.name).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise ReleaseCheckError(f"归档包含越界路径：{member.name}") from exc


def export_index(root: Path, target: Path) -> Path:
    """把暂存区导成一个干净的临时目录，不依赖工作区文件。"""
    tree = git(root, "write-tree")
    if tree.returncode != 0:
        raise ReleaseCheckError((tree.stderr or tree.stdout).strip() or "git write-tree 失败")
    tree_id = tree.stdout.strip()
    if not tree_id:
        raise ReleaseCheckError("暂存区为空，无法导出待提交快照")

    archive = target / "tree.tar"
    created = git(root, "archive", "--format=tar", f"--output={archive}", tree_id)
    if created.returncode != 0:
        raise ReleaseCheckError((created.stderr or created.stdout).strip() or "git archive 失败")

    out = target / "tree"
    out.mkdir()
    with tarfile.open(archive, "r") as tar:
        _safe_members(out, tar.getmembers())
        tar.extractall(out, filter="data")
    return out


def _format_test_result(res: subprocess.CompletedProcess, label: str) -> str:
    text = "\n".join(x for x in (res.stdout, res.stderr) if x and x.strip())
    tail = "\n".join(text.strip().splitlines()[-12:])
    return f"{label}：exit={res.returncode}\n{tail}"


def run_public_checks(root: Path) -> str:
    compiled = _run(root, [sys.executable, "-m", "compileall", "-q", "."])
    if compiled.returncode != 0:
        raise ReleaseCheckError(_format_test_result(compiled, "compileall"))
    tests = _run(root, [sys.executable, "-m", "unittest",
                        "discover", "-s", "checks", "-q"], timeout=1800)
    if tests.returncode != 0:
        raise ReleaseCheckError(_format_test_result(tests, "checks 测试"))
    return _format_test_result(tests, "checks 测试")


def run_private_checks(root: Path) -> str:
    test_files = sorted((root / "tests").glob("test*.py"))
    if not test_files:
        raise ReleaseCheckError(
            "要求私有回归，但 tests/ 下没有 test*.py。"
            "公开仓库不会包含它们，请在私有镜像或发布机上运行。")
    tests = _run(root, [sys.executable, "-m", "unittest",
                        "discover", "-s", "tests", "-q"], timeout=3600)
    if tests.returncode != 0:
        raise ReleaseCheckError(_format_test_result(tests, "tests 测试"))
    return _format_test_result(tests, "tests 测试")


def check(root: Path = ROOT, *, no_tests: bool = False,
          require_private_tests: bool = False) -> None:
    require_clean(root)

    missing = missing_required(root)
    if missing:
        raise ReleaseCheckError("关键文件不在工作区：\n" + "\n".join(f"  - {p}" for p in missing))

    print("[release-check] 工作区干净，开始导出暂存区快照")
    with tempfile.TemporaryDirectory(prefix="review-agent-release-") as tmp:
        clean = export_index(root, Path(tmp))
        missing_in_clean = missing_required(clean)
        if missing_in_clean:
            raise ReleaseCheckError(
                "关键文件没有进入待提交快照（多半是忘了 git add）：\n"
                + "\n".join(f"  - {p}" for p in missing_in_clean))

        findings, scanned = secret_scan.scan_tree(clean, include_tests=False)
        print(f"[release-check] 快照密钥扫描：{scanned} 个文件，命中 {len(findings)}")
        if findings:
            raise ReleaseCheckError("\n".join(f"  - {f.describe()}" for f in findings))

        if not no_tests:
            print("[release-check] 在快照内运行公开不变量")
            print(run_public_checks(clean))

    if require_private_tests:
        print("[release-check] 工作区私有回归")
        print(run_private_checks(root))

    print("[release-check] 通过")


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检查待提交快照是否可交付")
    ap.add_argument("--root", default=str(ROOT), help="项目根目录")
    ap.add_argument("--no-tests", action="store_true", help="只做完整性/密钥/归档检查")
    ap.add_argument("--require-private-tests", action="store_true",
                    help="额外在工作区运行 tests/ 私有回归")
    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        check(Path(args.root).resolve(), no_tests=args.no_tests,
              require_private_tests=args.require_private_tests)
        return 0
    except (ReleaseCheckError, subprocess.TimeoutExpired, OSError) as exc:
        print(f"[release-check] 失败：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
