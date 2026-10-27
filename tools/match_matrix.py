#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""match_matrix.py —— 「要求侧 vs 候选侧」逐项对照矩阵

## 为什么单独一层

目的文件（招聘 JD / 招标要求 / 客户检查清单）和待审对象（简历 / 投标文件）
是**两份不同的文本**。它们各自在报告里都看得见，但没有任何一处把它们逐项对上：

    JD：「本科及以上学历，熟悉 Python、SQL」
    简历：「教育背景：XX 大学 硕士」「技能：python、Excel」

`purpose.analyze` 会把这两句都定位成证据（support），可是「学历这一项对上了没有」
「要求的三项技能简历里有没有」仍然要人自己一行行比。矩阵就是把这件事做成一张表。

## 判定权限（与项目原则一致：误报的代价高于漏报）

| 情形 | 结论 | 为什么 |
|---|---|---|
| 两侧都能解析出学历层级 / 年限数字 | `met` / `unmet` | 这是**算术与集合比较**，交代码 |
| 要求侧写了，候选侧字面没出现 | `absent`（未提及） | 「没写」≠「不具备」，不能判不满足 |
| 只有一侧能给结构化值 | `unclear` | 结构不够，交给人看 |
| 薪资、到岗这类区间/时点 | `mentioned` | 谁满足谁要谈，代码不下结论 |

**它不产生 issue、不改 verdict**。矩阵是"对照表"，不是"判定"——
和 `tools/arith_check.py` 的关系也一样：错的是"算"，不是"判"。

## 抽取靠结构，不靠词表

- 学历：层级词（博士/硕士/本科/大专…）→ 整数比大小；
- 年限：只认「N 年…经验/经历」的显式写法，**不按在职区间推算**
  （区间累计要拿"今天"当锚点，重跑报告会漂移，和「报告可复现」冲突）；
- 技能：引导词（熟悉/精通/掌握/技能）后的**列举段**才收；中文项必须落在列举结构里，
  否则「熟悉业务流程」会被当成一项技能，然后报一个假缺口。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

from arith_check import sentences  # noqa: E402

CFG_PATH = ROOT / "config" / "match_matrix.yaml"

STATUS_CN = {
    "met": "✅ 满足",
    "unmet": "❌ 不满足",
    "absent": "— 未提及",
    "partial": "△ 部分未提及",
    "unclear": "? 无法判定",
    "mentioned": "· 双方均提及",
}

# 组装目的文本时，每个目的文件都以这个标记开头（server._assemble_purpose），
# 且**独占一行**：`【目的文件：名】\n正文`。
#
# 块标记只有**一条**路径：行首锚定 + 贪婪取名。
#
# 不保留「全文匹配」的回退，原因是它会把**下一行正文当成文件名**：
#   "【目的文件：\n核对学历】看这里" → name="核对学历"
# 于是手写/正文被认成要求侧，还带着一个并不存在的文件名——这正是"误报优先"
# 要避免的形态。没有行首标记就是没有要求文件，如实报"未生成矩阵：没上传要求文件"，
# 比编一个块出来安全。
LINE_MARK = re.compile(r"(?m)^[ \t]*【目的文件：([^\n]*)】")
# 组装尾巴（server._assemble_purpose 生成的截断说明）：
#   （说明：《名》超过 2000 字，已截断至前 2000 字；组合目的文本超过 6000 字，已整体截断）
#   （说明：文件名含【】《》等目的块定界符，标记内改写为《名》）
# 只认这几种固定开头，正文里自然写「（说明：…）」不能被误删。
TAIL_NOTE = re.compile(
    r"\n?\s*（说明：(?:《[^）]*》超过\s*\d+\s*字"
    r"|组合目的文本超过\s*\d+\s*字"
    r"|文件名含【】《》等目的块定界符，标记内改写为《[^）]*》)[^）]*）\s*$")

_CACHE = {"key": None, "data": None}

LEVEL_NAME = {5: "博士", 4: "硕士", 3: "本科", 2: "大专", 1: "高中/中专"}


# ---------------------------------------------------------------- 配置

def load_config(path=None):
    p = Path(path) if path else CFG_PATH
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"version": "", "dims": [], "max_items": 10}
    key = (str(p), mtime)
    if _CACHE["key"] == key:
        return _CACHE["data"]
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data.get("dims"), list):
        data["dims"] = []
    data.setdefault("version", "")
    data.setdefault("max_items", 10)
    _CACHE["key"] = key
    _CACHE["data"] = data
    return data


def config_version(cfg=None):
    return str((cfg or load_config()).get("version") or "")


def _patterns(value):
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in (value or []) if v]


# ---------------------------------------------------------------- 文本工具

def blocks(purpose_text):
    """把目的文本切成 [{"name","text"}]，只保留**目的文件**那几段。

    手写的那段（用户自己写的指令）不是要求侧：它是"要审什么"，
    要求侧是上传的那份 JD / 检查清单。两者混在一起会让矩阵把指令当要求。

    两个输入规范化，都是为了"别把小毛病变成判定差异"：
      · CRLF → LF：Windows 上传的文本会在每行留 `\\r`，进到要求字符串里
        就是隐形字符（正则按 `$` 匹配时会挡路）；
      · 空名字 → 「未命名」：与上传侧同一措辞，避免出现 name="" 的块。
    """
    text = (purpose_text or "").replace("\r\n", "\n").replace("\r", "\n")
    marks = list(LINE_MARK.finditer(text))
    if not marks:
        return []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = TAIL_NOTE.sub("", text[m.end():end])
        out.append({"name": m.group(1).strip() or "未命名", "text": body})
    return out


def _sent_at(text, pos):
    """取 pos 所在的整句（用于 quote：必须是原文子串，不能改写）。"""
    start = max(text.rfind("\n", 0, pos), text.rfind("。", 0, pos),
                text.rfind("；", 0, pos), text.rfind("！", 0, pos),
                text.rfind("？", 0, pos))
    start = start + 1 if start >= 0 else 0
    ends = [text.find(ch, pos) for ch in "。；！？\n"]
    ends = [e for e in ends if e >= 0]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end].strip()


def _clip(s, limit=120):
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= limit else s[:limit] + "…"


def _first(patterns, text):
    """按顺序找第一个命中；返回 (match, pattern_index)。"""
    for idx, pat in enumerate(patterns):
        try:
            rx = re.compile(pat)
        except re.error:
            continue
        m = rx.search(text)
        if m:
            return m, idx
    return None, -1


# ---------------------------------------------------------------- 各 kind 的比对

def _level_word(text, pattern, levels):
    """返回 (层级整数, 命中的层级词, 位置)。取**最低**层级词：
    JD 里常同时写「本科及以上，硕士优先」，取最低才不会把合格的判成不满足。"""
    try:
        rx = re.compile(pattern)
    except re.error:
        return None
    hits = [(levels[m.group(0)], m.group(0), m.start()) for m in rx.finditer(text)
            if m.group(0) in levels]
    if not hits:
        return None
    best = min(hits, key=lambda x: (x[0], x[2]))
    return best


def _cand_level(text, pattern, levels):
    """候选侧取**最高**学历（人可能有多个学历，最高那个才算数）。"""
    try:
        rx = re.compile(pattern)
    except re.error:
        return None
    hits = [(levels[m.group(0)], m.group(0), m.start()) for m in rx.finditer(text)
            if m.group(0) in levels]
    if not hits:
        return None
    return max(hits, key=lambda x: (x[0], -x[2]))


def _years(text, patterns):
    """返回 [(年限下界, 原始片段, 位置)]。区间（1-3 年）取**下界**：
    要求侧取下界＝门槛最低，候选侧取下界＝保守，两侧都不会造出假 unmet。"""
    out = []
    for pat in patterns:
        try:
            rx = re.compile(pat)
        except re.error:
            continue
        for m in rx.finditer(text):
            try:
                lo = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 0 <= lo <= 40:
                out.append((lo, m.group(0), m.start()))
    return out


# 英文虚词：混排文本里会出现在列举位置上，但不是技能名
ASCII_STOP = {"and", "or", "the", "with", "for", "to", "of", "in", "on", "a", "an"}


def _skill_items(dim, req_text, cand_text, limit):
    """从要求侧的**列举结构**里抽技能项，逐项看候选侧原文有没有出现。

    **只收拉丁字母工具名**（Python / SQL / Excel / Figma / Spring Boot）。
    中文技能名刻意不收：靠结构分不出名词与动词短语——
    「负责模型调用、检索」里的「调用」「检索」长得和「数据分析」一模一样，
    收进来就会在真实 JD 上造出「未出现：调用失數」这种假缺口（实测踩到）。
    宁可漏掉中文技能名，也不能让整张表失去可信度。

    段落边界 = 标点或**下一个引导词**：
      「熟练使用 Python、SQL，熟悉业务流程」→ 段只到"SQL"，不会把"业务流程"收进来。
    """
    # 段落边界 = 标点或**下一个引导词**：
    #  「熟练使用 Python、SQL，熟悉业务流程」→ 段只到"SQL"，不会把"业务流程"收进来。
    # 半角逗号不能当段尾：英文逗号分隔（Python, SQL, Excel）是技能列表常见写法，
    # 放进 stop 会把列表截成只剩第一项（2026-10-26 暴力测试踩到）。
    try:
        lead = re.compile(dim["lead"])
    except (KeyError, re.error):
        return []
    try:
        ascii_rx = re.compile("^" + dim["ascii"])
    except (KeyError, re.error):
        return []
    stop_rx = re.compile(r"[，；;。\n]|(?:" + str(dim.get("lead") or "") + ")")

    items, seen = [], set()
    for _start, seg in sentences(req_text):
        for m in lead.finditer(seg):
            tail = seg[m.end():]
            cut = stop_rx.search(tail)
            if cut:
                tail = tail[:cut.start()]
            for part in re.split(r"[、/,|，,]|和|与|及", tail):
                p = part.strip(" 　:：。.、,（）()【】[]\"'")
                if not p:
                    continue
                am = ascii_rx.match(p)
                token = am.group(0) if am else ""
                if not token or len(token) < 2 or token.lower() in ASCII_STOP:
                    break       # 不是工具名 → 列举到此为止
                if token.lower() in seen:
                    continue
                seen.add(token.lower())
                items.append({"name": token, "present": False, "where": "", "hit": ""})
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    lowered = (cand_text or "").lower()
    for it in items:
        key = it["name"].lower()
        pos = lowered.find(key)
        if pos >= 0:
            it["present"] = True
            # 用候选侧**原文**的写法回填片段（大小写以原文为准）
            orig = cand_text[pos:pos + len(it["name"])]
            it["where"] = _clip(_sent_at(cand_text, pos))
            it["hit"] = orig
    return items


def _match_dim(dim, blks, target_text, cfg):
    """一个维度 → 一行矩阵。要求侧没写这一项时返回 None（不占行）。"""
    kind = dim.get("kind") or "mention"
    name = dim.get("name") or dim.get("id") or ""
    limit = int(cfg.get("max_items") or 10)

    req_hit, req_src, req_quote = None, "", ""
    for blk in blks:
        if kind == "level":
            got = _level_word(blk["text"], dim.get("req") or "", dim.get("levels") or {})
        elif kind == "years":
            ys = _years(blk["text"], _patterns(dim.get("req")))
            got = min(ys, key=lambda x: (x[0], x[2])) if ys else None
        elif kind == "items":
            items = _skill_items(dim, blk["text"], target_text, limit)
            got = items or None
        else:
            got = _first(_patterns(dim.get("req")), blk["text"])[0]
        if got:
            req_hit, req_src = got, blk["name"]
            if kind == "items":
                req_quote = ""
            else:
                pos = got[2] if isinstance(got, tuple) and len(got) > 2 else got.start()
                req_quote = _clip(_sent_at(blk["text"], pos))
            break
    if not req_hit:
        return None

    row = {"id": dim.get("id"), "name": name, "kind": kind,
           "status": "unclear", "requirement": "", "req_src": req_src,
           "req_quote": req_quote, "candidate": "", "cand_start": -1,
           "note": "", "items": []}

    # -------------------------------------------------- 学历：比层级
    if kind == "level":
        levels = dim.get("levels") or {}
        req_lvl, req_word, _ = req_hit
        lvl_name = LEVEL_NAME.get(req_lvl, str(req_lvl))
        row["requirement"] = req_word if req_word == lvl_name else f"{req_word}（{lvl_name}）"
        cand = _cand_level(target_text, dim.get("cand") or "", levels)
        if not cand:
            row["status"] = "absent"
            row["note"] = f"要求「{req_word}」，候选侧未出现任何学历层级表述"
            return row
        cand_lvl, cand_word, cand_pos = cand
        row["candidate"] = cand_word
        row["cand_start"] = cand_pos
        row["note"] = f"要求 {lvl_name}，候选侧最高为 {cand_word}"
        row["status"] = "met" if cand_lvl >= req_lvl else "unmet"
        return row

    # -------------------------------------------------- 年限：比数字
    if kind == "years":
        need, raw, _ = req_hit
        row["requirement"] = f"{need} 年（原文「{_clip(raw, 40)}」）"
        cand_ys = _years(target_text, _patterns(dim.get("cand")))
        if not cand_ys:
            m, _ = _first([dim["range"]] if dim.get("range") else [], target_text)
            if m:
                row["status"] = "unclear"
                row["candidate"] = _clip(m.group(0), 40)
                row["note"] = ("候选侧只用「在职区间」表述，没有「N 年经验」字样；"
                               "本工具不按区间推算年限（会随运行日期漂移），需人工核算")
            else:
                row["status"] = "absent"
                row["note"] = f"要求 {need} 年，候选侧未出现年限表述"
            return row
        got, craw, cpos = max(cand_ys, key=lambda x: (x[0], -x[2]))
        row["candidate"] = _clip(craw, 40)
        row["cand_start"] = cpos
        row["status"] = "met" if got >= need else "unmet"
        row["note"] = f"要求 {need} 年，候选侧自述 {got} 年"
        if len({y[0] for y in cand_ys}) > 1:
            row["note"] += "（候选侧出现多个年限，按最高的计）"
        return row

    # -------------------------------------------------- 技能：逐项看字面
    if kind == "items":
        items = req_hit
        row["items"] = items
        row["requirement"] = "、".join(i["name"] for i in items)
        hit = [i for i in items if i["present"]]
        miss = [i for i in items if not i["present"]]
        row["candidate"] = "、".join(i["name"] for i in hit) or ""
        row["cand_start"] = -1
        if miss and hit:
            row["status"] = "partial"
        elif miss:
            row["status"] = "absent"
        else:
            row["status"] = "met"
        row["note"] = (f"要求列举 {len(items)} 项，候选侧原文出现 {len(hit)} 项"
                       + (f"；未出现：{'、'.join(i['name'] for i in miss)}" if miss else "")
                       + "（出现≠掌握，只说明字面写过）")
        return row

    # -------------------------------------------------- 薪资/到岗：只对照
    row["requirement"] = f"提及「{_clip(req_hit.group(0), 20)}」"
    m, _ = _first(_patterns(dim.get("cand")), target_text)
    if m:
        row["status"] = "mentioned"
        row["candidate"] = _clip(m.group(0), 40)
        row["cand_start"] = m.start()
        row["note"] = "双方均提及，需人工比对具体数值/时间（代码不判谁满足谁）"
    else:
        row["status"] = "absent"
        row["note"] = "要求侧提及，候选侧原文未出现相关表述"
    return row


# ---------------------------------------------------------------- 对外入口

def build(purpose_text, target_text, cfg=None):
    """构建对照矩阵。

    返回 dict（available=False 时 `reason` 说明为什么没有矩阵）。
    **不抛异常**：矩阵是附加信息，坏了不能连带审查失败。
    """
    cfg = cfg or load_config()
    out = {
        "available": False,
        "reason": "",
        "config_version": config_version(cfg),
        "requirement_source": "",
        "candidate_source": "",
        "dims": [],
        "coverage": {},
        "gaps": [],
        "limitations": [],
    }
    try:
        blks = blocks(purpose_text)
        if not blks:
            out["reason"] = ("本次目的没有上传要求侧文件（目的文件），无法做逐项对照；"
                             "把 JD / 招标要求 / 检查清单作为「目的文件」上传后才会生成矩阵。")
            return out
        if not (target_text or "").strip():
            out["reason"] = "待审对象为空，没有可对照的候选侧。"
            return out

        rows = []
        for dim in cfg.get("dims") or []:
            try:
                row = _match_dim(dim, blks, target_text, cfg)
            except Exception:                       # noqa: BLE001 单维异常不拖垮整表
                continue
            if row:
                rows.append(row)
        if not rows:
            out["reason"] = ("目的文件里没有识别到可比对的要求项"
                             "（学历 / 年限 / 技能 / 薪资 / 到岗）——"
                             "要求若是散文式描述，矩阵不会生成，结论仍走目标证据那一层。")
            return out

        cover = {k: 0 for k in STATUS_CN}
        for r in rows:
            cover[r["status"]] = cover.get(r["status"], 0) + 1
        cover["total"] = len(rows)

        gaps = []
        for r in rows:
            if r["status"] in ("unmet", "absent", "partial", "unclear"):
                gaps.append(f"{r['name']}：{STATUS_CN.get(r['status'], r['status'])}"
                            + (f"——{r['note']}" if r.get("note") else ""))

        srcs = sorted({r["req_src"] for r in rows if r["req_src"]})
        out.update({
            "available": True,
            "requirement_source": " + ".join(srcs),
            "candidate_source": "待审对象",
            "dims": rows,
            "coverage": cover,
            "gaps": gaps,
        })
        lim = ["矩阵只对照学历 / 年限 / 技能 / 薪资 / 到岗五类「可结构化」项；"
               "软性要求（沟通、抗压、责任心）无法结构化，不在矩阵内。",
               "「未提及」只表示候选侧原文没有这些字面表述，「不等于不具备」。"]
        if any(r["kind"] == "items" for r in rows):
            lim.append("技能项只对照拉丁字母工具名（Python、SQL…）；"
                       "中文技能名无法靠结构区分名词与动词短语，不做字面匹配。")
            lim.append("技能项按字面出现判断：出现≠掌握，缺项≠不会。")
        lim.append("矩阵只做对照，不产生 issue、不影响 verdict；"
                   "判定仍由规则与审查维度给出。")
        out["limitations"] = lim
        return out
    except Exception as e:                          # noqa: BLE001
        out["reason"] = f"矩阵生成失败（不影响审查结论）：{str(e)[:120]}"
        return out


def summarize(matrix):
    """一行摘要，给日志/报告抬头用。"""
    if not matrix or not matrix.get("available"):
        return ""
    cov = matrix.get("coverage") or {}
    return (f"对照 {cov.get('total', 0)} 项：满足 {cov.get('met', 0)}、"
            f"不满足 {cov.get('unmet', 0)}、部分 {cov.get('partial', 0)}、"
            f"未提及 {cov.get('absent', 0)}、需确认 {cov.get('unclear', 0) + cov.get('mentioned', 0)}")
