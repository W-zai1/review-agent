#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docx_format_checks.py —— Word 版式合规的确定性检查（FORMAT-001~006）

判定口径（源自课程报告格式要求：正文中文宋体、英文 Times New Roman、小四、
首行缩进 2 字符、固定 22 磅行距、两级或三级标题、图表编号与正文引用一致）：

- 「正文段」= 不在表格内 且 非图表题注 且 非标题段 且 文本 ≥10 字。
- 「标题段」= 显式黑体（eastAsia 含「黑体」）且（字号 ≥22 半磅 或 文本 <60 字）。
- 有效字体/字号：段落内显式属性优先，否则回退主题 minorFont(Hans) / 文档默认字号。

聚合原则：同一条规则的每处偏差聚合为一条 issue（evidence 带计数与分布），
避免 300 条同质问题淹没清单——与 ADVISORY_RULES 的聚合思路一致。
每条 issue：{rule_id, dimension, quote, location, evidence, tool, confidence}。
quote 必须为提取文本的逐字子串（取违规段原文）。
"""
from __future__ import annotations

import re
from collections import Counter

# 合格中文正文字体（宋体族）
OK_EAST = {"宋体", "SimSun", "新宋体", "NSimSun", "宋体-GB2312"}
# 小四 = 12pt = 24 半磅
SZ_XIAOSI = 24
# 首行缩进 2 字符：firstLineChars=200，或 firstLine 落在 400~520 缇（≈2 字符）
FIRST_LINE_OK = (200, lambda v: 400 <= v <= 520)
# 固定 22 磅行距 = 440 缇 + lineRule=exact
LINE_OK = 440
BODY_MIN = 10
HEADING_FONT = "黑体"
HEADING_SZ_MIN = 22
HEADING_SHORT = 40   # 黑体且无大字号的段落，仅当文本很短（<40 字）才算标题，避免误吞正文强调句

CAPTION_RE = re.compile(r"^(图|表)\s*\d")
CAPTION_NUM_RE = re.compile(r"^(图|表)\s*(\d+(?:[-.]\d+)?)")
NUM_RE = re.compile(r"(图|表)\s*(\d+(?:[-.]\d+)?)")
# 题注号后紧跟"是/为/指/表示…"的是正文引用句，不是题注
CAPTION_DENY_TAIL = re.compile(r"^(是|为|指|表示|作|作为|说明|用于|在|与|及|和|，|,)")


def _is_caption(text: str) -> bool:
    """题注判定：行首 图/表+编号；剩余为短行、无句号，且不以"是/为/指…"开头。

    真实题注通常写作「表 1-1　标题」（编号后全角空格）；正文引用句
    「表 2-1 是本情境下…」以"是"开头且往往带句号，不算题注。
    """
    s = text.strip()
    m = CAPTION_NUM_RE.match(s)
    if not m:
        return False
    body = s[m.end():].lstrip(" \u3000\t")
    if not body:
        return True
    if "。" in body or "；" in body:
        return False
    if CAPTION_DENY_TAIL.match(body):
        return False
    return len(body) <= 60


def _is_heading(p) -> bool:
    if p.get("pstyle") in _heading_style_set():
        return True
    if p.get("outline_lvl") is not None:
        return True
    east = p.get("east_set") or set()
    szs = p.get("sz_set") or set()
    if HEADING_FONT not in east:
        return False
    if any(sz >= HEADING_SZ_MIN for sz in szs):
        return True
    return len(p.get("text", "").strip()) < HEADING_SHORT


def _is_body(p) -> bool:
    if p.get("in_table"):
        return False
    if _is_caption(p.get("text", "")):
        return False
    if _is_heading(p):
        return False
    return len(p.get("text", "").strip()) >= BODY_MIN


def _eff_east(p, theme_minor) -> str:
    """有效中文字体：显式 eastAsia 优先，否则主题 minorFont(Hans)。"""
    if p.get("first_east"):
        return p["first_east"]
    if theme_minor:
        return theme_minor
    return "（未显式设置）"


def _eff_sz(p, doc_default_sz) -> int | None:
    return p.get("first_sz") if p.get("first_sz") is not None else doc_default_sz


def _eff_ascii(p, theme_minor) -> str:
    if p.get("first_ascii"):
        return p["first_ascii"]
    return theme_minor or "（未显式设置）"


def _quote(p, limit=160) -> str:
    t = p.get("text", "").strip()
    return t[:limit]


def _issue(rule_id, quote, location, evidence, tool, confidence=0.95, suggestion=None):
    out = {
        "rule_id": rule_id, "dimension": "format",
        "quote": quote, "location": location,
        "evidence": evidence, "tool": tool, "confidence": confidence,
    }
    if suggestion:
        out["suggestion"] = suggestion
    return out


def _fmt_sz(sz) -> str:
    pt = sz / 2 if sz else None
    if sz == 21:
        return "五号(10.5pt)"
    if sz == 24:
        return "小四(12pt)"
    if sz == 22:
        return "11pt"
    return f"{pt:g}pt" if pt else "未知"


def check_font(paras, body, info, ridset):
    if "FORMAT-001" not in ridset:
        return []
    theme = info.get("theme_minor_hans")
    bad = [p for p in body if _eff_east(p, theme) not in OK_EAST]
    if not bad:
        return []
    dist = Counter(_eff_east(p, theme) for p in bad)
    names = "、".join(f"{k}×{v}" for k, v in dist.most_common(6))
    p0 = bad[0]
    return [_issue(
        "FORMAT-001", _quote(p0), f"第 {paras.index(p0) + 1} 段（docx）",
        f"正文 {len(bad)} 段中文字体非宋体（{names}；要求宋体，正文英文 Times New Roman）。"
        f"示例段实际 eastAsia={p0.get('first_east') or '主题继承'}",
        "docx_format:font")]


def check_size(paras, body, info, ridset):
    if "FORMAT-002" not in ridset:
        return []
    dsz = info.get("doc_default_sz")
    bad = [p for p in body if _eff_sz(p, dsz) != SZ_XIAOSI]
    if not bad:
        return []
    dist = Counter(_eff_sz(p, dsz) for p in bad)
    parts = "、".join(f"{_fmt_sz(k)}×{v}" for k, v in sorted(dist.items()))
    p0 = bad[0]
    return [_issue(
        "FORMAT-002", _quote(p0), f"第 {paras.index(p0) + 1} 段（docx）",
        f"正文 {len(bad)} 段字号非小四（12pt）：{parts}。示例段实际 sz={p0.get('first_sz') or '文档默认'}",
        "docx_format:size")]


def check_indent(paras, body, ridset):
    if "FORMAT-003" not in ridset:
        return []
    bad = [p for p in body
           if not (p.get("first_line_chars") == 200
                   or (p.get("first_line") is not None
                       and 400 <= p["first_line"] <= 520))]
    if not bad:
        return []
    p0 = bad[0]
    with_ind = len(body) - len(bad)
    return [_issue(
        "FORMAT-003", _quote(p0), f"第 {paras.index(p0) + 1} 段（docx）",
        f"正文 {len(bad)} 段未设置首行缩进 2 字符（已设 {with_ind} 段，共 {len(body)} 段；"
        f"要求 firstLineChars=200 或 firstLine≈480 缇）。示例段缩进属性为空",
        "docx_format:indent")]


def check_spacing(paras, body, info, ridset):
    if "FORMAT-004" not in ridset:
        return []
    bad = [p for p in body
           if p.get("spacing_line") != LINE_OK
           or p.get("spacing_rule") != "exact"]
    if not bad:
        return []
    dist = Counter((p.get("spacing_line"), p.get("spacing_rule")) for p in bad)
    parts = "、".join(
        f"{k[0] / 20:g}pt×{v}" + ("(auto)" if k[1] != "exact" else "") if k[0] else f"继承默认×{v}"
        for k, v in sorted(dist.items(),
                           key=lambda kv: ((kv[0][0] or 0), kv[0][1] or "")))
    p0 = bad[0]
    return [_issue(
        "FORMAT-004", _quote(p0), f"第 {paras.index(p0) + 1} 段（docx）",
        f"正文 {len(bad)} 段行距不是固定 22 磅（要求 line=440 且 lineRule=exact）：{parts}。"
        f"示例段实际 line={p0.get('spacing_line')} rule={p0.get('spacing_rule')}",
        "docx_format:spacing")]


def check_heading(paras, info, ridset):
    if "FORMAT-005" not in ridset:
        return []
    non_tbl = [p for p in paras if not p.get("in_table")]
    style_levels = sorted({
        int(p["pstyle"][-1]) for p in non_tbl
        if p.get("pstyle") and p["pstyle"] in _heading_style_set()
        and p["pstyle"][-1].isdigit()
    })
    outline_levels = sorted({p["outline_lvl"] for p in non_tbl if p.get("outline_lvl") is not None})
    visual_sizes = sorted({
        max(p["sz_set"]) for p in non_tbl
        if HEADING_FONT in (p.get("east_set") or set()) and p.get("sz_set")
    }, reverse=True)

    if not style_levels and not outline_levels:
        first_head = next((p for p in non_tbl
                           if HEADING_FONT in (p.get("east_set") or set())), None)
        quote = _quote(first_head) if first_head else paras[0].get("text", "")[:60] or "（文档首段）"
        loc = f"第 {paras.index(first_head) + 1} 段（docx）" if first_head else "全文"
        ev = ("未检测到 Word 标题样式（Heading1-3 / 大纲级别）：全文标题仅靠手动黑体与字号区分"
              f"（视觉字号 {visual_sizes} 半磅），机器无法确认标题层级是否满足「两级或三级」。")
        return [_issue(
            "FORMAT-005", quote, loc, ev, "docx_format:heading", 0.8,
            "建议改用标题样式（标题 1/2/3）而不是手动加粗黑体，层级才能被识别与导航")]
    levels = style_levels or outline_levels
    if max(levels) > 2:
        return [_issue(
            "FORMAT-005", paras[0].get("text", "")[:60] or "（文档首段）", "全文",
            f"标题层级达 {max(levels) + 1} 级（样式 {levels}），超过要求的两级或三级",
            "docx_format:heading", 0.9)]
    return []


def _heading_style_set():
    return {
        "Heading1", "Heading2", "Heading3", "Heading4", "Heading5",
        "Heading6", "Heading7", "Heading8", "Heading9",
        "标题1", "标题2", "标题3", "标题4", "标题5",
        "标题6", "标题7", "标题8", "标题9",
    }


def check_figref(paras, text, ridset):
    if "FORMAT-006" not in ridset:
        return []
    captions = []
    for p in paras:
        t = p.get("text", "").strip()
        if _is_caption(t):
            m = NUM_RE.match(t)
            if m:
                captions.append((m.group(1), m.group(2)))
    cap_nums = [(k, n) for k, n in captions]
    dup = [n for k, n in cap_nums
           if cap_nums.count((k, n)) > 1]
    refs = []
    for line in text.split("\n"):
        s = line.strip()
        if _is_caption(s):
            continue
        for m in NUM_RE.finditer(s):
            refs.append((m.group(1), m.group(2)))
    ref_missing = sorted({f"{k}{n}" for k, n in refs if (k, n) not in cap_nums})
    issues = []
    if ref_missing:
        p0 = None
        for p in paras:
            if _is_caption(p.get("text", "")):
                continue
            if any(f"{m.group(1)}{m.group(2)}" in ref_missing
                   for m in NUM_RE.finditer(p.get("text", ""))):
                p0 = p
                break
        quote = _quote(p0) if p0 else text[:120]
        loc = f"第 {paras.index(p0) + 1} 段（docx）" if p0 else "全文"
        issues.append(_issue(
            "FORMAT-006", quote, loc,
            f"正文引用了 {len(ref_missing)} 个无对应题注的图表编号：{'、'.join(ref_missing)}。"
            f"要求图表编号与正文引用一致（题注共 {len(cap_nums)} 个）",
            "docx_format:figref", 0.9,
            "补全缺失题注，或把正文引用改成实际存在的编号"))
    if dup:
        issues.append(_issue(
            "FORMAT-006", _quote(paras[0]), "全文",
            f"存在重复题注编号：{'、'.join(sorted(set(dup)))}。每个图表编号只能出现一次",
            "docx_format:figref", 0.9))
    return issues


def run_all(info, ridset):
    """按 FORMAT 规则清单跑全部确定性检查，返回 issue 列表。"""
    paras = info.get("paragraphs") or []
    body = [p for p in paras if _is_body(p)]
    text = info.get("text") or ""
    issues = []
    issues += check_font(paras, body, info, ridset)
    issues += check_size(paras, body, info, ridset)
    issues += check_indent(paras, body, ridset)
    issues += check_spacing(paras, body, info, ridset)
    issues += check_heading(paras, info, ridset)
    issues += check_figref(paras, text, ridset)
    return issues
