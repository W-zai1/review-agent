#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""logic_checks.py —— 语义类逻辑规则的**窄口径**确定性检查

规则库里原本有 5 条逻辑规则只进了 LLM prompt（LOGIC-003/004/005/006/008），
没有执行器。后果是：报告里挂着 blocker 的结论，其实是模型"读了一遍觉得是"，
跨次会漂移，而用户看不出差别。

这里给其中 4 条补上确定性执行器——**只做能明确判定的子集**，
剩下判不了的（因果关系）继续留在 prompt，并在报告里显式标注为「仅提示」。

三条设计原则（沿用 arith_check 的教训）：

1. **窄口径**：只认「两种明确信号同时出现」，不猜语义。
   例：LOGIC-006 只在「结论用了绝对化词」**且**「正文有未决/保留意见」时才开火。
2. **目标/要求不算事实**：「目标 100%」「要求零缺陷」是期望，不是断言，必须排除。
3. **宁漏不误**：判不了就返回空，让模型去处理——误报会让人放弃整份清单。
"""
import re

import regex_check
from arith_check import sentences

# ---------------------------------------------------------------- 公共

# 「目标/要求/期望」语境：这些词后面的数字与断言不是事实陈述
ASPIRATION = re.compile(
    r"(目标|要求|力争|争取|计划|预计|预期|希望|考核标准|KPI|指标|承诺|不低于|达到)")

# 否定语境：避免把「无风险」「不存在问题」当成风险信号
NEGATED = re.compile(
    r"(无|没有|不存在|未见|未发现|未出现)\s*(?:明显|重大|任何)?\s*(?:风险|问题|缺陷|异常|投诉)")


def _finding(rule_id, quote, start, evidence, suggestion, confidence, tool):
    return {"rule_id": rule_id, "dimension": "logic", "quote": quote.strip(),
            "start": start, "evidence": evidence, "suggestion": suggestion,
            "confidence": confidence, "tool": f"logic_check:{tool}"}


# ---------------------------------------------------------------- LOGIC-004
# 年龄与工龄矛盾：N 岁 + M 年工龄，N-M 小于可就业年龄
AGE = re.compile(r"(\d{2})\s*(?:周)?岁")
# 「从业 25 年」是简历/简介里最常见的写法，早期只认「从业年限」，于是
# 「从业 25 年，现年 30 岁」这种最典型的矛盾反而漏了（第六轮 B2）。
# 这里改成**多写法并列**，且每种都要求「数字 + 年」紧邻，避免把「工作 3 个季度」认成工龄。
TENURE_PATTERNS = (
    re.compile(r"(?:工龄|司龄|工作年限|从业年限|从业时间|工作经验)\s*(?:为|是|有|达)?\s*(\d{1,2})\s*年"),
    re.compile(r"(?:从业|工作|从事|执业|任教|服役)\s*(?:了|已|超过|近)?\s*(\d{1,2})\s*年"),
)
MIN_WORK_AGE = 14          # 低于 14 岁参加工作不成立；比 16 更保守，避免误伤老一辈履历


def _tenure_in(seg):
    for pat in TENURE_PATTERNS:
        m = pat.search(seg)
        if m:
            return int(m.group(1))
    return None


def check_age_tenure(text):
    out = []
    for start, seg in sentences(text):
        ages = [int(x) for x in AGE.findall(seg)]
        years = _tenure_in(seg)
        if not ages or not years:
            continue
        age = ages[0]
        if age < MIN_WORK_AGE or years <= 0:
            continue
        if age - years < MIN_WORK_AGE:
            out.append(_finding(
                "LOGIC-004", seg, start,
                f"年龄 {age} 岁、工龄 {years} 年 → 参加工作年龄约 {age - years} 岁，"
                f"低于 {MIN_WORK_AGE} 岁不成立（确定性推算）",
                "核对年龄或工龄其中一个数字。", 0.88, "age_tenure"))
    return out


# ---------------------------------------------------------------- LOGIC-005
# 样本代表性：局部样本 + 全域结论
SAMPLE_WORDS = re.compile(
    r"(抽样|样本|受访|问卷|回访|调查了|访谈了|抽查|调研|调查|走访|取样|普查|试点)")
SAMPLE_SIZE = re.compile(r"(\d+)\s*(人|份|位|家|个|名|座|所|条|款)")
SCOPE_ALL = re.compile(r"(所有|全体|全部|全公司|全国|全行业|全省|整体|普遍|全面推广)")
SATISFYING = re.compile(r"(满意|支持|认可|同意|通过率|好评)")
# 「据此判断全国市场」并没有"满意"这类评价词，但同样是拿局部样本推全域结论。
# 判断依据换成**推论动词**：有结论动作 + 全域范围 + 样本量，三者同句即成立。
CONCLUSION_VERB = re.compile(r"(据此|因此|可见|由此可见|说明|表明|证明|判断|推断|足以说明|可以认为)")
# 作者自己已经把结论限定在样本内 → 不是「误推全域」，不能报
HEDGE = re.compile(r"(仅代表|不代表|不能推广|不具代表性|样本范围内|仅限于此|局限)")


def check_sampling(text):
    out = []
    for start, seg in sentences(text):
        if not (SAMPLE_WORDS.search(seg) and SCOPE_ALL.search(seg)):
            continue
        if not (SATISFYING.search(seg) or CONCLUSION_VERB.search(seg)):
            continue
        if HEDGE.search(seg):
            continue
        # 样本量必须先出现，且「全部/所有」在样本量之后（描述结论范围）；
        # 「调查了全部 200 名员工」是全覆盖，不是抽样，不能报。
        size = SAMPLE_SIZE.search(seg)
        scope = SCOPE_ALL.search(seg)
        if not size or not scope or scope.start() < size.start():
            continue
        if ASPIRATION.search(seg):
            continue
        out.append(_finding(
            "LOGIC-005", seg, start,
            f"以 {size.group(1)} {size.group(2)}的样本，得出「{scope.group(0)}」层面的结论；"
            "抽样范围与结论普适性不匹配（确定性识别「局部样本 + 全域结论」同句）",
            "把结论限定在样本范围内，或说明抽样方法与代表性。", 0.72, "sampling"))
    return out


# ---------------------------------------------------------------- LOGIC-006
# 结论与正文冲突：绝对化结论 + 正文存在未决/风险
CONCLUSION_CUE = re.compile(
    r"(综上|结论|总体(?:来看|而言)|最终(?:决定|结论|意见)|会议结论|一句话|总体判断)")
ABSOLUTE_PASS = re.compile(
    r"(一致同意|全部通过|全票通过|无异议|零缺陷|没有问题|均无问题|整体正常|全部合格|没有风险)")
BODY_RISK = re.compile(
    r"(待确认|待定|未决|保留意见|未达成|未通过|不通过|待整改|存在问题|风险|隐患|延期|缺口|异常|投诉)")


def check_conclusion_conflict(text):
    out = []
    segs = sentences(text)
    for idx, (start, seg) in enumerate(segs):
        if not (CONCLUSION_CUE.search(seg) and ABSOLUTE_PASS.search(seg)):
            continue
        # 正文 = 结论句之前的所有句子；只看结论**之前**，避免把结论自己当证据
        body = "".join(s for _, s in segs[:idx])
        if not body.strip():
            continue
        # 「不存在风险」「无明显问题」这类否定不算风险信号：逐处判断其前 8 字
        real = []
        for m in BODY_RISK.finditer(body):
            if NEGATED.search(body[max(0, m.start() - 8): m.end()]):
                continue
            real.append(m.group(0))
        if not real:
            continue
        uniq = list(dict.fromkeys(real))[:3]
        out.append(_finding(
            "LOGIC-006", seg, start,
            f"结论使用绝对化表述「{ABSOLUTE_PASS.search(seg).group(0)}」，"
            f"但正文出现未决/风险信号：{'、'.join(uniq)}——结论与正文证据冲突",
            "把结论改为与正文一致（如「原则通过，遗留 2 项待确认」），或补充闭环证据。",
            0.74, "conclusion_conflict"))
        break       # 每份文档只报一次，避免同一结论反复刷
    return out


# ---------------------------------------------------------------- LOGIC-008
# 绝对化表述与文中事实冲突：100%/零缺陷 等，同时文中承认竞品/问题
ABSOLUTE_NUM = re.compile(
    r"(100\s*%|百分之百|零(?:缺陷|风险|投诉|事故|故障)|无任何(?:问题|风险|缺陷|故障)|"
    r"完全(?:一致|满足|没有)|全部(?:合格|通过))")
# 不可证伪的绝对化断言：「毫无问题」「绝对领先」既没有量化口径，也无法被反证。
# 这类表述**不需要**同句出现反证词就已经是问题——它把"没有被发现的问题"说成"没有问题"。
# 第六轮 B3 就是拿它绕过「绝对化 + 反证同句」这条窄口径的。
ABSOLUTE_UNFALSIFIABLE = re.compile(
    r"(毫无(?:问题|瑕疵|缺陷|风险|差错|破绽)|完美无瑕|无可挑剔|"
    r"绝对(?:领先|优于|安全|可靠|第一)|百分之百(?:正确|可靠|准确|没问题)|"
    r"零(?:瑕疵|差错)|从不出错)")
CONTRADICTION = re.compile(
    r"(竞品|竞争对手|对手|投诉|缺陷|故障|延期|下滑|下降|未达|问题|不足|返修)")
TARGET_WORDS = re.compile(r"(目标|要求|指标|力争|承诺|KPI|考核|计划|预计|预期|希望|不低于|达到)")


def check_absolute_claim(text):
    out = []
    whole = text
    has_contradiction = bool(CONTRADICTION.search(whole))
    for start, seg in sentences(text):
        unfal = ABSOLUTE_UNFALSIFIABLE.search(seg)
        if unfal:
            if TARGET_WORDS.search(seg) or ASPIRATION.search(seg):
                continue            # 目标/承诺不是事实断言
            # 否定语境检查必须**排除绝对化表述自身**：「毫无问题」里含「无…问题」，
            # 拿整句去跑 NEGATED 会把自己否掉——这就是 B3 一开始 0 命中的原因。
            rest_seg = seg[:unfal.start()] + "。" + seg[unfal.end():]
            if NEGATED.search(rest_seg):
                continue
            # 引述他人/竞品的说法（「对方宣称毫无问题」）不算本文件的主张
            if re.search(r"(宣称|声称|自称|号称|广告语|宣传语|对方|竞品)", seg[:unfal.start()]):
                continue
            out.append(_finding(
                "LOGIC-008", seg, start,
                f"不可证伪的绝对化表述「{unfal.group(0)}」：既未给量化口径，也无反证可能；"
                "「未发现问题」不等于「没有问题」",
                "改为可核验的量化表述，并说明结论覆盖面与已知局限。", 0.72,
                "absolute_unfalsifiable"))
            return out              # 每份文档只报一次
        if not has_contradiction:
            continue
        m = ABSOLUTE_NUM.search(seg)
        if not m:
            continue
        if TARGET_WORDS.search(seg) or ASPIRATION.search(seg):
            continue        # 目标/承诺不是事实断言
        if NEGATED.search(seg):
            continue
        # 只在同一句里同时出现反证词时才报；且**排除绝对化表述自身**，
        # 否则「没有对手」会被当成「对手」这个反证词，证据就成了循环论证。
        rest = seg[:m.start()] + seg[m.end():]
        contra = CONTRADICTION.search(rest)
        if not contra:
            continue
        out.append(_finding(
            "LOGIC-008", seg, start,
            f"绝对化表述「{m.group(0)}」与同句的「{contra.group(0)}」冲突"
            "（确定性识别：绝对化 + 反证同句）",
            "改为可核验的量化表述（如「市占率 62%，另有 3 家竞品」）。", 0.76, "absolute_claim"))
        break               # 同样每份文档只报一次
    return out


# ---------------------------------------------------------------- LOGIC-001（金额）
# 中文大写金额 ↔ 阿拉伯数字比对：「人民币壹佰万元整（¥800,000）」是合同里最要命的
# 一类文字差错——大写与小写不一致时，司法实践按大写认定，但付款方按小写付款，
# 争议就此产生。规则层原来完全没有这个能力（第六轮 B9）。
CN_UPPER_AMOUNT = re.compile(r"([零〇壹贰叁肆伍陆柒捌玖拾佰仟万亿两]{2,})\s*元(整)?")
BRACKET_AMOUNT = re.compile(r"[（(]\s*[¥￥]?\s*([\d,]+(?:\.\d+)?)\s*(万|亿)?\s*[）)]")


def check_amount_words(text):
    out = []
    for start, seg in sentences(text):
        for m in CN_UPPER_AMOUNT.finditer(seg):
            cn = regex_check.cn2num(m.group(1))
            if not cn:
                continue
            tail = seg[m.end(): m.end() + 24]
            b = BRACKET_AMOUNT.search(tail)
            if not b:
                continue
            arabic = float(b.group(1).replace(",", "")) * {"万": 1e4, "亿": 1e8}.get(b.group(2) or "", 1)
            if not arabic:
                continue
            if abs(cn - arabic) / arabic > 0.005:      # 容差 0.5%，覆盖角分四舍五入
                out.append(_finding(
                    "LOGIC-001", seg, start,
                    f"大写金额「{m.group(1)}元」= {int(cn):,}，小写「{b.group(0)}」= {int(arabic):,}，"
                    f"相差 {abs(cn - arabic):,.0f} 元（确定性换算比对）",
                    "统一大小写金额，并在合同中明确「大小写不一致时以大写为准」。",
                    0.93, "amount_words"))
                break
    return out


CHECKS = (check_age_tenure, check_sampling, check_conclusion_conflict,
          check_absolute_claim, check_amount_words)


def run_all(text, active_rule_ids=None):
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
        raise SystemExit("用法：python tools/logic_checks.py <文本文件>")
    txt = p.read_text(encoding="utf-8")
    fs = run_all(txt)
    print(f"{p.name}：{len(fs)} 条确定性逻辑发现")
    for f in fs:
        print(f"\n[{f['rule_id']}] conf={f['confidence']} {f['tool']}")
        print(f"  原文：{f['quote'][:70]}")
        print(f"  依据：{f['evidence']}")
