#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pattern_rules.py —— 声明式正则规则的通用执行器

规则在 YAML 里长这样：

    - rule_id: SEC-004
      dimension: injection
      severity: blocker
      pattern: '(?:shell\\s*=\\s*True|os\\.system\\s*\\()'
      require: '(?i)subprocess'      # 同一行必须同时出现
      guard:   '(?:#\\s*示例)'        # 出现即放弃（防御性语境）
      unless:  '(?:literal_eval)'    # 出现即放弃（更安全的替代写法）
      confidence: 0.9
      suggestion: "..."

**判定条件全部留在 YAML，执行器只做通用的匹配与去噪。**
这样加一类检查不用改主流程，也让「自动提案」能产出可被真正执行的规则——
而不是一个没人执行的 pattern 字段。

两道去噪是必须的（都是被误报逼出来的）：
1. 注释行与文档字符串：说明里的示例不是实现；
2. 同一行内 require/guard/unless 的组合判断：单靠一条正则太容易张冠李戴。
"""
import re

_COMMENT_PREFIX = ("#", "//", "*", "<!--")
_CODE_SUFFIX = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".cpp", ".c", ".rb")


def _scope(value):
    """把单条正则包成非捕获组；行内 flag 降级为局部 flag。

    `(?i)abc` 直接拼进 `(?:...)|(?:...)` 会报
    "global flags not at the start of the expression"，所以改写成 `(?i:...)`。
    """
    text = str(value)
    m = re.match(r"^\(\?([imsx]+)\)", text)
    if m:
        return f"(?{m.group(1)}:{text[m.end():]})"
    return f"(?:{text})"


def compile_multi(value):
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        return re.compile("|".join(_scope(v) for v in value))
    return re.compile(str(value))


def string_lines(text):
    """文档字符串内部的行号集合（含单行 `\"\"\"...\"\"\"`）。"""
    quotes = ('"""', "'''")
    inside, out = None, set()
    for lineno, line in enumerate(text.splitlines(), 1):
        if inside is not None:
            out.add(lineno)
        i = 0
        while i < len(line):
            if inside is not None:
                idx = line.find(inside, i)
                if idx < 0:
                    break
                i = idx + 3
                inside = None
            else:
                cand = sorted((line.find(q, i), q) for q in quotes if line.find(q, i) >= 0)
                if not cand:
                    break
                pos, q = cand[0]
                close = line.find(q, pos + 3)
                if close >= 0:
                    i = close + 3
                    continue
                inside = q
                out.add(lineno)
                break
        head = line.lstrip()
        if head.startswith(quotes + ('r"""', "r'''", 'f"""', "f'''")):
            out.add(lineno)
    return out


def scan_hits(text, rules, dimension=None, skip_comments=True, target_suffix=""):
    """纯核心：返回 (参与匹配的规则数, [命中])，不碰 Session。

    为什么单独抽出来：`rules.pattern` 工具要用**同一套**判定语义（pattern /
    require / guard / unless 四件套），但工具不该造一个假 Session 去调 `scan`。
    判定条件只在这里实现一次，`scan` 与工具都调它；两处行为一旦分叉，
    「同一份文本，流水线说有命中、工具说没有」这种矛盾就没法解释。

    命中的元素是 dict：rule / lineno / quote / matched / confidence / suggestion。
    """
    str_lines = string_lines(text) if target_suffix.lower() in _CODE_SUFFIX else set()
    total, hits = 0, []
    for rule in rules:
        if not rule.get("pattern"):
            continue
        if dimension and rule.get("dimension") != dimension:
            continue
        total += 1
        pat = compile_multi(rule["pattern"])
        req = compile_multi(rule.get("require"))
        guard = compile_multi(rule.get("guard"))
        unless = compile_multi(rule.get("unless"))
        conf = float(rule.get("confidence") or 0.8)
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or lineno in str_lines:
                continue
            if skip_comments and stripped.startswith(_COMMENT_PREFIX):
                continue
            m = pat.search(line)
            if not m:
                continue
            if req and not req.search(line):
                continue
            if guard and guard.search(line):
                continue
            if unless and unless.search(line):
                continue
            hits.append({
                "rule": rule,
                "lineno": lineno,
                "quote": stripped,
                "matched": (m.group(0) or "").strip()[:60],
                "confidence": conf,
                "suggestion": (rule.get("suggestion")
                               or "按规则要求整改后复核。"),
            })
    return total, hits


def scan(s, text, rules, dimension=None, tool="pattern_rule", skip_comments=True,
         target_suffix=""):
    """执行所有带 `pattern` 的规则，返回 session。

    dimension 为 None 时不筛维度（由调用方保证 rules 已按维度裁剪）。
    实现委托 `scan_hits()`：匹配语义只有一份，这里只负责把命中写进 Session。
    """
    total, hits = scan_hits(text, rules, dimension=dimension,
                            skip_comments=skip_comments,
                            target_suffix=target_suffix)
    added = 0
    for hit in hits:
        rule = hit["rule"]
        if s.add_issue(s.make_issue(
                dim=rule["dimension"],
                rule_id=rule["rule_id"],
                quote=hit["quote"],
                location=f"第 {hit['lineno']} 行",
                evidence=f"{rule.get('desc', '')}｜命中「{hit['matched']}」",
                tool=tool,
                confidence=hit["confidence"],
                suggestion=hit["suggestion"])):
            added += 1
    s.tool(tool, f"{dimension or '全部'}｜声明式规则 {total} 条",
           f"命中 {added} 条", 0, status="success" if total else "skipped")
    return s
