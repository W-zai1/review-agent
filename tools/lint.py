#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lint.py —— 代码 lint + 硬编码密钥扫描 (Demo 工具)

优先调用 ruff(python) / eslint(js,ts)；未安装则优雅跳过 lint，但密钥扫描始终执行。
输出结构化的 findings，供 review.py 直接生成 STYLE-004 issue（带行号，可回查）：

    {"kind": "secret", "key": "api_key", "line": 6, "preview": "api_key = \"sk-demo\"…"}
    {"kind": "lint", "tool": "ruff", "line": 3, "col": 1,
     "code": "F401", "message": "'json' imported but unused"}

CLI:
    python lint.py path/to/code.py
"""
import os
import re
import sys
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regex_check

# ruff:   path/to/file.py:3:1: F401 'json' imported but unused
RUFF_LINE = re.compile(r"^(.+?):(\d+):(\d+):\s*(.*)$")
# eslint stylish:  "  3:10  error  'x' is not defined  no-undef"
ESLINT_STYLISH = re.compile(r"^\s*(\d+):(\d+)\s+(error|warning)\s+(.+?)(?:\s{2,}(\S+))?\s*$")
# eslint compact:  "/path/file.js: line 3, col 10, Error - 'x' is not defined (no-undef)"
ESLINT_COMPACT = re.compile(
    r"^(.+?): line (\d+), col (\d+), (?:Error|Warning) - (.+?)(?: \(([^)]+)\))?$")


def _line_no(text, idx):
    return text.count("\n", 0, idx) + 1


def _secret_findings(text):
    findings = []
    for m in regex_check.find_secret_spans(text):
        raw = m["raw"]
        findings.append({
            "kind": "secret",
            "key": m["key"],
            "line": _line_no(text, m["start"]),
            "preview": (raw[:18] + "…") if len(raw) > 18 else raw,
        })
    return findings


def _parse_ruff(output, tool="ruff"):
    findings = []
    for line in output.splitlines():
        m = RUFF_LINE.match(line.strip())
        if not m:
            continue
        rest = m.group(4).strip()
        code = ""
        mm = re.match(r"^([A-Z]+[0-9]+)\s+(.*)$", rest)
        if mm:
            code, rest = mm.group(1), mm.group(2)
        findings.append({
            "kind": "lint", "tool": tool, "line": int(m.group(2)),
            "col": int(m.group(3)), "code": code, "message": rest,
        })
    return findings


def _parse_eslint(output):
    findings = []
    for line in output.splitlines():
        m = ESLINT_COMPACT.match(line.strip())
        if m:
            findings.append({
                "kind": "lint", "tool": "eslint", "line": int(m.group(2)),
                "col": int(m.group(3)), "code": m.group(5) or "eslint",
                "message": m.group(4).strip(),
            })
            continue
        m = ESLINT_STYLISH.match(line)
        if m:
            findings.append({
                "kind": "lint", "tool": "eslint", "line": int(m.group(1)),
                "col": int(m.group(2)), "code": m.group(5) or m.group(3),
                "message": m.group(4).strip(),
            })
    return findings


def run_lint(path):
    ext = os.path.splitext(path)[1].lower()
    result = {"file": path, "status": "skipped", "details": None, "findings": []}

    if ext == ".py":
        tool, parser = "ruff", _parse_ruff
        cmd = ["ruff", "check", path]
    elif ext in (".js", ".ts", ".jsx", ".tsx"):
        tool, parser = "eslint", _parse_eslint
        cmd = ["eslint", path]
    else:
        tool, parser, cmd = None, None, None
        result["details"] = "非代码文件，跳过 lint（仍执行密钥扫描）"

    if cmd:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            raw = (r.stdout or "")
            if r.stderr:
                raw = (raw + "\n" + r.stderr).strip()
            result["findings"].extend(parser(raw))
            result["status"] = "issues" if result["findings"] else "ok"
            result["details"] = (raw.strip() or "无输出")[:800]
        except FileNotFoundError:
            result["status"] = "skipped"
            result["details"] = f"{tool} 未安装，跳过 lint（仍执行密钥扫描）"
        except subprocess.TimeoutExpired:
            result["status"] = "failed"
            result["details"] = f"{tool} 超时（30s）"

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
        secrets = _secret_findings(text)
    except Exception as e:  # 读不了文件也不能让审查崩掉
        secrets = []
        result["details"] = (result["details"] or "") + f" / 读取失败：{e}"
    result["findings"].extend(secrets)
    result["secret_findings"] = secrets
    if secrets:
        result["status"] = "issues"
    return result


def main():
    if len(sys.argv) < 2:
        print("usage: lint.py <file>")
        sys.exit(1)
    print(json.dumps(run_lint(sys.argv[1]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
