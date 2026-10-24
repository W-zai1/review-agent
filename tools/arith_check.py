#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""arith_check.py —— 确定性算术/日历校验（不依赖模型）

为什么单独做这一层：
  压力测试里稳定漏报的 6 条中，有 4 条是「算一下就知道」的题——
  日期-星期、日期差、加总、占比。这些恰恰是 LLM 结构上不擅长的：
  它会把 360+210+480 算成 1080，也会说 2026-10-18 是周六。
  把这类判断从概率模型里拿走，换成代码，结果是 100% 可复现的。

设计原则（宁可漏报，不可误报）：
  1. 只在关系**明确无歧义**时才开火——必须有「三项/四项」这类计数词、
     明确的「合/加总」动词，或明确的「占…%」+「员工总数」。
  2. 单位必须一致；百分数不当金额参与求和。
  3. 容差覆盖正常的四舍五入（金额 ±0.5 或 0.2%，占比 ±2 个百分点）。
  4. 年份无法确定时**不猜**——日期-星期检查直接跳过。
  误报会直接污染本来就只有约 1/4 信噪比的清单，所以这里必须保守。
"""
import datetime
import re

import regex_check

# ---------- 通用 ----------
SENT_SPLIT = re.compile(r"(?<=[。！？\n])")
AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(万|亿)?元?")
COUNT_WORD = {"两项": 2, "三项": 3, "四项": 4, "五项": 5}
WEEKDAY_CN = "一二三四五六日"


def sentences(text):
    """返回 [(start, sentence)]，保留原文位置以便回填 location。"""
    out, pos = [], 0
    for m in SENT_SPLIT.finditer(text):
        seg = text[pos:m.end()]
        if seg.strip():
            out.append((pos, seg))
        pos = m.end()
    if pos < len(text) and text[pos:].strip():
        out.append((pos, text[pos:]))
    return out


CN_AMOUNT = re.compile(
    r"([一二三四五六七八九十百千零两壹贰叁肆伍陆柒捌玖拾佰仟]{1,14})\s*(万|亿)\s*元?")


def _amounts_in(seg, unit=None):
    """抽取金额；跳过百分数（45% 不是金额）。

    支持两种常见写法：
      - 阿拉伯数字带千分位：1,360 万
      - 中文数字（会计/正式文书）：三百六十万、一千零八十万
    中文数字**必须带万/亿单位**才认，否则「二季度」「三月」会被当成金额。
    """
    out = []
    for m in AMOUNT.finditer(seg):
        raw = m.group(1).replace(",", "")
        u = m.group(2) or ""
        if u and unit and u != unit:
            continue
        # 百分数不是金额：全角 ％、带空格 45 % 都要认（第三轮 N2）
        if re.match(r"\s*[%％]", seg[m.end():m.end() + 3]):
            continue
        out.append({"value": float(raw), "unit": u,
                    "start": m.start(), "end": m.end(),
                    "raw": m.group(0).strip()})
    for m in CN_AMOUNT.finditer(seg):
        u = m.group(2)
        if unit and u != unit:
            continue
        val = regex_check.cn2num(m.group(1))
        if val is None:
            continue
        out.append({"value": float(val), "unit": u,
                    "start": m.start(), "end": m.end(),
                    "raw": m.group(0).strip()})
    out.sort(key=lambda x: x["start"])
    return out


def _num(raw):
    """把「1,360」「三百六十」这类写法转成 float；失败返回 None。"""
    raw = str(raw).strip().replace(",", "")
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return float(raw)
    val = regex_check.cn2num(raw)
    return float(val) if val is not None else None


GROWTH_CTX = re.compile(r"(增长|下降|减少|增加|提升|下滑|同比|环比|涨|跌|净增|净减)")


def _is_growth(seg, pos, end=None, before=8, after=18):
    """数字邻近出现"增长/环比"等变化词 → 它是变化量，不是分项金额。

    必须**前后都看**：第二轮只查了数字前 8 字，被
    「回款 45 万（较上月有所增长）」这种后置说明绕过（第三轮 N1）。
    """
    if end is None:
        end = pos
    return bool(GROWTH_CTX.search(seg[max(0, pos - before): min(len(seg), end + after)]))


def _fmt(v):
    return str(int(v)) if abs(v - int(v)) < 1e-9 else f"{v:g}"


# ---------- 1. 分项加总 ----------
TOTAL = re.compile(
    r"(两项|三项|四项|五项)?\s*(?:加总|合计|共计|总计|累计|总和)"
    r"\s*(?:为|是|共)?\s*"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
    r"|[一二三四五六七八九十百千零两]{1,14})\s*(万|亿)?\s*元?")


def check_sums(text):
    """「销售 360 万、管理 210 万、研发 480 万，三项加总 1080 万元」这类。

    只认带计数词（三项/四项…）的合计，避免把「收入 2400 万…合计 1050 万」
    这种混合表述误判。取合计前最近的 k 个同单位金额作为被加数。
    """
    findings = []
    for start, seg in sentences(text):
        for m in TOTAL.finditer(seg):
            word = m.group(1)
            if not word:                       # 没有计数词 → 不敢断言，跳过
                continue
            need = COUNT_WORD[word]
            unit = m.group(3) or ""
            total = _num(m.group(2))
            if total is None:
                continue
            prefix = seg[:m.start()]
            ops = [a for a in _amounts_in(prefix, unit)
                   if a["unit"] == unit
                   and not _is_growth(prefix, a["start"], a.get("end"))]
            if len(ops) < need:
                continue
            ops = ops[-need:]
            s = sum(a["value"] for a in ops)
            # 容差：金额量级越大越允许一点误差，但不超过 0.5 或 0.2%
            tol = max(0.5, abs(total) * 0.002)
            if abs(s - total) <= tol:
                continue
            expr = "+".join(_fmt(a["value"]) for a in ops)
            findings.append({
                "rule_id": "LOGIC-001",
                "dimension": "logic",
                "quote": seg.strip(),
                "start": start,
                "evidence": (f"分项相加 {expr}={_fmt(s)}{unit}，"
                             f"文中写 {_fmt(total)}{unit}，相差 {_fmt(abs(s - total))}{unit}"),
                "suggestion": "核对分项数字与合计数，二者必须一致（可能是漏项或合计笔误）。",
                "confidence": 0.95,
                "tool": "arith_check:sum",
            })
    return findings


# ---------- 2. 日期-星期 ----------
_D = r"(\d{1,2}|[一二三四五六七八九十]{1,3})"
# 括号可省略：「11 月 5 日 周五」「11 月 5 日（周五）」都要认
DATE_WEEK = re.compile(
    _D + r"\s*月\s*" + _D + r"\s*日\s*[（(]?\s*(?:星期|周)\s*([一二三四五六日天])\s*[)）]?")
YEAR_NEAR = re.compile(r"(\d{4})\s*年")
# 只把「YYYY 年 M 月」当作可靠锚点：单独的「2024 年」（版本号、数据口径）不足以确定日期年份
YEAR_ANCHOR = re.compile(r"(\d{4})\s*年\s*\d{1,2}\s*月")
# 「2024 年 12 月版本」「基于…12 月修订」不是时点，是口径/版本标识，不能用来定年份
ANCHOR_BAD_CTX = re.compile(r"^[^，。；\n]{0,6}(版本|修订|版次|口径|基准|截至|发布|更新|存档|归档)")


def _valid_anchors(text, end):
    """返回 [(year, is_valid)]：把"版本/口径"类锚点标记为无效。"""
    out = []
    for m in YEAR_ANCHOR.finditer(text[:end]):
        tail = text[m.end(): m.end() + 12]
        out.append((int(m.group(1)), not bool(ANCHOR_BAD_CTX.search(tail))))
    return out


def _infer_year(text, pos, prev_sent_start):
    """年份推断：日期内联的年份 → 最近一个「YYYY 年 M 月」锚点 → 放弃。

    三代实现都踩过坑：
      ① 取全文第一个年份 → 多年度文档把 2026 按 2024 算（a02 误报）；
      ② 取最近一个「YYYY 年」 → 被「豆包 2024 年 MAU」这种无关口径带偏（对抗样本误报）；
      ③ 现在：只认完整日期锚点（年+月），因为只有它代表"某个具体时点"。
         找不到锚点就**不判**——宁可漏报，不可用错年份去断言星期。
    """
    inline = re.search(r"(\d{4})\s*年\s*$", text[max(0, pos - 14):pos])
    if inline:
        return int(inline.group(1))
    anchors = _valid_anchors(text, pos)
    valid = [y for y, ok in anchors if ok]
    if valid:
        return valid[-1]                      # 最近的有效「年+月」锚点
    # 没有有效锚点：退回最近的裸年份，但排除只出现在「版本/口径」语境里的年份
    bad_years = {y for y, ok in anchors if not ok}
    bare = [m for m in YEAR_NEAR.finditer(text[:pos])]
    if bare:
        year = int(bare[-1].group(1))
        if year in bad_years and year not in valid:
            return None                       # 唯一线索是版本年份 → 宁可不判
        return year
    return None


def check_weekdays(text):
    """「10 月 18 日（周六）」 → 查真实日历。年份不确定就不判。"""
    findings = []
    for start, seg in sentences(text):
        for m in DATE_WEEK.finditer(seg):
            month, day = _num(m.group(1)), _num(m.group(2))
            if month is None or day is None:
                continue
            month, day = int(month), int(day)
            claim = m.group(3)
            year = _infer_year(text, start + m.start(), start)
            if not year:
                continue
            try:
                actual = datetime.date(year, month, day)
            except ValueError:
                findings.append({
                    "rule_id": "LOGIC-002", "dimension": "logic",
                    "quote": seg.strip(), "start": start,
                    "evidence": f"{year} 年 {month} 月 {day} 日不存在（日期本身非法）",
                    "suggestion": "核对日期。", "confidence": 0.95,
                    "tool": "arith_check:weekday",
                })
                continue
            claim_norm = "日" if claim == "天" else claim
            real = WEEKDAY_CN[actual.weekday()]
            if claim_norm == real:
                continue
            findings.append({
                "rule_id": "LOGIC-002",
                "dimension": "logic",
                "quote": seg.strip(),
                "start": start,
                "evidence": (f"{year}-{month:02d}-{day:02d} 实际是周{real}，"
                             f"文中写「周{claim}」（按日历推算，不依赖模型）"),
                "suggestion": f"把「周{claim}」改为「周{real}」，或核对日期是否为 {month} 月 {day} 日。",
                "confidence": 0.97,
                "tool": "arith_check:weekday",
            })
    return findings


# ---------- 3. 日期区间 vs 历时月数 ----------
DATE = re.compile(r"(?:(\d{4})\s*年\s*)?"
                  r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*月\s*"
                  r"(\d{1,2}|[一二三四五六七八九十]{1,3})\s*日")
# 只认明确的"工期跨度"词。「共投入 6 个月人力」是工作量，不是工期（实测 a03 误报）
DURATION = re.compile(r"(?:历时|前后|跨度|工期|耗时)\s*(?:了|约|共)?\s*"
                      r"(\d+(?:\.\d+)?)\s*个?\s*(半)?\s*个?月")
AVG_MONTH_DAYS = 30.44


def check_durations(text):
    """「7 月 1 日启动，10 月 15 日发运完毕，历时 4 个半月」→ 实际 3.5 个月。"""
    findings = []
    for start, seg in sentences(text):
        dur = DURATION.search(seg)
        if not dur:
            continue
        dates = list(DATE.finditer(seg))
        if len(dates) < 2:
            continue
        year = _infer_year(text, start, start) or datetime.date.today().year
        try:
            m1, dd1 = _num(dates[0].group(2)), _num(dates[0].group(3))
            m2, dd2 = _num(dates[-1].group(2)), _num(dates[-1].group(3))
            if None in (m1, dd1, m2, dd2):
                continue
            d1 = datetime.date(year, int(m1), int(dd1))
            d2 = datetime.date(year, int(m2), int(dd2))
        except ValueError:
            continue
        if d2 < d1:                        # 跨年：补一年
            try:
                d2 = d2.replace(year=d2.year + 1)
            except ValueError:
                continue
        claimed = float(dur.group(1)) + (0.5 if dur.group(2) else 0.0)
        actual = (d2 - d1).days / AVG_MONTH_DAYS
        actual_r = round(actual * 2) / 2    # 取到 0.5 个月
        if abs(actual_r - claimed) <= 0.75:
            continue
        findings.append({
            "rule_id": "LOGIC-002",
            "dimension": "logic",
            "quote": seg.strip(),
            "start": start,
            "evidence": (f"{d1.month} 月 {d1.day} 日至 {d2.month} 月 {d2.day} 日共 "
                         f"{(d2 - d1).days} 天，约 {_fmt(actual_r)} 个月；"
                         f"文中写 {_fmt(claimed)} 个月（按日期差计算，不依赖模型）"),
            "suggestion": "核对起止日期或「历时」月数，二者应对应。",
            "confidence": 0.93,
            "tool": "arith_check:duration",
        })
    return findings


# ---------- 4. 占比 vs 人数/总数 ----------
PEOPLE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:名|人)")
TOTAL_PEOPLE = re.compile(r"(?:员工总数|总人数|在册员工|全体员工|公司总人数|全部员工)"
                          r"\s*(?:为|有|达)?\s*(\d+)\s*(?:名|人)")
PCT_CLAIM = re.compile(r"占\s*(?:员工总数的?|公司总人数的?|全员的?|总数的?)?\s*"
                       r"(\d+(?:\.\d+)?)\s*%")
PCT_TOL = 2.0          # 百分点；覆盖正常四舍五入（22.7% → 23%），拦得住 22.7% 报 25%


def check_ratios(text):
    """「售后团队 50 人，占员工总数的 25%」+「在册员工 220 人」→ 实际 22.7%。"""
    totals = {int(m.group(1)) for m in TOTAL_PEOPLE.finditer(text)}
    if len(totals) != 1:          # 多个总人数 → 口径不明，宁可不判
        return []
    total = totals.pop()
    if total <= 0:
        return []
    findings = []
    for start, seg in sentences(text):
        for m in PCT_CLAIM.finditer(seg):
            claimed = float(m.group(1))
            people = PEOPLE.findall(seg[:m.start()])
            subject = None
            # 优先读「行政占…」这类显式主语，再从同句找它的人数；
            # 一句话里出现多组人数时，取最后一个会张冠李戴（实测 a05）
            tail = re.sub(r"[\s，,。；;、]+$", "", seg[:m.start()])
            sm = re.search(r"([\u4e00-\u9fa5]{1,6})$", tail)
            if sm:
                name = re.sub(r"^(其中|另外|而|但|所以|因此)", "", sm.group(1))
                if name:
                    hit = re.search(re.escape(name) + r"[^。]{0,8}?(\d+(?:\.\d+)?)\s*(?:人|名)",
                                    seg)
                    if hit:
                        subject = float(hit.group(1))
            if subject is None:
                if not people:
                    continue
                subject = float(people[-1])
            if subject <= 0 or subject > total:
                continue
            actual = subject / total * 100
            if abs(actual - claimed) <= PCT_TOL:
                continue
            findings.append({
                "rule_id": "LOGIC-001",
                "dimension": "logic",
                "quote": seg.strip(),
                "start": start,
                "evidence": (f"{_fmt(subject)} 人 / 总人数 {total} 人 = {actual:.1f}%，"
                             f"文中写 {_fmt(claimed)}%（相差 {abs(actual - claimed):.1f} 个百分点）"),
                "suggestion": "核对人数或占比口径；若总数不是这个口径，请补明分母。",
                "confidence": 0.82,
                "tool": "arith_check:ratio",
            })
    return findings


# ---------- 5. 入职年份 vs「我干了 N 年」 ----------
JOIN_YEAR = re.compile(r"(\d{4})\s*年[^。；\n]{0,10}(入职|社招|进公司|加入公司|入司|来到公司)")
SELF_CLAIM = re.compile(r"(?:我|本人|自己)[^。；\n]{0,12}?"
                        r"([一二三四五六七八九十两]{1,4}|\d{1,3})\s*年(?:了|多)?")
DOC_YEAR = re.compile(r"(20\d{2})\s*年\s*\d{1,2}\s*月")
CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s):
    """中文数字/阿拉伯数字 → int（支持 十一 / 二十三 / 十一 这种）。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if not s or any(c not in CN_DIGIT for c in s):
        return None
    if len(s) == 1:
        return CN_DIGIT[s]
    if s[0] == "十":                       # 十一 / 十二
        return 10 + CN_DIGIT.get(s[1:], 0)
    if "十" in s:
        a, _, b = s.partition("十")         # 二十三 / 三十
        return CN_DIGIT[a] * 10 + (CN_DIGIT.get(b) if b else 0)
    return None


def check_tenure(text):
    """「我在北辰十一年了」+「2016 年社招进公司」→ 2026−2016=10 年，差 1 年。

    刻意保守：全篇必须**只有一个**入职年份、**只有一个**第一人称年限自述，
    否则无法确定两者说的是同一个人，宁可不判。
    """
    joins = list(JOIN_YEAR.finditer(text))
    claims = list(SELF_CLAIM.finditer(text))
    if len(joins) != 1 or len(claims) != 1:
        return []
    join = joins[0]
    claim = claims[0]
    # 「我研究行业二十年」不是工龄（实测 a04 误报）：句中含这些动词就跳过
    sent_start = text.rfind("\n", 0, claim.start()) + 1
    sent_end = text.find("\n", claim.end())
    sentence = text[sent_start: sent_end if sent_end > 0 else len(text)]
    # 词表兜不住（第三轮 N4 用「摸爬滚打」绕过）。改为识别**行业/从业语义**：
    # 出现行业领域词或从业动词，就说明说的是"干这行多少年"，不是"在这家公司多少年"。
    TRADE = re.compile(r"(这行|这行业|行业|领域|本职|本行|专业|技术|市场|销售|研发|"
                       r"财务|管理|业务|岗位|手艺|一线)")
    CAREER = re.compile(r"(研究|从事|入行|从业|经验|深耕|做这行|干这行|接触|"
                        r"摸爬滚打|闯荡|打拼|投身|扎根|打杂|混迹)")
    if TRADE.search(sentence) or CAREER.search(sentence):
        return []
    claim_years = _cn_to_int(claim.group(1))
    if not claim_years:
        return []
    m = DOC_YEAR.search(text)
    ref_year = int(m.group(1)) if m else datetime.date.today().year
    actual = ref_year - int(join.group(1))
    if actual < 0 or abs(claim_years - actual) < 1:
        return []
    # 句子的定位取"自述"所在句（问题出在这句）
    sent_start = text.rfind("\n", 0, claim.start()) + 1
    quote = text[sent_start: text.find("\n", claim.end()) if text.find("\n", claim.end()) > 0
                  else len(text)].strip()
    return [{
        "rule_id": "LOGIC-007",
        "dimension": "logic",
        "quote": quote or claim.group(0),
        "start": sent_start,
        "evidence": (f"文中自述「{claim_years} 年」，但唯一的入职年份是 {join.group(1)} 年"
                     f"（{join.group(0)}），按 {ref_year} 年推算应为 {actual} 年"),
        "suggestion": "核对入职年份或自述年限；两者至少有一个需要更正。",
        "confidence": 0.75,
        "tool": "arith_check:tenure",
    }]


CHECKS = (check_sums, check_weekdays, check_durations, check_ratios, check_tenure)


def run_all(text, active_rule_ids=None):
    """跑全部确定性校验，按 (rule_id, quote) 去重。

    active_rule_ids：只跑被启用的规则，与 rules.state.json 保持一致。
    """
    findings, seen = [], set()
    for fn in CHECKS:
        for f in fn(text):
            if active_rule_ids is not None and f["rule_id"] not in active_rule_ids:
                continue
            key = (f["rule_id"], f["quote"])
            if key in seen:
                continue
            seen.add(key)
            findings.append(f)
    return findings


if __name__ == "__main__":
    import sys
    from pathlib import Path
    p = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not p or not p.exists():
        raise SystemExit("用法：python tools/arith_check.py <文本文件>")
    txt = p.read_text(encoding="utf-8")
    fs = run_all(txt)
    print(f"{p.name}：{len(fs)} 条确定性发现")
    for f in fs:
        print(f"\n[{f['rule_id']}] conf={f['confidence']} {f['tool']}")
        print(f"  原文：{f['quote'][:70]}")
        print(f"  依据：{f['evidence']}")
