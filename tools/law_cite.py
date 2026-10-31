#!/usr/bin/env python3
"""law_cite.py —— 法定量断言 vs 真实条文（确定性检查器）

它解决的正是「证据库太薄、靠模型常识蒙」：
模型可以张口说「试用期不得超过六个月」，但它说不出这是《劳动合同法》
第十九条，也没法让你去核对。这个检查器反过来——**先有条文，才下结论**。

口径刻意收窄（三条，全部是"超限即违法"的单侧判定）
------------------------------------------------
| 检查 | 法定要求 | 依据 |
|---|---|---|
| 试用期上限 | ≤6 个月（一年以上不满三年 ≤2 个月；三个月以上不满一年 ≤1 个月） | 劳动合同法 第十九条 |
| 竞业限制期限 | ≤2 年 | 劳动合同法 第二十四条 |
| 试用期工资 | ≥ 本单位同岗位最低档工资或约定工资的 80%，且 ≥ 当地最低工资 | 劳动合同法 第二十条 |

为什么不做成"你提到劳动法就给你找条文"
--------------------------------------
那种做法会往报告里灌几十条"建议参考第 X 条"，把真问题淹掉。这里只在
**文本给出了一个法定量、且这个量可以对照条文判断**时才开口；
判不了就不说（宁可不报）。

反向也成立：**没超限的要记"已核验"**——因为"查了没问题"和"没查"是
两件事，报告必须能分开说。
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import law_index  # noqa: E402

_SENT_SPLIT = re.compile(r"(?<=[。；！？\n])")
_NUM = r"(\d+(?:\.\d+)?|[一二三四五六七八九十两半]+)"
_CN_NUM = re.compile(r"^[一二三四五六七八九十两半]+$")


def _to_num(s):
    """「12」「十二」「两」→ 数字。半 → 0.5。"""
    s = (s or "").strip()
    if not s:
        return None
    if _CN_NUM.match(s):
        if s == "半":
            return 0.5
        v = law_index.cn2int(s)
        return float(v) if v is not None else None
    try:
        return float(s)
    except ValueError:
        return None


def _sentences(text):
    return [x.strip() for x in _SENT_SPLIT.split(text or "") if x and x.strip()]


def _article(law_id, no):
    """按条号精确取依据。检查器**知道自己要哪一条**，就不该用检索去猜：
    实测「竞业限制三年」检索会飘到第二十三条（讲怎么约定），
    而规定期限上限的是第二十四条。"""
    return law_index.get_article(law_id, no)


def _cite_dict(hit):
    if not hit:
        return None
    return {
        "law": hit["law_short"],
        "law_name": hit["law_name"],
        "article": hit["no_cn"],
        "article_no": hit["no"],
        "chapter": hit.get("chapter", ""),
        "text": hit["text"],
        "status": hit.get("law_status", ""),
        "citation": law_index.citation(hit, max_chars=100000),
    }


def _guard_quote_clause(sent, words):
    """「不得超过六个月」这类**引用法定上限**的句子不该被判超限。

    但注意：不是一律跳过——写成「试用期不得超过十二个月」是把上限说错了，
    仍然要报。所以这里只用来降低置信度，不做豁免。
    """
    return any(w in sent for w in words)


# 第十九条是分档的，不是一句「六个月」：
#   3 个月以上不满 1 年 → 1 个月；1 年以上不满 3 年 → 2 个月；
#   3 年以上固定期限 / 无固定期限 → 6 个月；不满 3 个月 / 以完成一定工作任务为期限 → 不得约定。
# 扁平按 6 个月放行会把「一年期合同 + 四个月试用期」认证成合规（已实测）。
_TERM_RE = re.compile(
    r"(?:劳动)?合同期限(?:为|是|：|:)?\s*" + _NUM + r"\s*(个月|月|年)")
_TERM_SHORT_RE = re.compile(
    r"本合同(?:为)?\s*" + _NUM + r"\s*年(?:期)?"
    r"(?:固定期限|期限)?(?:劳动合同)?")
_DATE_RANGE_RE = re.compile(
    r"自\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:起|开始)?\s*"
    r"至\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:止|结束)?")
# 试用期工资：同时支持「60%」「百分之六十」「百分之 60」。
_SALARY_RE = re.compile(
    r"试用期[^。；\n]{0,24}?工资[^。；\n]{0,24}?"
    r"(?:百分之\s*" + _NUM + r"|(\d+(?:\.\d+)?)\s*[%％])")


def _date_range_months(sent):
    """把「自 2026 年 1 月 1 日起至 2029 年 12 月 31 日止」换算成月数。

    按“起止日都包含”算天数，再按 30.4375 天/月折算并四舍五入：
    一年期合同会得到 12 个月，不会被 365/30 算成 11 个月而错降一档。
    日期非法/倒置 → None（交给调用方按“无法定档”处理）。
    """
    m = _DATE_RANGE_RE.search(sent or "")
    if not m:
        return None
    try:
        start = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        end = datetime.date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None
    days = (end - start).days + 1
    if days <= 0:
        return None
    return round(days / 30.4375)


def _explicit_term_months(*sources):
    """从若干段文本里抽「合同期限 N 年/月」；多个不同值 → None（不猜）。"""
    values = set()
    for source in sources:
        for m in _TERM_RE.finditer(source or ""):
            n = _to_num(m.group(1))
            if n is not None:
                values.add(n * 12 if m.group(2) == "年" else n)
        for m in _TERM_SHORT_RE.finditer(source or ""):
            n = _to_num(m.group(1))
            if n is not None:
                values.add(n * 12)
    return int(next(iter(values))) if len(values) == 1 else None


def _term_months(text, sent):
    """返回 (合同期限月数 | None, 说明)。

    优先级：同句日期区间 → 同句显式期限 → 全文唯一日期区间/显式期限 →
    「无固定期限 / 以完成一定工作任务为期限」标记（同句优先）。
    显式期限必须先于标记判断：一份合同同时出现「一年期」和引文里的
    「无固定期限」时，不能把引文当成本合同的期限。
    """
    months = _date_range_months(sent)
    if months is not None:
        return months, "合同起止日期"
    months = _explicit_term_months(sent)
    if months is not None:
        return months, "合同期限"

    # 同句明确写了合同类型时，优先于全文里的其他期限：
    # “另：无固定期限劳动合同的试用期不得超过六个月”不能被上一句的
    # “一年期合同”带偏成 2 个月档。
    if "以完成一定工作任务为期限" in (sent or ""):
        return 0, "以完成一定工作任务为期限"
    if "无固定期限" in (sent or ""):
        return 36, "无固定期限"

    months = _date_range_months(text)
    if months is not None:
        return months, "合同起止日期"
    months = _explicit_term_months(text)
    if months is not None:
        return months, "合同期限"

    if "以完成一定工作任务为期限" in (text or ""):
        return 0, "以完成一定工作任务为期限"
    if "无固定期限" in (text or ""):
        return 36, "无固定期限"
    return None, "合同期限未写明"


def _probation_cap(term_months):
    """按第十九条档位返回 (上限月数 | None, 档位说明)。"""
    if term_months is None:
        return None, "合同期限无法确定"
    if term_months < 3:
        return 0, "合同期限不满三个月"
    if term_months < 12:
        return 1, "合同期限三个月以上不满一年"
    if term_months < 36:
        return 2, "合同期限一年以上不满三年"
    return 6, "合同期限三年以上/无固定期限"


def load_articles():
    """取出本次判定要用的条文，返回 `{(law_id, no): article}`。

    为什么单独成函数：`check()` 要能被单独测试、也要能被工具调用，而「加载法条库」
    是 IO。把 IO 收在这里，判定逻辑只认一张条文字典——缺条文自然落进 `skipped`，
    不会因为「读不到库」把整类检查静默变成「没问题」。
    """
    try:
        law_index.load_all()
    except Exception as e:                      # noqa: BLE001
        # 法条库坏了必须显式失败，不能静默变成"没有法律依据可查"
        raise law_index.LawLibraryError(f"法条库不可用：{e}") from e
    wanted = (("labor-contract", 19), ("labor-contract", 20),
              ("labor-contract", 24))
    return {key: law_index.get_article(*key) for key in wanted}


def check(text, min_conf=0.6, *, articles=None):
    """返回 (issues, verified, skipped)。

    `skipped` 非空表示「本该跑但判不了」——法条库缺条文，或合同期限未写明
    导致试用期档位无法确定。必须由调用方写进 limitations：静默跳过等于
    报告正常但少跑一整类检查，那比报错更危险。

    `articles`：注入的条文字典（`{(law_id, no): article}`）。给了它就不碰法条库，
    这让判定逻辑可以被单独测试、也可以被工具调用而不带隐式 IO。
    """
    issues, verified = [], []
    if articles is None:
        articles = load_articles()

    art19 = articles.get(("labor-contract", 19))
    art20 = articles.get(("labor-contract", 20))
    art24 = articles.get(("labor-contract", 24))
    skipped = [f"法条库缺少「{label}」，该项未执行（不是没问题，是没查）"
               for label, art in (("劳动合同法 第十九条 试用期上限", art19),
                                  ("劳动合同法 第二十条 试用期工资", art20),
                                  ("劳动合同法 第二十四条 竞业限制期限", art24))
               if not art]

    skip_notes = []

    for sent in _sentences(text):
        # ---- 1) 试用期时长 ----
        term_months, _term_reason = _term_months(text, sent)
        cap, cap_label = _probation_cap(term_months)
        # 缺条文时整段跳过：结论要引用法条，条文都没有就不能出结论。
        # 「缺哪条」已经由上面的 skipped 记下——静默跳过不行，编一条依据更不行。
        for m in (re.finditer(r"试用期[^。；\n]{0,24}?" + _NUM + r"\s*(个月|月|年)", sent)
                  if art19 else ()):
            n = _to_num(m.group(1))
            if n is None:
                continue
            months = n * 12 if m.group(2) == "年" else n
            written = f"{m.group(1)}{m.group(2)}"
            quote_wrong = _guard_quote_clause(sent, ["不得超过", "不超过", "上限"])

            # 合同期限定不了档：>6 个月仍违法（6 是任何档位的绝对上限）；
            # ≤6 个月则只能说“判不了”，绝不能说“已核验”。
            if cap is None:
                if months > 6:
                    issues.append({
                        "rule_id": "LAW-001",
                        "quote": sent,
                        "evidence": (f"《{art19['law_short']}》{art19['no_cn']}：试用期最长不得超过六个月"
                                     f"；本文写「{written}」≈{months:g} 个月，超出任何合同期限档位的上限。"),
                        "severity": "major",
                        "confidence": 0.9 if not quote_wrong else 0.75,
                        "tool": "law_cite:试用期上限",
                        "citation": _cite_dict(art19),
                        "suggestion": ("按合同期限分档：三个月以上不满一年→≤1 个月；"
                                       "一年以上不满三年→≤2 个月；三年以上/无固定期限→≤6 个月。"
                                       "超出部分已履行的，按第八十三条支付赔偿金。"),
                    })
                else:
                    note = (f"合同期限未写明，试用期「{written}」的档位无法确定"
                            "（第十九条），本条未判「已核验」")
                    if note not in skip_notes:
                        skip_notes.append(note)
                break

            if cap == 0:
                issues.append({
                    "rule_id": "LAW-001",
                    "quote": sent,
                    "evidence": (f"《{art19['law_short']}》{art19['no_cn']}：{cap_label}"
                                 "，不得约定试用期；本文写「" + written + "」。"),
                    "severity": "major",
                    "confidence": 0.9 if not quote_wrong else 0.75,
                    "tool": "law_cite:试用期上限",
                    "citation": _cite_dict(art19),
                    "suggestion": "删除试用期约定，或把合同期限调整为三个月以上并重新约定。",
                })
                break

            if months > cap:
                issues.append({
                    "rule_id": "LAW-001",
                    "quote": sent,
                    "evidence": (f"《{art19['law_short']}》{art19['no_cn']}：{cap_label}，"
                                 f"试用期不得超过 {cap} 个月；本文写「{written}」≈{months:g} 个月，"
                                 f"超出该档上限 {months - cap:g} 个月。"),
                    "severity": "major",
                    "confidence": 0.9 if not quote_wrong else 0.75,
                    "tool": "law_cite:试用期上限",
                    "citation": _cite_dict(art19),
                    "suggestion": (f"按{cap_label}，试用期压到 {cap} 个月以内；"
                                   "超出部分已履行的，按第八十三条支付赔偿金。"),
                })
            elif art19:
                verified.append({
                    "text": (f"试用期约定 {written}，未超{cap_label}的 {cap} 个月上限"),
                    "citation": _cite_dict(art19)})
            break       # 一句只报一条，避免同一句里两个数字各报一次

        # ---- 2) 竞业限制期限 ----
        for m in (re.finditer(r"竞业限制[^。；\n]{0,40}?" + _NUM + r"\s*(年)", sent)
                  if art24 else ()):
            n = _to_num(m.group(1))
            if n is None:
                continue
            if n <= 2:
                if art24:
                    verified.append({
                        "text": f"竞业限制期限 {m.group(1)}{m.group(2)}，未超法定上限 2 年",
                        "citation": _cite_dict(art24)})
                continue
            issues.append({
                "rule_id": "LAW-001",
                "quote": sent,
                "evidence": (f"《{art24['law_short']}》{art24['no_cn']}：竞业限制期限"
                             f"不得超过二年；本文写「{m.group(1)}{m.group(2)}」，超出法定上限。"),
                "severity": "major",
                "confidence": 0.9 if "不得超过" not in sent else 0.75,
                "tool": "law_cite:竞业限制期限",
                "citation": _cite_dict(art24),
                "suggestion": "竞业限制期限压缩到 2 年以内；超期部分对劳动者不具约束力。",
            })
            break

        # ---- 3) 试用期工资比例 ----
        for m in (_SALARY_RE.finditer(sent) if art20 else ()):
            if m.group(1):
                pct = _to_num(m.group(1))
                written = f"百分之{m.group(1)}"
            else:
                pct = float(m.group(2))
                written = f"{m.group(2)}%"
            if pct is None:
                continue
            if pct >= 80:
                if art20:
                    verified.append({
                        "text": f"试用期工资为 {written}，不低于法定 80%",
                        "citation": _cite_dict(art20)})
                continue
            issues.append({
                "rule_id": "LAW-001",
                "quote": sent,
                "evidence": (f"《{art20['law_short']}》{art20['no_cn']}：试用期工资不得低于"
                             f"本单位相同岗位最低档工资或约定工资的百分之八十，且不得低于"
                             f"当地最低工资标准；本文写 {written}，低于法定下限。"),
                "severity": "major",
                "confidence": 0.88,
                "tool": "law_cite:试用期工资",
                "citation": _cite_dict(art20),
                "suggestion": "试用期工资提高到约定工资的 80% 以上，并核对当地最低工资标准。",
            })
            break

    # 同句去重：只对他同一检查项去重。一句里同时出现「试用期超限」和
    # 「试用期工资低于 80%」是两处独立违法，合并成一条会让读者以为只有一处。
    seen, uniq = set(), []
    for it in issues:
        key = (it["tool"], it["quote"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return uniq, verified, skipped + skip_notes


def lookup(query, top_n=3):
    """对外查询入口：给报告/接口用，返回带定位的条文列表。"""
    return [dict(h, citation=law_index.citation(h, max_chars=100000))
            for h in law_index.search(query, top_n=top_n)]


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    path = args[0]
    if os.path.exists(path):
        text = open(path, encoding="utf-8", errors="replace").read()
    else:
        text = " ".join(args)
    issues, verified, skipped = check(text)
    for i in issues:
        print(f"[{i['severity']}] {i['tool']}\n  原文：{i['quote'][:90]}\n  依据：{i['citation']['citation'][:120]}")
    for v in verified:
        print(f"[已核验] {v['text']}（{v['citation']['citation'][:60]}）")
    for s in skipped:
        print(f"[未执行] {s}")
    if not issues and not verified:
        print("未发现可对照条文的法定量断言（不等于没有问题）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
