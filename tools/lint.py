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
    """对一个文件跑「外部 linter」+「纯 Python 密钥扫描」两条独立链路。

    两条链路的**状态必须分开报**（决策书 4.8）：外部的 ruff/eslint 可能没装、
    没有执行权限、超时、异常退出，这些都不该让密钥扫描跟着一起消失——
    密钥扫描只依赖内置正则，任何 linter 结局下都必须执行。

    返回 dict 的键（只增不改，调用方按需读）：
      file / status / details / findings / secret_findings  —— 兼容旧口径
      lint_status   —— 外部 linter 轴：ok / issues / skipped / failed
      secret_status —— 密钥扫描轴：  ok / issues / failed
      exit_code     —— 外部程序退出码（没跑到程序时为 None）

    `status` 是综合结论，保持旧词表（ok / issues / skipped / failed）：
      有结构化命中 → issues（命中本身就是结论，linter 失败另见 lint_status）
      无命中但有任一轴失败 → failed（"没跑成"不许被当成"没问题"）
      linter 未执行（没装 / 非代码文件）→ skipped
      其余 → ok
    """
    ext = os.path.splitext(path)[1].lower()
    result = {
        "file": path, "status": "skipped", "details": None, "findings": [],
        "lint_status": "skipped", "secret_status": "skipped", "exit_code": None,
    }

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
        except FileNotFoundError:
            result["lint_status"] = "skipped"
            result["details"] = f"{tool} 未安装，跳过 lint（仍执行密钥扫描）"
        except subprocess.TimeoutExpired:
            result["lint_status"] = "failed"
            result["details"] = f"{tool} 超时（30s），lint 结果不可信（仍执行密钥扫描）"
        except PermissionError as e:
            # Windows 上是 WinError 5「拒绝访问」，POSIX 上是 EACCES；两者都是
            # 「程序在、但跑不起来」，必须与「没装」区分开。
            result["lint_status"] = "failed"
            result["details"] = (
                f"{tool} 无执行权限，lint 未运行（仍执行密钥扫描）：{e}")
        except OSError as e:
            # 其余「起不来」的情形（路径被占用、解释器缺失、参数过长……）
            result["lint_status"] = "failed"
            result["details"] = (
                f"{tool} 无法执行，lint 未运行（仍执行密钥扫描）："
                f"{type(e).__name__}: {e}")
        else:
            result["exit_code"] = r.returncode
            raw = (r.stdout or "")
            if r.stderr:
                raw = (raw + "\n" + r.stderr).strip()
            findings = parser(raw)
            result["findings"].extend(findings)
            if r.returncode in (0, 1):
                # ruff / eslint 都以 1 表示「有告警/错误」，是正常结局，不是崩溃。
                if findings:
                    result["lint_status"] = "issues"
                elif r.returncode == 1:
                    result["lint_status"] = "failed"
                    result["details"] = (
                        f"{tool} 退出码 1 但未解析出结构化告警，lint 结果不可信："
                        f"{(raw.strip() or '无输出')[:400]}")
                    raw = ""
                else:
                    result["lint_status"] = "ok"
                if raw.strip():
                    result["details"] = raw.strip()[:800]
                elif result["lint_status"] == "ok":
                    result["details"] = "无输出"
            else:
                # 退出码 2+ 是工具自身出错（ruff 配置错、eslint 配置错等）：
                # 以前这里会被当 "ok"，等于把「工具崩了」报成「没问题」。
                result["lint_status"] = "failed"
                result["details"] = (
                    f"{tool} 异常退出（退出码 {r.returncode}），lint 结果不可信"
                    + (f"：{raw.strip()[:400]}" if raw.strip() else ""))

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
        secrets = _secret_findings(text)
        result["secret_status"] = "issues" if secrets else "ok"
    except OSError as e:  # 文件读不了：密钥扫描没做成，不能记成「没命中」
        secrets = []
        result["secret_status"] = "failed"
        result["details"] = ((result["details"] or "")
                             + f" / 密钥扫描读取失败：{type(e).__name__}: {e}").strip(" /")
    except Exception as e:  # noqa: BLE001 - 正则引擎自身出错也不能让审查崩掉
        secrets = []
        result["secret_status"] = "failed"
        result["details"] = ((result["details"] or "")
                             + f" / 密钥扫描异常：{type(e).__name__}: {e}").strip(" /")
    result["findings"].extend(secrets)
    result["secret_findings"] = secrets

    if result["findings"]:
        result["status"] = "issues"
    elif "failed" in (result["lint_status"], result["secret_status"]):
        result["status"] = "failed"
    elif result["lint_status"] == "skipped":
        result["status"] = "skipped"
    else:
        result["status"] = "ok"
    return result


def main():
    if len(sys.argv) < 2:
        print("usage: lint.py <file>")
        sys.exit(1)
    print(json.dumps(run_lint(sys.argv[1]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
