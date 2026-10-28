#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""格式规格（format spec）：告诉格式检查器"这一次按哪套版式标准判"。

**解决的问题**：`rules/format.yaml` 是长期裁判尺度（「要检查哪些维度」），
但「正文必须宋体小四、固定 22 磅行距」是**某一份委托方的格式要求**。
上一版把这份课程报告要求的数值硬编码在 `tools/docx_format_checks.py` 里，
后果是：换一份标准（期刊三号仿宋、1.5 倍行距）就得改 Python，
规则库还会越攒越多套互相矛盾的标准，最后没人知道该选哪个。

**边界（很重要）**：
1. 规格只提供**数值**，不新增也不删除"要查什么"的能力。
   规格里没写的那一项，就是这一份标准不关心——检查跳过，并写进报告 limitations。
2. 选 `none`（或不选）时，字体/字号/缩进/行距/标题层级五项**不参与**，
   而不是"按某套默认值判"。没有委托方标准时判任何一项都是瞎猜。
3. 未知规格一律**报错**，不静默回退到默认。静默回退最难查：
   用户以为按投稿须知判的，实际按课程作业模板判的。
4. 每份报告记录 `format_spec{id,name,version,source}`，
   半年后能回答「这份报告是按哪套标准判的」。

配置：config/format_specs.yaml（版本化；改动要升 version）
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPECS_PATH = ROOT / "config" / "format_specs.yaml"

# check 里允许出现的键：写错键名等于"配了不生效"，必须当配置报错
_KNOWN_KEYS = {
    "body_font_east", "body_size_halfpt",
    "first_line_chars", "first_line_twips_min", "first_line_twips_max",
    "line_twips", "line_rule",
    "heading_font", "heading_min_size_halfpt", "heading_max_level",
    "figref",
}


class FormatSpecError(Exception):
    """格式规格配置/选择有问题——一律让审查失败，不做静默兜底。"""


def _clean_list(raw, field, sid):
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FormatSpecError(f"规格 {sid} 的 {field} 必须是列表")
    return [str(x).strip() for x in raw if str(x).strip()]


def _check_dict(raw, sid):
    """校验 check 块：键必须在白名单内，值类型要对。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FormatSpecError(f"规格 {sid} 的 check 必须是对象")
    out = {}
    for k, v in raw.items():
        k = str(k)
        if k not in _KNOWN_KEYS:
            raise FormatSpecError(
                f"规格 {sid} 的 check 里有未知字段 {k!r}；"
                f"可用：{'、'.join(sorted(_KNOWN_KEYS))}")
        if k == "body_font_east":
            out[k] = _clean_list(v, k, sid)
            if not out[k]:
                raise FormatSpecError(f"规格 {sid} 的 body_font_east 不能为空列表")
        elif k == "line_rule":
            v = str(v).strip()
            if v not in ("exact", "auto"):
                raise FormatSpecError(f"规格 {sid} 的 line_rule 只能是 exact / auto")
            out[k] = v
        elif k == "figref":
            if not isinstance(v, bool):
                raise FormatSpecError(f"规格 {sid} 的 figref 必须是 true/false")
            out[k] = v
        elif k == "heading_font":
            out[k] = str(v).strip()
            if not out[k]:
                raise FormatSpecError(f"规格 {sid} 的 heading_font 不能为空")
        else:
            # 数值项：必须是正整数（半磅/缇/字符×100 都不可能是 0 或小数）
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise FormatSpecError(f"规格 {sid} 的 {k} 必须是正整数，收到 {v!r}")
            out[k] = v
    # 区间成对出现，否则判定会退化成"只判下限"这种半吊子
    lo, hi = "first_line_twips_min", "first_line_twips_max"
    if (lo in out) != (hi in out):
        raise FormatSpecError(f"规格 {sid} 的 {lo}/{hi} 必须成对出现")
    if lo in out and out[lo] > out[hi]:
        raise FormatSpecError(f"规格 {sid} 的 {lo} 大于 {hi}")
    return out


def load():
    """读取并校验格式规格配置。结构不对就抛错，不返回半成品。"""
    try:
        raw = SPECS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FormatSpecError(f"缺少格式规格配置：{SPECS_PATH.name}")
    try:
        data = yaml.safe_load(raw) or {}
    except Exception as e:
        raise FormatSpecError(f"格式规格配置无法解析：{e}")
    if not isinstance(data, dict):
        raise FormatSpecError("格式规格配置顶层必须是对象")

    items = data.get("specs")
    if not isinstance(items, list) or not items:
        raise FormatSpecError("格式规格配置里没有任何规格（specs 必须是非空列表）")

    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            raise FormatSpecError("specs 里每一项都必须是对象")
        sid = str(it.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,60}", sid):
            raise FormatSpecError(f"规格 id 不合法：{sid!r}（只允许小写字母/数字/点/下划线/连字符）")
        if sid in seen:
            raise FormatSpecError(f"规格 id 重复：{sid}")
        seen.add(sid)
        out.append({
            "id": sid,
            "name": str(it.get("name") or sid).strip(),
            "source": str(it.get("source") or "").strip(),
            "desc": " ".join(str(it.get("desc") or "").split()),
            "check": _check_dict(it.get("check"), sid),
            "disable_rules": _clean_list(it.get("disable_rules"), "disable_rules", sid),
        })

    default = str(data.get("default") or out[0]["id"]).strip()
    if default not in seen:
        raise FormatSpecError(f"default 指向不存在的规格：{default}")
    return {"version": str(data.get("version") or "").strip(),
            "default": default, "specs": out}


def get(sid):
    """按 id 取规格；不存在返回 None（调用方决定报错还是用默认值）。"""
    if not sid:
        return None
    for s in load()["specs"]:
        if s["id"] == sid:
            return s
    return None


def public_list():
    """给前端的清单：带上"会查哪几项"，让人一眼知道选它意味着什么。"""
    data = load()
    labels = {
        "body_font_east": "正文字体",
        "body_size_halfpt": "正文字号",
        "first_line_chars": "首行缩进",
        "first_line_twips_min": "首行缩进",
        "line_twips": "行距",
        "line_rule": "行距",
        "heading_font": "标题字体",
        "heading_min_size_halfpt": "标题字号",
        "heading_max_level": "标题层级",
        "figref": "图表编号",
    }
    items = []
    for s in data["specs"]:
        checks = []
        for k in s["check"]:
            lab = labels.get(k)
            if lab and lab not in checks:
                checks.append(lab)
        items.append({
            "id": s["id"],
            "name": s["name"],
            "source": s["source"],
            "desc": s["desc"],
            "checks": checks,
            "disabled_count": len(s["disable_rules"]),
            "disabled_rules": s["disable_rules"],
        })
    return {"ok": True, "version": data["version"],
            "default": data["default"], "items": items}


def apply(sid, rules):
    """按格式规格过滤规则，返回规格内容 + 过滤结果 + 记账。

    返回：{id,name,source,desc,version,check,rules[],skipped[],limitations[]}
    未知规格 → 抛 FormatSpecError（不静默降级）。
    """
    data = load()
    sp = next((s for s in data["specs"] if s["id"] == sid), None)
    if sp is None:
        opts = "、".join(s["id"] for s in data["specs"])
        raise FormatSpecError(f"未知格式规格：{sid}；可选：{opts}")

    disable = set(sp["disable_rules"])
    kept, skipped, hit = [], [], set()
    for r in rules:
        rid = str(r.get("rule_id") or "")
        if rid in disable:
            skipped.append({"rule_id": rid,
                            "dimension": str(r.get("dimension") or ""),
                            "desc": str(r.get("desc") or "")[:160],
                            "reason": "格式规格不判此项"})
            hit.add(rid)
        else:
            kept.append(r)

    limitations = []
    if sp["id"] == "none":
        limitations.append(
            "未提供格式规格：本报告**不判断**字体、字号、首行缩进、行距、标题层级"
            "（这些没有通用的正确答案，必须由委托方的格式要求决定）。"
            "「未报出版式问题」不等于版式合规。")
    else:
        unchecked = [x for x in ("字体", "字号", "首行缩进", "行距", "标题层级", "图表编号")]
        got = set()
        c = sp["check"]
        if "body_font_east" in c:
            got.add("字体")
        if "body_size_halfpt" in c:
            got.add("字号")
        if "first_line_chars" in c or "first_line_twips_min" in c:
            got.add("首行缩进")
        if "line_twips" in c or "line_rule" in c:
            got.add("行距")
        if "heading_max_level" in c:
            got.add("标题层级")
        if c.get("figref"):
            got.add("图表编号")
        miss = [x for x in unchecked if x not in got]
        if miss:
            limitations.append(
                f"所选格式规格「{sp['name']}」未包含 {'、'.join(miss)} 的判定，"
                f"这些项本次未检查（不是「已合规」）。")

    # 配置里写了、但当前规则库中不存在的 ID：可能规则被改名/删除。
    # 不报错（不影响本次审查），但必须让人看见。
    unmatched = sorted(disable - hit)
    if unmatched:
        limitations.append(
            f"格式规格「{sp['name']}」配置了 {len(unmatched)} 条当前规则库中不存在的规则："
            f"{'、'.join(unmatched)}——请核对 config/format_specs.yaml。")

    return {
        "id": sp["id"],
        "name": sp["name"],
        "source": sp["source"],
        "desc": sp["desc"],
        "version": data["version"],
        "check": sp["check"],
        "rules": kept,
        "skipped": skipped,
        "unmatched": unmatched,
        "limitations": limitations,
    }
