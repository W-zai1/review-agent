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


def check(text, min_conf=0.6):
    """返回 (issues, verified, skipped)。

    `skipped` 非空表示「本该跑但法条库缺条文」——必须由调用方写进
    limitations。静默跳过等于报告正常但少跑一整类检查，那比报错更危险。
    """
    issues, verified = [], []
    try:
        law_index.load_all()
    except Exception as e:                      # noqa: BLE001
        # 法条库坏了必须显式失败，不能静默变成"没有法律依据可查"
        raise law_index.LawLibraryError(f"法条库不可用：{e}") from e

    art19 = _article("labor-contract", 19)
    art20 = _article("labor-contract", 20)
    art24 = _article("labor-contract", 24)
    skipped = [f"法条库缺少「{label}」，该项未执行（不是没问题，是没查）"
               for label, art in (("劳动合同法 第十九条 试用期上限", art19),
                                  ("劳动合同法 第二十条 试用期工资", art20),
                                  ("劳动合同法 第二十四条 竞业限制期限", art24))
               if not art]

    for sent in _sentences(text):
        # ---- 1) 试用期时长 ----
        for m in re.finditer(r"试用期[^。；\n]{0,24}?" + _NUM + r"\s*(个月|月|年)", sent):
            n = _to_num(m.group(1))
            if n is None:
                continue
            months = n * 12 if m.group(2) == "年" else n
            if months <= 6:
                if art19:
                    verified.append({
                        "text": f"试用期约定 {m.group(1)}{m.group(2)}，未超法定上限 6 个月",
                        "citation": _cite_dict(art19)})
                continue
            quote_wrong = _guard_quote_clause(sent, ["不得超过", "不超过", "上限"])
            issues.append({
                "rule_id": "LAW-001",
                "quote": sent,
                "evidence": (f"《{art19['law_short']}》{art19['no_cn']}：试用期最长不得超过六个月"
                             f"（三年以上固定期限/无固定期限）；本文写「{m.group(1)}{m.group(2)}」"
                             f"≈{months:g} 个月，超出法定上限。"),
                "severity": "major",
                "confidence": 0.9 if not quote_wrong else 0.75,
                "tool": "law_cite:试用期上限",
                "citation": _cite_dict(art19),
                "suggestion": ("按合同期限下调试用期：三个月以上不满一年→≤1 个月；"
                               "一年以上不满三年→≤2 个月；三年以上/无固定期限→≤6 个月。"
                               "超出部分已履行的，按第八十三条支付赔偿金。"),
            })
            break       # 一句只报一条，避免同一句里两个数字各报一次

        # ---- 2) 竞业限制期限 ----
        for m in re.finditer(r"竞业限制[^。；\n]{0,40}?" + _NUM + r"\s*(年)", sent):
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
        for m in re.finditer(r"试用期[^。；\n]{0,24}?工资[^。；\n]{0,24}?"
                             r"(\d+(?:\.\d+)?)\s*[%％]", sent):
            pct = float(m.group(1))
            if pct >= 80:
                if art20:
                    verified.append({
                        "text": f"试用期工资为 {pct:g}%，不低于法定 80%",
                        "citation": _cite_dict(art20)})
                continue
            issues.append({
                "rule_id": "LAW-001",
                "quote": sent,
                "evidence": (f"《{art20['law_short']}》{art20['no_cn']}：试用期工资不得低于"
                             f"本单位相同岗位最低档工资或约定工资的百分之八十，且不得低于"
                             f"当地最低工资标准；本文写 {pct:g}%，低于法定下限。"),
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
    return uniq, verified, skipped


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
