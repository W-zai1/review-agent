#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks.py —— 安全审查 Agent 的确定性检查器

设计原则（沿用主项目的三条硬约束）：

1. **判定条件留在 YAML，代码只做通用匹配**。`rules/security.yaml` 里写
   pattern / require / guard / unless，这一层只负责逐行匹配、去噪、产出 issue。
   加一条规则不用改代码，也就不会出现「同一个引擎在三份报告里行为不同」。
2. **quote 必须逐字来自原文**。这里一律摘原文的整行（去首尾空白），
   不做任何改写；改写了下游就无法回查。
3. **宁可少报，不可乱报**。误报会让人放弃整份清单，所以每条规则都带 guard：
   注释掉的示例、占位符、安全写法一律不算命中。

> 这一层不做语义判断（「这段代码是否真的可被利用」需要人判断）。
> 它保证的是：**命中的每一条都确定存在那个写法**，可复算、可复现。
"""
import re
import sys
import time
from pathlib import Path

# 允许 checks.py 直接引用项目工具（它与主流程共享同一套抽取/去噪实现）
_TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

import pattern_rules  # noqa: E402
import regex_check  # noqa: E402

# 占位符/示例值不算泄露：这类写法在文档与模板里到处都是
_PLACEHOLDER = re.compile(
    r"(?i)(?:^xxx+$|your[_-]?\w*|<[^>]{1,40}>|\{\{|\$\{|\bexample\b|placeholder|"
    r"changeme|dummy|^test|^\*\*\*|^\.\.\.|^null$|^none$|^0+$)")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----")
_URL_CRED = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^/\s:@]{2,}:[^/\s@]{2,}@")
_DEP_FILE = re.compile(
    r"(?i)^(requirements.*\.txt|constraints.*\.txt|pyproject\.toml|package\.json|"
    r"Pipfile|go\.mod|environment\.ya?ml)$")
_DEP_URL = re.compile(r"(?:git\+|https?://|svn\+|hg\+)")


def _emit(s, dim, rule, line, lineno, evidence, confidence=None):
    """按「整行原文」产出一条 issue —— 不改写、不留空白行。"""
    quote = line.strip()
    if not quote:
        return False
    return bool(s.add_issue(s.make_issue(
        dim=dim,
        rule_id=rule["rule_id"],
        quote=quote,
        location=f"第 {lineno} 行",
        evidence=evidence,
        tool="security.scan",
        confidence=confidence if confidence is not None else 0.8,
        suggestion=rule.get("suggestion") or "按规则要求整改后复核。",
    )))


def _scope(value):
    """把单条正则包成非捕获组；行内 flag 必须降级为局部 flag。

    `(?i)abc` 直接拼进 `(?:...)|(?:...)` 会报
    "global flags not at the start of the expression"——所以要把
    `(?i)` 改写成 `(?i:...)`，否则一条带 i 的规则就会让整个维度崩掉。
    """
    text = str(value)
    m = re.match(r"^\(\?([imsx]+)\)", text)
    if m:
        return f"(?{m.group(1)}:{text[m.end():]})"
    return f"(?:{text})"


def check_patterns(s, text, rules, ctx):
    """注入 / 配置两类规则：交给通用声明式执行器（与文档审共用同一实现）。

    以前这里自带一套正则引擎，文档审 agent 却完全没有执行器——
    结果「自动挖掘的规则」在文档审里是死数据。两个 Agent 共用一份实现，
    也就不会出现「同一份规则在两处匹配结果不同」。
    """
    return pattern_rules.scan(
        s, text, rules, dimension=ctx.get("dimension"), tool="security.scan",
        target_suffix=str(ctx.get("target_path") or ""))


def check_secret(s, text, rules, ctx):
    """凭据检查：明文口令 / 私钥块 / URL 内嵌凭据。

    复用 regex_check.SECRET_RE（前几轮对抗测试已把「无引号、短值、纯数字、
    中文键名」都补过），这里只加一层占位符豁免，避免把模板里的
    `password = "your_password"` 报成泄露。
    """
    by_id = {r["rule_id"]: r for r in rules}
    r1, r2 = by_id.get("SEC-001"), by_id.get("SEC-002")
    started = time.perf_counter()
    hits = 0
    for lineno, line in enumerate(text.splitlines(), 1):
        if r1:
            for m in regex_check.SECRET_RE.finditer(line):
                raw = m.group(0)
                if regex_check._is_placeholder(raw):
                    continue
                tail = raw.split("=", 1)[-1].split(":", 1)[-1].strip().strip("\"'")
                if _PLACEHOLDER.search(tail) or len(tail) < 5:
                    continue
                if _emit(s, "secret", r1, line, lineno,
                         f"命中明文凭据「{raw.split('=')[0].strip()} = ***」"
                         f"（值长度 {len(tail)}，已脱敏）", 0.9):
                    hits += 1
                break           # 一行只报一次，避免同一行重复计入
        if r2:
            if _PRIVATE_KEY.search(line):
                if _emit(s, "secret", r2, line, lineno, "命中私钥块（PRIVATE KEY）", 0.95):
                    hits += 1
                continue
            m = _URL_CRED.search(line)
            if m and not _PLACEHOLDER.search(m.group(0)):
                if _emit(s, "secret", r2, line, lineno,
                         "URL 中内嵌用户名与口令", 0.85):
                    hits += 1
    s.tool("security.scan", "secret｜凭据检查（含注释）",
           f"命中 {hits} 条", (time.perf_counter() - started) * 1000, status="success")
    return s


def check_dependency(s, text, rules, ctx):
    """依赖检查：只在依赖清单上生效（requirements/pyproject/package.json…）。

    对非依赖文件不猜——把业务代码里的 `>=` 当版本号是典型的误报来源。
    """
    path = ctx.get("target_path")
    name = Path(path).name if path else ""
    started = time.perf_counter()
    if not _DEP_FILE.match(name):
        s.tool("security.scan", "dependency",
               f"跳过：{name or '未知文件'} 不是依赖清单", 0,
               status="skipped")
        return s

    by_id = {r["rule_id"]: r for r in rules}
    r12, r13 = by_id.get("SEC-012"), by_id.get("SEC-013")
    hits = 0
    is_manifest = name.lower() in ("package.json", "pyproject.toml", "pipfile", "go.mod")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "-r", "-c", "--")):
            continue
        if r13 and _DEP_URL.search(stripped):
            if _emit(s, "dependency", r13, line, lineno,
                     "从 URL / git 直接安装，来源与内容无法用版本号锁定", 0.85):
                hits += 1
            continue
        if not r12 or is_manifest:
            continue            # 清单型文件用版本区间是正常的，只查来源
        pkg = re.split(r"[<>=!~;\[\s]", stripped, maxsplit=1)[0]
        if not pkg or pkg.startswith(("-", "git+", "http")):
            continue
        if "==" not in stripped and "===" not in stripped:
            if _emit(s, "dependency", r12, line, lineno,
                     f"依赖「{pkg}」未用 == 锁定版本（当前写法允许跨次安装到不同版本）", 0.8):
                hits += 1
    s.tool("security.scan", f"dependency｜{name}",
           f"命中 {hits} 条", (time.perf_counter() - started) * 1000, status="success")
    return s


CHECKERS = {
    "secret": check_secret,
    "injection": check_patterns,
    "config": check_patterns,
    "dependency": check_dependency,
}
