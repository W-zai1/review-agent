#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描会被交付的文本，阻止 API Key / 私钥进入提交或发布包。

为什么需要它：
    `.gitignore` 只能阻止文件被 Git 跟踪，不能阻止「整个目录被压缩后发给别人」。
    本项目曾经把 `config/llm.local.json` 连同明文 Key 一起带进交付包，因此
    凭据检查必须是一条可执行命令，而不是 README 里的一句提醒。

扫描范围：
    - 默认：Git 跟踪的文件 + 未跟踪但未被忽略的文件 + 已知本地敏感配置；
    - `--staged`：只扫描暂存区，供 pre-commit 使用；
    - `--root DIR`：扫描一个已导出的目录（没有 .git 也能用）。

刻意不做的事：
    - 不输出命中的密钥原文，只显示脱敏预览；
    - 不扫描 `targets/`、`outputs/`、`state/` 等运行产物；
    - 默认不扫描 `tests/`：那里可能有故意构造的假密钥。

退出码：0 干净 / 1 命中 / 2 环境不满足（例如 --staged 但没有 Git）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 2 * 1024 * 1024

# 凭据正则只有一份：定义在 `tools/secret_patterns.py`（交付族）。
sys.path.insert(0, str(ROOT / "tools"))
import secret_patterns  # noqa: E402

SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "cache", "outputs", "logs", "state", "targets", "dist", "build",
}
EXPLICIT_LOCAL_PATHS = (
    "config/llm.local.json",
    "config/llm.local.json.tmp",
    ".env",
    ".env.local",
)

# 这些是测试里为了验证脱敏/配置行为而写死的假值，不是可用凭据。
# 只允许出现在指定文件里；复制到其它文件仍会命中。
SAFE_FIXTURES = {
    "sk-" + "abcdefghijklmnopqrstuvwxyz123456": {"checks/test_invariants.py"},
}

# 交付族正则定义在 tools/secret_patterns.py；这里保持同名、同序、同一个对象。
PATTERNS = secret_patterns.DELIVERY_PATTERNS


class SecretScanError(RuntimeError):
    """扫描环境不满足（没有 Git、读不到暂存对象等）。"""


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    preview: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} — {self.preview}"


def normalize(path: str) -> str:
    value = str(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _safe_fixture(kind: str, literal: str, path: str) -> bool:
    return literal in SAFE_FIXTURES and normalize(path) in SAFE_FIXTURES[literal]


def _preview(line: str, start: int, end: int) -> str:
    left = line[max(0, start - 24):start].strip()
    right = line[end:end + 12].strip()
    return f"{left}[REDACTED]{right}".strip()


def scan_text(text: str, path: str) -> list[Finding]:
    """扫描一段文本；同一个位置只报一次。"""
    findings: list[Finding] = []
    seen: set[tuple[int, int, int]] = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(line):
                literal = match.group(0)
                if _safe_fixture(kind, literal, path):
                    continue
                key = (lineno, match.start(), match.end())
                if key in seen:
                    continue
                seen.add(key)
                findings.append(Finding(
                    path=normalize(path),
                    line=lineno,
                    kind=kind,
                    preview=_preview(line, match.start(), match.end()),
                ))
    return findings


def scan_bytes(data: bytes, path: str) -> list[Finding]:
    if len(data) > MAX_BYTES or b"\x00" in data[:4096]:
        return []
    return scan_text(data.decode("utf-8", "replace"), path)


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def iter_tree(root: Path, *, include_tests: bool = False) -> Iterator[Path]:
    """遍历目录文本文件；运行产物和二进制由调用方再过滤。"""
    skip = set(SKIP_DIRS)
    if not include_tests:
        skip.add("tests")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in skip for part in rel.parts[:-1]):
            continue
        yield path


def _read_path(root: Path, rel: Path) -> list[Finding]:
    if not _inside(root, root / rel):
        return []
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return []
    return scan_bytes(data, rel.as_posix())


def scan_tree(root: Path, *, include_tests: bool = False) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    count = 0
    for path in iter_tree(root, include_tests=include_tests):
        count += 1
        findings.extend(_read_path(root, path.relative_to(root)))
    return findings, count


def _git(root: Path, *args: str, text: bool = False) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", "core.quotepath=false", *args]
    try:
        return subprocess.run(
            cmd, cwd=str(root), capture_output=True, text=text,
            encoding="utf-8" if text else None,
            errors="replace" if text else None,
        )
    except (OSError, ValueError) as exc:
        raise SecretScanError(f"无法执行 Git：{exc}") from exc


def _git_paths(root: Path, *args: str) -> list[Path]:
    res = _git(root, *args, "-z")
    if res.returncode != 0:
        msg = (res.stderr or res.stdout or b"").decode("utf-8", "replace").strip()
        raise SecretScanError(f"Git 取文件列表失败：{msg or res.returncode}")
    return [Path(p) for p in res.stdout.decode("utf-8", "replace").split("\0") if p]


def explicit_local_paths(root: Path) -> list[Path]:
    paths = [Path(p) for p in EXPLICIT_LOCAL_PATHS]
    paths.extend(Path("config").glob("*.local.json"))
    out: list[Path] = []
    for rel in paths:
        if (root / rel).is_file() and rel not in out:
            out.append(rel)
    return out


def scan_repo(root: Path = ROOT, *, include_tests: bool = False) -> tuple[list[Finding], int]:
    """扫描将要进入版本库的文件，并额外检查被忽略的本地凭据文件。"""
    try:
        tracked = _git_paths(root, "ls-files", "--cached")
        untracked = _git_paths(root, "ls-files", "--others", "--exclude-standard")
    except SecretScanError:
        return scan_tree(root, include_tests=include_tests)

    paths = list(dict.fromkeys(tracked + untracked + explicit_local_paths(root)))
    if not include_tests:
        paths = [p for p in paths if not p.as_posix().startswith("tests/")]

    findings: list[Finding] = []
    for rel in paths:
        findings.extend(_read_path(root, rel))
    return findings, len(paths)


def scan_staged(root: Path = ROOT) -> tuple[list[Finding], int]:
    paths = _git_paths(root, "diff", "--cached", "--name-only",
                       "--diff-filter=ACMR")
    findings: list[Finding] = []
    for rel in paths:
        res = _git(root, "show", f":{rel.as_posix()}")
        if res.returncode != 0:
            raise SecretScanError(f"读取暂存对象失败：{rel.as_posix()}")
        findings.extend(scan_bytes(res.stdout, rel.as_posix()))
    return findings, len(paths)


def _print(findings: list[Finding], scanned: int, scope: str) -> int:
    print(f"[secret-scan] 范围={scope} 扫描={scanned} 命中={len(findings)}")
    for finding in findings:
        print(f"  - {finding.describe()}")
    if findings:
        print("[secret-scan] 已阻断：请移除凭据、改为环境变量，并轮换已经落盘的 Key。")
        return 1
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫描提交/发布内容中的明文凭据")
    ap.add_argument("--staged", action="store_true", help="只扫描暂存区")
    ap.add_argument("--root", default=str(ROOT), help="扫描根目录（默认项目根目录）")
    ap.add_argument("--include-tests", action="store_true",
                    help="连 tests/ 一起扫描（测试夹具可能产生误报）")
    args = ap.parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).resolve()
    try:
        if args.staged:
            findings, scanned = scan_staged(root)
            return _print(findings, scanned, "暂存区")
        findings, scanned = scan_repo(root, include_tests=args.include_tests)
        return _print(findings, scanned, "工作区")
    except SecretScanError as exc:
        print(f"[secret-scan] 无法完成扫描：{exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
