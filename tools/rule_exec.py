#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rule_exec.py —— 规则「执行方式」的唯一事实源

## 为什么需要这一层

规则文件里的 `method` 是**标签**，不是执行器。同一个 method 名可能对应
有代码的检查器，也可能只是给模型的提示词。结果就是：报告里一条 blocker，
用户无法判断它是「代码算出来的」还是「模型读了一遍觉得是」——这两者的可信度
差一个量级。

这个模块把「谁能跑这条规则」显式登记下来，供三处消费：

    · 报告：rule_execution 块 + limitations 里的「仅提示」清单
    · 接口：GET /api/rules 每条规则带 capability
    · 控制台：规则表新增「执行方式」列

## 三种执行方式

| kind | 含义 | 可信度 |
|---|---|---|
| `deterministic` | 有专用执行器（事实库 / 算术 / 正则 / checklist / lint） | 可复现，可单测 |
| `declarative` | 规则自带 `pattern`，由 `pattern_rules` 通用引擎执行 | 可复现，但模式是人/机器写的，覆盖有限 |
| `prompt_only` | 只作为分维 prompt 的「重点提示」 | **依赖模型，跨次可能漂移** |

新增规则时如果没登记执行器，这里会诚实地把它算作 `prompt_only`——
不登记不等于有执行器。
"""

# 有专用执行器的规则（与 review.py 里的实现一一对应）。
# 改代码时如果新增/删除了执行器，必须同步这里，否则报告会误报执行方式。
_BUILTIN = {
    "FACT-001": "fact_review·事实库断言",
    "FACT-002": "fact_review·事实库比对",
    "FACT-003": "fact_review·事实库比对",
    "FACT-004": "fact_review·口径/百分比正则",
    "LOGIC-001": ("arith_check·加总/占比/占比上限/占比合计/单位换算/同口径反向 "
                  "+ logic_checks·大小写金额"),
    "LOGIC-002": "arith_check·日期与历时",
    "LOGIC-004": "logic_checks·年龄与工龄",
    "LOGIC-005": "logic_checks·样本代表性",
    "LOGIC-006": "logic_checks·结论与正文冲突",
    "LOGIC-007": "arith_check·入职年份与自述年限",
    "LOGIC-008": "logic_checks·绝对化表述",
    "LOGIC-009": "arith_check·方向词与负号同现",
    "SAFE-001": "safety_review·危险操作词（含防御语境豁免）",
    "SAFE-002": "safety_review·违法操作词（含防御语境豁免）",
    "SAFE-003": "safety_review·隐私信息（手机号/身份证正则 + 银行卡 Luhn + 住址结构）",
    "SAFE-004": "safety_review·注入句式（显式命令 + 授权主张/免除约束结构）",
    "STYLE-001": "regex·emoji",
    "STYLE-002": "regex·口语词（含地名豁免）",
    "STYLE-003": "regex·情绪化表达",
    "STYLE-004": "static_scan·lint + 密钥扫描",
    "STYLE-005": "regex·用词规范（截止/截至、异形词）",
    "COMP-001": "compliance_review·主体要件",
    "COMP-002": "compliance_review·生效/终止要件",
    "COMP-003": "compliance_review·争议解决要件",
    "COMP-004": "compliance_review·数据披露正则",
    "FORMAT-001": "docx_format·版式检查（正文字体）",
    "FORMAT-002": "docx_format·版式检查（正文字号）",
    "FORMAT-003": "docx_format·版式检查（首行缩进）",
    "FORMAT-004": "docx_format·版式检查（行距）",
    "FORMAT-005": "docx_format·版式检查（标题层级）",
    "FORMAT-006": "docx_format·版式检查（图表编号一致性）",
}

_LABEL = {"deterministic": "确定性", "declarative": "声明式", "prompt_only": "仅提示"}


def capability(rule):
    """返回 {"kind", "executor", "label"}。未登记执行器 → prompt_only。"""
    rid = str(rule.get("rule_id") or "")
    execs = []
    if rid in _BUILTIN:
        execs.append(_BUILTIN[rid])
    if rule.get("pattern"):
        execs.append("pattern_rules·声明式")
    if rid.startswith("SEC-") and not execs:
        # 安全审 Agent 的检查器是包内实现（凭据/依赖按维度分派）
        execs.append("security-audit·检查器")
    if not execs:
        return {"kind": "prompt_only", "executor": "", "label": _LABEL["prompt_only"]}
    kind = "declarative" if (rule.get("pattern") and not (rid in _BUILTIN)) else "deterministic"
    return {"kind": kind, "executor": " + ".join(execs), "label": _LABEL[kind]}


def summarize(rules):
    """按执行方式汇总，返回 {kind: [rule_id...], "executors": {rid: 文案}}。"""
    out = {"deterministic": [], "declarative": [], "prompt_only": [], "executors": {}}
    for r in rules:
        cap = capability(r)
        out[cap["kind"]].append(r["rule_id"])
        if cap["executor"]:
            out["executors"][r["rule_id"]] = cap["executor"]
    return out


def counts(rules):
    s = summarize(rules)
    return {k: len(s[k]) for k in ("deterministic", "declarative", "prompt_only")}
