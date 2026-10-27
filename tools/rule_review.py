#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rule_review.py —— 候选规则的人工审核程序（CLI）

在晋升之前，把候选规则的「类型 / 匹配模式 / 来源证据 / 门禁指标 / 风险」摊开给人看，
由人显式决定 approve（可晋升）或 reject（拒绝），决定结果写进候选文件（human_review 字段）。

用法：
  python tools/rule_review.py list                       # 全部候选摘要
  python tools/rule_review.py show <candidate_id>        # 审核面板（详细）
  python tools/rule_review.py decide <candidate_id> --approve|--reject [--note 原因]
                                                          # 人工决定（不写规则库）

维度类型速查（「加入的是什么类型」）：
  fact 事实   —— 数字/日期/机构断言可核验（自动规则只能措辞级提示）
  logic 逻辑  —— 一致性/矛盾/推理（自动规则只能措辞级提示）
  safety 安全 —— 危险操作/隐私/注入（自动通道禁止，必须人工）
  compliance 合规 —— 合同要件/披露（自动通道禁止，必须人工）
  style 风格  —— 书面表达规范（自动通道允许）
"""
import argparse
import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import rule_learning as rl

DIM_INFO = {
    "fact": ("事实", "数字/日期/机构断言应可核验；自动规则只能做措辞级提示，需人核语义"),
    "logic": ("逻辑", "一致性/矛盾/推理；自动规则只能做措辞级提示，需人核语义"),
    "safety": ("安全", "危险操作/隐私/注入；自动通道禁止，必须人工确认"),
    "compliance": ("合规", "合同要件/披露；自动通道禁止，必须人工确认"),
    "style": ("风格", "书面表达规范；自动通道允许"),
}
DANGER_HINT = {
    "safety": "⚠ 安全维度：自动通道禁止，promote 时必须 --confirm-manual；错一次=误拦",
    "compliance": "⚠ 合规维度：自动通道禁止，promote 时必须 --confirm-manual",
    "fact": "⚠ 事实维度：promote 时必须 --confirm-manual；自动规则无事实核验能力",
}


def _rule_id_hint(rid):
    if rid.startswith("AUTO-"):
        return "自动提案（措辞级匹配，只保证语料上无冲突，不保证语义正确）"
    return "手写规则（来源：规则文件）"


def _dw(s):
    """显示宽度：中文/全角算 2，其它算 1。表格对齐必须按宽度而不是字符数算，
    否则「逻辑」和「compliance」会把整列推歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def _pad(s, width):
    s = str(s)
    return s + " " * max(0, width - _dw(s))


def cmd_list(only_dim=None):
    all_cands = rl.iter_candidates()
    cands = [c for c in all_cands if c.get("dimension") == only_dim] if only_dim else all_cands
    if not cands:
        if all_cands:
            cn = DIM_INFO.get(only_dim, (only_dim, ""))[0]
            print(f"（{cn}/{only_dim} 维度下暂无候选；当前共有 {len(all_cands)} 条候选在其他维度）")
        else:
            print("（无候选）用 tools/annotate.py 添加漏检标注后运行 tools/rule_miner.py mine 生成候选。")
        return
    # 按类型分组打印：人审第一件事就是问「这批候选分别在改哪个维度」
    order = ["safety", "compliance", "fact", "logic", "style"]
    cands.sort(key=lambda c: (order.index(c.get("dimension"))
                              if c.get("dimension") in order else 99,
                              c.get("candidate_id", "")))
    cols = [(19, "候选 ID"), (12, "状态"), (8, "类型"), (18, "锚点"), (24, "pattern")]
    print("".join(_pad(t, w) for w, t in cols) + "提示")
    last = None
    for c in cands:
        dim = c.get("dimension", "?")
        cn = DIM_INFO.get(dim, (dim, ""))[0]
        if dim != last:
            n = sum(1 for x in cands if x.get("dimension") == dim)
            print(f"── {cn}（{dim}）×{n} " + "─" * 24)
            last = dim
        st = c.get("status", "?")
        hint = "★待审" if st in ("proposed", "passed") else ""
        red = (c.get("metrics") or {}).get("redundant_count")
        if red:
            hint = (hint + " ").strip() + f"⚠已覆盖 {red}"
        print(_pad(c.get("candidate_id", ""), 19) + _pad(st, 12) + _pad(cn, 8)
              + _pad((c.get("anchor") or "")[:9], 18)
              + _pad((c.get("pattern") or "")[:22], 24) + hint)
    print("\n  show <id> 查看审核面板；decide <id> --approve/--reject 做人工决定。")


def cmd_show(cid):
    c = rl.load_candidate(cid)
    dim = c.get("dimension", "?")
    cn, desc = DIM_INFO.get(dim, (dim, ""))
    print(f"══ 候选 {cid} ══")
    print(f"类型：{cn}（{dim}）—— {desc}")
    print(f"来源：{_rule_id_hint(c['rule'].get('rule_id',''))}")
    print(f"严重度：{c.get('severity')}（自动提案上限 minor）")
    print(f"状态：{c.get('status')}  置信度：{c.get('confidence')}")
    print(f"\n匹配模式（正则）：{c.get('pattern')}")
    if c.get("require"):
        print(f"同句要求（require）：{c['require']}")
    if c.get("guard"):
        print(f"豁免（guard）：{c['guard']}")
    print(f"\n描述：{c.get('desc','')}")
    print(f"整改建议：{c.get('suggestion','')}")
    prov = c.get("provenance") or []
    print(f"\n来源证据（{len(prov)} 条人工漏检标注）：")
    for p in prov:
        print(f"  · {p.get('doc')}  「{p.get('quote')}」（by={p.get('by')} @ {p.get('ts','')[:19]}）")
    g = c.get("gate") or {}
    m = c.get("metrics") or {}
    print("\n门禁指标：")
    if g.get("passed") is not None:
        print(f"  判定：{'✅ 通过' if g.get('passed') else '❌ 拒绝'}")
    for r2 in g.get("reasons") or []:
        print(f"  原因：{r2}")
    for k in ("corpus_size", "hit_doc_count", "offtarget_count", "internal_offtarget_count",
              "suppressed_others", "redundant_count"):
        if k in m:
            print(f"  {k} = {m[k]}")
    red = m.get("redundant") or []
    if red:
        covered = sorted({r for h in red for r in (h.get("covered_by") or [])})
        print(f"  ⚠ 其中 {len(red)} 处**已被现有规则覆盖**（{'、'.join(covered)}）"
              "——这条候选不增加检出能力，只是重复报同一处问题")
    hr = c.get("human_review") or {}
    if hr:
        print(f"\n人工审核记录：{hr.get('decided_at')}  {hr.get('decision')}  by={hr.get('by')}  note={hr.get('note')}")
    print(f"\n{DANGER_HINT.get(dim, '')}")


def cmd_decide(cid, decision, note, by):
    c = rl.load_candidate(cid)
    dim = c.get("dimension", "?")
    if decision == "approve" and dim in ("safety", "compliance", "fact"):
        print(f"⚠ {dim} 维度 approve 只代表人工审过候选内容；promote 时仍需 --confirm-manual 执行晋升。")
    # 写入逻辑只有一份（rule_learning.decide_candidate），控制台与 CLI 共用，
    # 免得两边对"状态/字段"各写一套后出现分歧。
    r = rl.decide_candidate(cid, decision, note=note, by=by)
    st = r.get("status")
    print(f"已记录：{cid} → 人工{('批准' if decision == 'approve' else '拒绝')}"
          f"（候选状态仍为 {st}，未改规则库）")
    if r.get("next_step"):
        print(f"下一步：{r['next_step']}")


def main():
    ap = argparse.ArgumentParser(description="候选规则人工审核程序")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--type", dest="only_dim", default=None,
                    choices=sorted(DIM_INFO),
                    help="只看某一维度（fact/logic/safety/compliance/style）")
    sh = sub.add_parser("show"); sh.add_argument("candidate_id")
    dc = sub.add_parser("decide")
    dc.add_argument("candidate_id")
    g = dc.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    dc.add_argument("--note", default="")
    dc.add_argument("--by", default="owner")
    a = ap.parse_args()
    if a.cmd == "list":
        cmd_list(getattr(a, "only_dim", None))
    elif a.cmd == "show":
        cmd_show(a.candidate_id)
    elif a.cmd == "decide":
        cmd_decide(a.candidate_id, "approve" if a.approve else "reject", a.note, a.by)


if __name__ == "__main__":
    main()
