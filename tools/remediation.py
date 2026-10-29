#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""remediation.py —— 把「可疑」变成「改哪一句、改成什么、不改会怎样」。

审查报告最常见的失败不是漏报，而是**只给判断、不给动作**：
  「COMP-002 缺失终止条款，建议补充」——用户仍然不知道补什么、补在哪、
  不改会损失什么。

本模块只做一件事：为每条 issue 生成一个可执行的 fix 对象。

    {
      "action": "replace" | "insert" | "delete" | "annotate" | "manual",
      "where": "第 3 句 / 合同末尾（签署页之前）",
      "from": "原文片段",
      "to": "改写后的文本（能直接替换）" 或 None,
      "consequence": "不改会怎样（具体后果，不是「有风险」）",
      "how": "人工操作说明（当 to 为空时）",
      "topic": "终止条款"
    }

三条硬约束：
1. **不凭空造事实**。数值改写只允许用事实库/法条库里已有的期望值；
   没有依据的，to 一律为 None，交给人工，不猜。
2. **缺失条款不要求 quote**。合同里没有的条款，原文里当然找不到引用；
   这类 fix 的 where 是「应插入的位置」，不是「哪句话」。
3. **后果要具体到钱/权/时间**。写「有法律风险」等于没写。
"""
from __future__ import annotations

import re

import regex_check


# ---------------------------------------------------------------------------
# 后果库：按规则给「不改会怎样」。这是报告里最该被读到的一行。
# ---------------------------------------------------------------------------
CONSEQUENCE = {
    "COMP-001": "主体不明确，合同可能约束不到真正的交易方；发生纠纷时连被告都列不出来。",
    "COMP-002": "无法确定合同何时结束、如何退出；可能被无限期约束，或提前解约时没有依据。",
    "COMP-003": "没有约定管辖，只能按法定管辖起诉，可能异地诉讼，周期和成本都会上升。",
    "COMP-004": "读者无法核验，数据等于没有；对外披露时可能被认定为误导性陈述。",
    "FACT-001": "对外发布构成事实性错误；被引用、被追责时无法用「笔误」解释。",
    "FACT-002": "结论没有可核验的出处，读者只能选择相信或不信；正式文书里等于没写。",
    "FACT-003": "把相关性说成因果，据此做决策会押错方向；被质疑时拿不出前提。",
    "FACT-004": "口径不明，同一数字可以被读成完全不同的结论；同比/环比混用会直接误导。",
    "LOGIC-001": "同一指标出现两个值，读者无法判断哪个为准；据此决策会直接出错。",
    "LOGIC-002": "时间线自相矛盾，事实叙述不可信；合同/报告的关键节点无法执行。",
    "LOGIC-003": "因果链不成立，结论只是猜测；被追问证据时无法自洽。",
    "LOGIC-004": "违反常识或物理约束，读者会立刻怀疑整份材料的可信度。",
    "LOGIC-005": "样本撑不起结论，属于过度外推；用于决策会放大偏差。",
    "LOGIC-006": "结论与正文相反，读者会按结论行动，而正文已经否定了它。",
    "LOGIC-007": "人物属性前后矛盾，事实基础不可信；涉及任职/资历的表述可能构成不实陈述。",
    "LOGIC-008": "绝对化表述与文中事实冲突，容易被抓住做反例，削弱整份材料的可信度。",
    "LOGIC-009": "方向词与负号同时出现，读者无法判断是增长还是下降。",
    "LAW-001": "违反法定上限的部分无效；对方可据此主张条款不生效或要求补足。",
    "SAFE-001": "内容可被直接用于实施攻击/违法操作；发布后可能承担平台责任或法律责任。",
    "SAFE-002": "提供了违法操作的具体路径；可能构成帮助违法，至少会被平台下架。",
    "SAFE-003": "泄露个人信息，可能违反《个人信息保护法》；面临投诉、处罚或民事赔偿。",
    "SAFE-004": "提示词注入可覆盖系统约束；下游模型会执行攻击者指令，输出不可控。",
    "STYLE-001": "正式文书出现表情符号，专业性和严肃性受损。",
    "STYLE-002": "口语化表达削弱正式文书的可信度；对外文件显得不专业。",
    "STYLE-003": "第一人称情绪化表达不适合正式文书；读者会质疑客观性。",
    "STYLE-004": "密钥/口令进入代码或仓库，等于把访问权公开；泄露后需立即轮换。",
    "STYLE-005": "用词错误在正式文书里会被直接看到，影响专业形象。",
    "FORMAT-001": "版式不符合委托方要求，交付时会被退回重排。",
    "FORMAT-002": "字号不符合要求，打印/归档版式不统一。",
    "FORMAT-003": "首行缩进不符合要求，段落层级不清晰。",
    "FORMAT-004": "行距不符合要求，页数和版面不可控。",
    "FORMAT-005": "标题层级混乱，目录与正文结构对不上。",
    "FORMAT-006": "图表编号与正文引用对不上，读者找不到对应图表。",
}
GENERIC_CONSEQUENCE = "该问题会降低文本的可信度或可执行性；发布/签署前应处理。"


# ---------------------------------------------------------------------------
# 缺失条款：给可直接粘贴的条款文本。
# 只给通用骨架 + 空格占位，不替用户填具体金额/日期——那是当事人的商业决定。
# ---------------------------------------------------------------------------
CLAUSE_TEMPLATES = {
    "COMP-001": {
        "where": "合同首部（标题下方、正文之前）",
        "topic": "合同主体",
        "to": (
            "甲方：________________（统一社会信用代码：________________，"
            "住所：________________，联系人及电话：________________）\n"
            "乙方：________________（统一社会信用代码：________________，"
            "住所：________________，联系人及电话：________________）"
        ),
    },
    "COMP-002": {
        "where": "合同末尾（签署页之前）",
        "topic": "生效与终止条款",
        "to": (
            "第 __ 条  生效与终止\n"
            "本合同自双方签字盖章之日起生效，有效期至 ____ 年 __ 月 __ 日。\n"
            "任何一方需提前解除本合同的，应提前 30 日书面通知对方，"
            "并结清截至解除之日已发生的费用；一方严重违约经催告后 15 日内仍未纠正的，"
            "另一方有权书面通知解除。"
        ),
    },
    "COMP-003": {
        "where": "合同末尾（签署页之前）",
        "topic": "争议解决条款",
        "to": (
            "第 __ 条  争议解决\n"
            "因本合同引起的或与本合同有关的任何争议，双方应友好协商解决；"
            "协商不成的，任何一方均有权向合同签订地有管辖权的人民法院提起诉讼。"
        ),
    },
}
MISSING_RE = re.compile(r"(未检索到|未约定|缺少|缺失|未包含|没有约定|未明确)")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _number_spans(text):
    """返回非年份数字（含单位），用于替换。"""
    out = []
    for n in regex_check.extract_numbers(text):
        tail = text[n["end"]:n["end"] + 1]
        if 1900 <= n["num"] <= 2100 and tail == "年":
            continue
        if tail in ("月", "日"):
            continue
        out.append(n)
    return out


# 中文数字 + 量词：「十二个月」「三年」「百分之八十」这类写法在合同里比阿拉伯数字还常见。
# 旧实现只认 \d，导致「试用期十二个月」这种最典型的违法条款给不出改写文本。
CN_CLAIM_RE = re.compile(
    r"([零〇一二两三四五六七八九十百千]+)\s*(个月|年|%|％|万|亿|台|人|元|倍)")


def _cn_spans(text):
    out = []
    for m in CN_CLAIM_RE.finditer(text):
        num = regex_check.cn2num(m.group(1))
        if num is None:
            continue
        out.append({"raw": m.group(0), "num": float(num), "unit": m.group(2),
                    "start": m.start(), "end": m.end()})
    return out


def _fmt(num, unit):
    if isinstance(num, float) and num.is_integer():
        num = int(num)
    sep = "" if unit in ("%", "％") else " "
    return f"{num:g}{sep}{unit}" if isinstance(num, float) else f"{num}{sep}{unit}"


UNIT_TAILS = ("个月", "年", "日", "小时", "分钟", "%", "％", "亿", "万",
              "台", "人", "元", "倍", "家", "辆", "套", "份", "个", "次", "条", "件")


def _consume_unit(q, end, label):
    """把数字后面残留的量词吃掉：`12 个月` 的 raw 只是 `12 `。"""
    tail = q[end:]
    m = re.match(r"\s*(" + "|".join(re.escape(u) for u in UNIT_TAILS) + ")", tail)
    if m and label.endswith(m.group(1)):
        return end + m.end()
    if tail[:1] in "台人元家辆套份个次条件艘架" and label.endswith(tail[:1]):
        return end + 1
    return end
    if isinstance(num, float) and num.is_integer():
        num = int(num)
    return f"{num:g}{unit}" if isinstance(num, float) else f"{num}{unit}"


def _expected_for(issue, assertions):
    """从事实库里找这条 issue 对应的期望值；找不到返回 None。

    只按实体名匹配，不按 rule_id 猜——同一实体可能有多条断言。
    """
    quote = str(issue.get("quote") or "")
    text = quote + "\n" + str(issue.get("evidence") or "")
    for a in assertions or []:
        ent = str(a.get("entity") or "")
        if not ent or ent not in text:
            continue
        attr = str(a.get("attr") or "")
        if attr and attr not in text and a.get("type") != "date":
            continue
        return a
    return None


def _replace_claim_value(quote, assertion):
    """把 quote 里被核验的那个数字/年份换成事实库的期望值。

    只替换第一个同类数字；多数字句子只改一个，避免把无关数字一起改掉。
    """
    q = str(quote or "")
    typ = assertion.get("type")
    if typ == "date":
        want = assertion.get("expected_year")
        if not want:
            return None
        years = list(re.finditer(r"(?<!\d)((?:19|20)\d{2})(?!\d)", q))
        if not years:
            return None
        target = years[0]
        return q[:target.start(1)] + str(want) + q[target.end(1):]
    exp = assertion.get("expected") or {}
    num, unit = exp.get("num"), str(exp.get("unit") or "")
    if num is None:
        return None
    hint = str(assertion.get("unit_hint") or unit)
    spans = _number_spans(q)
    if not spans:
        # 中文数字：整段替换（「十二个月」→「6 个月」）
        cn = _cn_spans(q)
        if cn:
            pick = next((n for n in cn if hint and n["unit"] in hint), cn[0])
            label = str(assertion.get("label") or _fmt(num, hint or pick["unit"] or unit))
            end = _consume_unit(q, pick["end"], label)
            return q[:pick["start"]] + label + q[end:]
        return None
    pick = None
    for n in spans:
        if hint and (n["unit"] == hint or q[n["end"]:n["end"] + len(hint)].startswith(hint)):
            pick = n
            break
    if pick is None:
        pick = spans[0]
    label = str(assertion.get("label") or _fmt(num, hint or pick["unit"] or unit))
    end = pick["end"]
    # 「5 亿台」→ 替换范围要把量词「台」也吃掉，否则会得到「5,000 万台台」
    end = _consume_unit(q, pick["end"], label)
    return q[:pick["start"]] + label + q[end:]


# ---------------------------------------------------------------------------
# 各类规则的具体改法
# ---------------------------------------------------------------------------
STYLE_WORDS = {
    "截止目前": "截至目前",
    "截止当前": "截至当前",
    "截止现在": "截至现在",
    "截止今日": "截至今日",
    "截止今天": "截至今天",
    "帐户": "账户",
    "帐目": "账目",
    "按装": "安装",
    "既使": "即使",
}


def _fix_style(issue):
    rid = issue.get("rule_id")
    q = str(issue.get("quote") or "")
    if rid == "STYLE-005":
        for wrong, right in STYLE_WORDS.items():
            if wrong in q:
                return {
                    "action": "replace", "from": q, "to": q.replace(wrong, right),
                    "topic": "用词规范",
                    "how": f"把「{wrong}」改为「{right}」。",
                }
        return None
    if rid == "STYLE-001":
        cleaned = regex_check.EMOJI_RE.sub("", q).strip()
        if cleaned != q:
            return {
                "action": "replace" if cleaned else "delete",
                "from": q, "to": cleaned or None,
                "topic": "表情符号",
                "how": "删除表情/符号；需要语气时用文字表达。",
            }
        return None
    if rid == "STYLE-004":
        masked = re.sub(
            r"((?:password|passwd|pwd|secret|api[_-]?key|token|密钥|口令|密码)\s*[:=]\s*)"
            r"([\"']?)[^\"'\s,;]{4,}", r"\1\2***（改为 os.getenv(\"ENV_NAME\")）", q, flags=re.I)
        if masked != q:
            return {
                "action": "replace", "from": q, "to": masked,
                "topic": "硬编码密钥",
                "how": "把明文值换成环境变量读取，并立即轮换已泄露的密钥。",
            }
    return None


def _fix_privacy(issue):
    q = str(issue.get("quote") or "")
    if not q:
        return None
    masked = q
    masked = re.sub(r"(?<!\d)(1[3-9]\d{9})(?!\d)", "1**********", masked)
    # 18 位身份证：保留前 6 位与后 4 位
    masked = re.sub(r"(?<![\dXx])(\d{6})\d{8}(\d{4})(?![\dXx])",
                    r"\1********\2", masked)
    masked = re.sub(r"(?<!\d)(\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4})(?!\d)",
                    "**** **** **** ****", masked)
    if masked == q:
        return None
    return {
        "action": "replace", "from": q, "to": masked,
        "topic": "个人信息",
        "how": "删除或脱敏身份证号/手机号/银行卡号；确需保留时取得单独同意并限定使用范围。",
    }


def _fix_logic(issue):
    rid = issue.get("rule_id")
    q = str(issue.get("quote") or "")
    if rid == "LOGIC-009":
        # 「同比下降 -3.2%」→「同比下降 3.2%」（或「同比变化 -3.2%」）
        m = re.search(r"(下降|下滑|减少|降低)\s*(-\s*\d+(?:\.\d+)?\s*%)", q)
        if m:
            fixed = q[:m.start(2)] + m.group(2).replace("-", "").lstrip() + q[m.end(2):]
            return {
                "action": "replace", "from": q, "to": fixed,
                "topic": "方向词与负号",
                "how": "去掉负号；若要保留负号，把「下降」改为「变化」。",
            }
    if rid in ("LOGIC-001", "LOGIC-002", "LOGIC-003", "LOGIC-006", "LOGIC-007"):
        return {
            "action": "manual", "from": q, "to": None,
            "topic": "前后矛盾",
            "how": "先确定哪一处为准（以原始凭证/时间线/已确认事实为准），"
                   "再把另一处改成同一值；无法确定的，两处都标注口径与来源，禁止同时保留两个互斥值。",
        }
    return None


def _fix_compliance(issue):
    rid = issue.get("rule_id")
    ev = str(issue.get("evidence") or "") + str(issue.get("rule_text") or "")
    if rid in CLAUSE_TEMPLATES and MISSING_RE.search(ev):
        tpl = CLAUSE_TEMPLATES[rid]
        return {
            "action": "insert", "where": tpl["where"], "from": None,
            "to": tpl["to"], "topic": tpl["topic"],
            "how": "把占位符替换成本次交易的实际主体/期限；条款编号按全文顺延。",
        }
    if rid == "COMP-004":
        q = str(issue.get("quote") or "")
        return {
            "action": "annotate", "from": q,
            "to": q.rstrip("。；; ") + "（来源：________；统计口径：________；统计期：________）。",
            "topic": "数据出处",
            "how": "补来源、口径、统计期三项；找不到来源就删除该数据，不要保留裸数字。",
        }
    return None


def _fix_fact(issue, assertion):
    rid = issue.get("rule_id")
    q = str(issue.get("quote") or "")
    text = q + "\n" + str(issue.get("evidence") or "")
    if (rid == "LAW-001" or
            ("劳动合同法" in text and any(k in text for k in ("试用期", "竞业", "工资")))):
        # 法定量不是「把 12 改成 6」就完了：原句常常还有「因此可以约定一年」
        # 这种结论，只改数字会留下自相矛盾。这里直接给一句可整句替换的合规表述。
        if "竞业" in text:
            return {
                "action": "replace", "from": q,
                "to": ("根据《劳动合同法》第二十四条，竞业限制期限不得超过二年；"
                       "超过二年的部分无效，应按不超过二年重新约定。"),
                "topic": "竞业限制期限",
                "consequence": "超过二年的部分对劳动者没有约束力；企业据此主张违约金可能不被支持。",
                "how": "整句替换；超过二年的部分对劳动者没有约束力。",
            }
        if "工资" in text or "80%" in text or "百分之八十" in text:
            return {
                "action": "replace", "from": q,
                "to": ("根据《劳动合同法》第二十条，试用期工资不得低于本单位相同岗位"
                       "最低档工资或者劳动合同约定工资的百分之八十，并不得低于用人单位"
                       "所在地的最低工资标准。"),
                "topic": "试用期工资",
                "consequence": "低于法定标准的约定无效，需补足差额；还可能被劳动监察责令改正。",
                "how": "整句替换，并把约定比例调整为不低于法定下限。",
            }
        if "试用期" in text:
            return {
                "action": "replace", "from": q,
                "to": ("根据《劳动合同法》第十九条，试用期最长不得超过六个月；"
                       "本合同约定的试用期应调整为不超过六个月，"
                       "并按劳动合同期限确定具体上限。"),
                "topic": "试用期法定上限",
                "consequence": "超过法定上限的部分无效；劳动者可主张按转正工资补足差额，企业还可能面临劳动监察风险。",
                "how": "整句替换；不要把「最长不得超过」改成一个仍然超限的数字。",
            }
    if assertion and rid in ("FACT-001", "LAW-001"):
        rewritten = _replace_claim_value(q, assertion)
        if rewritten and rewritten != q:
            num = (assertion.get("expected") or {}).get("num")
            unit = (assertion.get("unit_hint") or
                    (assertion.get("expected") or {}).get("unit") or "")
            topic = f"{assertion.get('entity', '')}{assertion.get('attr', '')}".strip()
            return {
                "action": "replace", "from": q, "to": rewritten,
                "topic": topic or "事实数值",
                "how": f"改为 {assertion.get('label') or _fmt(num, unit)}，"
                       "并在句末标注统计口径与时间。",
                "expected": {"num": num, "unit": unit,
                             "year": assertion.get("expected_year")},
            }
    if rid == "FACT-002":
        return {
            "action": "annotate", "from": q,
            "to": q.rstrip("。；; ") + "（来源：________；统计期：________）。",
            "topic": "引用来源",
            "how": "补可定位的来源（链接/文献名/文号）；补不出来就删掉这句结论。",
        }
    if rid == "FACT-003":
        return {
            "action": "manual", "from": q, "to": None,
            "topic": "因果归因",
            "how": "补前提与数据；补不出来就把「因此/说明/意味着」改成「可能/有待验证」。",
        }
    if rid == "FACT-004":
        return {
            "action": "annotate", "from": q,
            "to": q.rstrip("。；; ") + "（同比/环比：________；统计期：________）。",
            "topic": "统计口径",
            "how": "补同比/环比与统计期；倍数要写清比较基线。",
        }
    return None


def _fix_safety(issue):
    rid = issue.get("rule_id")
    q = str(issue.get("quote") or "")
    if rid == "SAFE-003":
        return _fix_privacy(issue)
    if rid in ("SAFE-001", "SAFE-002"):
        return {
            "action": "delete", "from": q, "to": None,
            "topic": "可操作的危险内容",
            "how": "删除具体操作步骤/剂量/工具清单；只保留风险提示和防御建议。",
        }
    if rid == "SAFE-004":
        return {
            "action": "delete", "from": q, "to": None,
            "topic": "提示词注入",
            "how": "删除该指令；如确需讨论注入，改为「攻击者可能尝试覆盖系统约束」的转述。",
        }
    return None


def _fix_format(issue):
    rid = issue.get("rule_id")
    q = str(issue.get("quote") or "")
    if not rid.startswith("FORMAT-"):
        return None
    what = {
        "FORMAT-001": "按所选格式规格设置正文字体",
        "FORMAT-002": "按所选格式规格设置正文字号",
        "FORMAT-003": "设置正文首行缩进",
        "FORMAT-004": "设置正文行距",
        "FORMAT-005": "统一标题样式与层级",
        "FORMAT-006": "补题注/改编号，使正文引用与图表编号一致",
    }.get(rid, "按格式规格调整版式")
    return {
        "action": "manual", "from": q, "to": None,
        "topic": "版式",
        "how": what + "；具体数值以本次格式规格为准（报告 format_spec 字段）。",
    }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def build_fix(issue, assertions=None):
    """为一条 issue 生成 fix；永远返回 dict（不允许因为没模板就消失）。"""
    rid = str(issue.get("rule_id") or "")
    quote = str(issue.get("quote") or "")
    base = {
        "action": "manual",
        "where": str(issue.get("location") or ""),
        "from": quote or None,
        "to": None,
        "consequence": CONSEQUENCE.get(rid, GENERIC_CONSEQUENCE),
        "how": str(issue.get("suggestion") or "").strip() or "需人工复核并改写。",
        "topic": "",
    }

    # 1) 规则专属改法
    fix = None
    if rid.startswith("COMP-"):
        fix = _fix_compliance(issue)
    elif rid.startswith("FACT-") or rid == "LAW-001":
        fix = _fix_fact(issue, _expected_for(issue, assertions or []))
    elif rid.startswith("STYLE-"):
        fix = _fix_style(issue)
    elif rid.startswith("SAFE-"):
        fix = _fix_safety(issue)
    elif rid.startswith("FORMAT-"):
        fix = _fix_format(issue)
    elif rid.startswith("LOGIC-"):
        fix = _fix_logic(issue)
    elif rid.startswith("OPEN-"):
        fix = {
            "action": "manual", "from": quote, "to": None,
            "topic": "规则外发现",
            "how": "模型发现不在规则库内：先人工确认是否成立，再按上方建议改写；"
                   "确认成立的，考虑补成正式规则。",
        }

    if fix:
        # from 允许显式为 None（缺失条款没有原文可引）；to 也必须允许 None
        base.update({k: v for k, v in fix.items()
                     if v is not None or k in ("to", "from")})
    # 2) 没有具体改写文本时，明确标记为人工改写（而不是假装有方案）
    base["has_rewrite"] = bool(base.get("to"))
    base["summary"] = _summary(base)
    return base


def _summary(fix):
    rid = fix.get("topic") or ""
    if fix["action"] == "insert":
        return f"新增「{rid}」模板"
    if fix["action"] == "annotate":
        return f"补「{rid}」：按模板补来源/口径"
    if fix["action"] == "delete":
        return f"删除「{rid}」：去掉可操作内容"
    if fix.get("to"):
        return f"把原文改为：{str(fix['to'])[:80]}"
    return f"需人工改写（{rid or '未分类'}）：{str(fix.get('how') or '')[:80]}"


def attach(issues, assertions=None, top_n=8):
    """给最终 issue 列表逐条挂 fix，并返回报告用的整改计划。"""
    items = []
    for it in issues or []:
        if not it.get("fix"):
            it["fix"] = build_fix(it, assertions)
        fix = it["fix"]
        # 只改「缺失条款」的建议：原文的「补充 COMP-002 要求的条款」对用户没信息量。
        # 其余规则保留原建议（它可能带着偏差倍数/法条上限等读者需要的判据），
        # 具体改法由 fix.to / fix.how 单独给，不覆盖判据。
        if fix.get("action") == "insert":
            it["suggestion"] = f"{fix.get('where') or '合同末尾'}：{fix['summary']}"
        items.append(it)
    # 计划只列 blocker/major；minor 即使有改写文本也不进「先改这几处」
    order = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
    plan_items = [x for x in items if x.get("severity") in ("blocker", "major")]
    ranked = sorted(
        plan_items,
        key=lambda x: (0 if x["fix"].get("has_rewrite") else 1,
                       order.get(x.get("severity"), 9),
                       -float(x.get("confidence") or 0)))
    top = [{
        "issue_id": x.get("issue_id"),
        "rule_id": x.get("rule_id"),
        "severity": x.get("severity"),
        "where": x["fix"].get("where") or x.get("location") or "",
        "from": x["fix"].get("from") or "",
        "to": x["fix"].get("to"),
        "consequence": x["fix"].get("consequence"),
        "how": x["fix"].get("how"),
        "action": x["fix"].get("action"),
        "summary": x["fix"].get("summary"),
        "has_rewrite": x["fix"].get("has_rewrite"),
    } for x in ranked[:top_n]]
    concrete = sum(1 for x in plan_items if x["fix"].get("has_rewrite"))
    return {
        "total": len(items),
        "concrete": concrete,
        "manual": len(plan_items) - concrete,
        "top": top,
        "headline": (f"{concrete} 条可直接改，{len(plan_items) - concrete} 条需人工改写"
                     if plan_items else "没有需要整改的条目"),
    }


def main():
    """CLI：把一段 issue JSON 补上 fix，便于单测/调试。"""
    import json
    import sys
    raw = sys.stdin.read()
    issues = json.loads(raw)
    print(json.dumps(attach(issues), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
