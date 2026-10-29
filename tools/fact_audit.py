#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fact_audit.py —— 已入库事实断言的体检（用新门禁的预演，回测老库）

一句话：**回答"库里这 9 条，每一条到底会不会把对的判错"。**

## 为什么不能直接对着现库跑 `fact_promote.shadow()`

`shadow(候选)` 的算法是 `库 + 候选  vs  库`，增量归**候选**。拿现库里的断言当候选
再跑一遍，等号两边都是现库 —— 增量恒为 0，什么都测不出来（自己跟自己比）。

所以审计必须**摘除法**：

    本次审计  =  库            vs  库 − 本条
    归因      =  只有"摘掉它才消失"的误报，才算这条断言的账

这才是"它会不会把对的判错"的因果证据。

## 两类结论的可靠性不同，读报告时必须分开

- `harmful`（制造增量误报）—— **可靠**。摘掉它误报就没了，因果清楚。
- `weak_signal`（零正贡献）  —— **会低估**。同一句里还有别的断言在报时，
  摘掉本条标注照样被同伴命中，于是它看起来"零贡献"。那不是"没用"，
  是**被同伴替它报了**，两种情形用 `on_from_others` 区分：
  `shadowed`（有人在替它报）/ `weak_signal`（谁都没报，语料真没覆盖这个属性）。

## 五档结论

| verdict | 含义 | 该怎么办 |
|---|---|---|
| `kept` | 找回了真错误，且没制造误报 | 保留 |
| `harmful` | 让**正确**陈述变成误报 | 改口径或删（误报零容忍） |
| `shadowed` | 单独贡献为 0，但同句有同伴在报 | 不是缺陷；撤销归因即可 |
| `weak_signal` | 语料提到实体，但零正贡献 | 补一条该属性的阳性用例 |
| `unmeasured` | 语料里根本没有这个实体 | 补用例，否则这条断言永远不可验 |

## 三条约束

1. **只读**。不写 facts.json、不写 state/，只打印（`--json` 才落盘）。
2. 语料预过滤沿用 `_exercised_by`：没提到该实体的语料不可能因它开火，
   跳过是等价变换，但跳了多少份必须报出来。
3. 预演默认跟随生产配置（rule+llm）。`--rule-only` 只用于排查，
   结果里 `mode` 会写明 `rule-only`，**不能拿它当生产模式下的结论**。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import fact_promote as fp  # noqa: E402

VERDICTS = ("kept", "harmful", "shadowed", "weak_signal", "unmeasured")

# 给人看的说明：为什么这一档是这一档
WHY = {
    "kept": "找回了真错误，且没让任何正确陈述变成误报",
    "harmful": "摘掉它误报就消失 —— 是这条断言把对的判错了",
    "shadowed": "单独贡献 0，但同句还有别的断言在报（被同伴替它报了），不是缺陷",
    "weak_signal": "语料提到该实体，摘掉它之后谁也报不出来 —— 语料没覆盖这个属性",
    "unmeasured": "金标准语料里没有一份提到该实体，这次什么都没验到",
}


def _expected_str(a):
    t = str(a.get("type") or "")
    if t == "date":
        return f"{a.get('expected_year')} 年"
    exp = a.get("expected") or {}
    if isinstance(exp, dict) and "num" in exp:
        return f"{exp.get('num')} {exp.get('unit') or ''}".strip()
    if a.get("expected_text"):
        return str(a["expected_text"])
    return ""


def audit_assertion(a, cases_all=None, mode=None):
    """单条断言的体检。返回 item dict（不写任何文件）。"""
    cases_all = fp._corpus_cases() if cases_all is None else cases_all
    mode = fp._shadow_llm(None) if mode is None else mode

    base = fp._read_json(fp.FACTS, {"assertions": []})
    key = fp._assertion_key(a)
    without = copy.deepcopy(base)
    # 同 key 可能不止一条（历史上重复导入过），全摘掉才测得干净
    without["assertions"] = [x for x in (base.get("assertions") or [])
                             if fp._assertion_key(x) != key]

    cand = {"entity": a.get("entity")}
    cases = [c for c in cases_all if fp._exercised_by(cand, c[2])]

    item = {
        "entity": a.get("entity"), "attr": a.get("attr"),
        "type": a.get("type"), "expected": _expected_str(a),
        "rule_id": a.get("rule_id"),
        "corpus_total": len(cases_all), "corpus_exercised": len(cases),
        "corpus_cases": [c[0].stem for c in cases],
    }
    if not cases:
        item.update(on_hits=0, on_labels=[], on_from_others=0, off_delta=0,
                    off_baseline=0, verdict="unmeasured", why=WHY["unmeasured"])
        return item

    # before = 没有本条；after = 现库。方向不能反，否则"增量"会变成负数含义
    before_off, before_on = fp._shadow_pass(without, cases, mode)
    after_off, after_on = fp._shadow_pass(base, cases, mode)

    delta = {k: v for k, v in after_off.items() if k not in before_off}
    on_new = sorted(after_on - before_on, key=str)

    if delta:
        verdict = "harmful"
    elif on_new:
        verdict = "kept"
    elif before_on:
        verdict = "shadowed"
    else:
        verdict = "weak_signal"

    item.update(
        on_hits=len(on_new), on_labels=[fp._label_key(x) for x in on_new],
        on_from_others=len(before_on),
        off_delta=len(delta),
        off_baseline=len(before_off),
        off_detail=[{"case": k[0], "quote": v} for k, v in sorted(delta.items())[:5]],
        off_baseline_cases=sorted({k[0] for k in before_off}),
        verdict=verdict, why=WHY[verdict],
    )
    return item


def audit_all(mode=None, only=None):
    cases_all = fp._corpus_cases()
    mode = fp._shadow_llm(None) if mode is None else mode
    assertions = fp._read_json(fp.FACTS, {"assertions": []}).get("assertions") or []
    items = []
    for a in assertions:
        if only and str(a.get("entity")) != only:
            continue
        items.append(audit_assertion(a, cases_all, mode))
    summary = {v: 0 for v in VERDICTS}
    for it in items:
        summary[it["verdict"]] += 1
    return {
        "mode": "rule+llm" if mode else "rule-only",
        "facts_version": fp.knowledge_version(),
        "corpus_total": len(cases_all),
        "summary": dict(total=len(items), **summary),
        "items": items,
    }


def render(res):
    lines = [
        f"事实断言体检（{res['mode']}，knowledge_version={res['facts_version']}）",
        f"语料 {res['corpus_total']} 份；断言 {res['summary']['total']} 条"
        f" → 保留 {res['summary']['kept']} / 有害 {res['summary']['harmful']}"
        f" / 被覆盖 {res['summary']['shadowed']} / 无信号 {res['summary']['weak_signal']}"
        f" / 未测到 {res['summary']['unmeasured']}",
        "",
    ]
    mark = {"kept": "OK ", "harmful": "!! ", "shadowed": "-- ",
            "weak_signal": "?  ", "unmeasured": "·· "}
    for i, it in enumerate(res["items"], 1):
        left = f"{i}. {it['entity']}·{it['attr']}（{it['type']} {it['expected']}）"
        print_tail = (f"语料 {it['corpus_exercised']}/{it['corpus_total']} · "
                      f"正贡献 {it['on_hits']} · 增量误报 {it['off_delta']} · "
                      f"存量误报 {it['off_baseline']}")
        lines.append(f"{mark[it['verdict']]}{left}")
        lines.append(f"    {print_tail} · 同伴已报 {it['on_from_others']}")
        lines.append(f"    → {it['verdict']}：{it['why']}")
        for d in it.get("off_detail") or []:
            lines.append(f"    ! {d['case']}「{d['quote'][:40]}」")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="已入库事实断言体检（只读，不改库）")
    ap.add_argument("--rule-only", action="store_true",
                    help="只跑规则（快，但**不是生产模式**，结论要标出来）")
    ap.add_argument("--llm", action="store_true",
                    help="强制启用 LLM（默认跟随生产配置）")
    ap.add_argument("--only", default=None, help="只看某个实体")
    ap.add_argument("--json", nargs="?", const="-", default=None,
                    help="输出 JSON（省略值=stdout，给路径=写文件）")
    args = ap.parse_args(argv)

    mode = False if args.rule_only else (True if args.llm else None)
    res = audit_all(mode, args.only)

    if args.json == "-":
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(render(res))
        if args.json:
            p = Path(args.json)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            print(f"\n已写入 {p}")

    # 退出码只看"把对的判错了"：那是必须修的红灯。
    # 未测到 / 无信号是语料的债，不阻断（但会一直显示在这里）。
    return 1 if res["summary"]["harmful"] else 0


if __name__ == "__main__":
    sys.exit(main())
