#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rule_promote.py —— 候选规则的晋升、回滚与审计

用法：
    python tools/rule_promote.py status                      # 看闭环状态
    python tools/rule_promote.py shadow AUTO-LOGIC-XXXX      # 真实语料预演（不改结论）
    python tools/rule_promote.py promote AUTO-LOGIC-XXXX --by wang
    python tools/rule_promote.py promote AUTO-LOGIC-XXXX --by wang --confirm-manual
    python tools/rule_promote.py promote AUTO-LOGIC-XXXX --by wang --auto   # 仅 logic/style
    python tools/rule_promote.py rollback AUTO-LOGIC-XXXX --by wang
    python tools/rule_promote.py log                         # 晋升/回滚审计

**这是唯一会写 rules/ 的入口。** 审查过程、HTTP 接口都没有写规则库的能力——
这不是疏忽，是设计：被审文档不能获得规则写入权。

每次写入都会：快照当前规则库 → 只新增一个 auto_*.yaml → 自增 rules_version
→ 记一行审计。版本号自增是必须的，否则历史报告会被判成「规则库被篡改」。
"""
import argparse
import sys

import rule_learning as rl  # noqa: E402


def cmd_status(args):
    s = rl.summary()
    print(f"规则库版本：{s['rules_version']}")
    print(f"人工标注：{s['annotations']} 条（其中漏检 {s['negatives']} 条可作为学习种子）")
    print(f"候选队列：{s['candidates']} 条 {s['by_status'] or ''}")
    print(f"本周自动晋升：{s['auto_this_week']} / {s['quota_week']} 条")
    print(f"历史晋升：{len(s['promoted'])} 条｜回滚：{len(s['rolled_back'])} 条")
    cfg = rl.load_config()
    print("\n门禁参数：")
    for k, v in cfg["gate"].items():
        print(f"  {k}: {v}")
    print(f"  人工确认维度：{'、'.join(cfg['approval']['manual_only_dimensions'])}")
    print(f"  可自动晋升维度：{'、'.join(cfg['approval']['auto_ok_dimensions'])}"
          f"（严重度上限 {cfg['severity']['max_auto']}）")
    return 0


def cmd_shadow(args):
    payload = rl.shadow(args.candidate_id)
    m = payload["metrics"]
    print(f"{args.candidate_id} shadow 预演完成（{m['corpus_size']} 份语料，不改任何结论）")
    print(f"  若启用会新增异议 {payload['new_issues']} 条"
          f"（上限 {rl.load_config()['shadow']['max_new_issues']}）")
    print(f"  其中来源文档 {m['hit_doc_count']} 份｜非来源文档 {m['offtarget_count']} 条")
    print(f"  门禁：{'通过' if payload['gate']['passed'] else '拒绝'}")
    for r in payload["gate"]["reasons"]:
        print(f"    · {r}")
    if payload["over_limit"]:
        print("  ⚠ 新增条数超限：这类规则太吵，不建议晋升")
    return 0 if payload["gate"]["passed"] else 1


def cmd_promote(args):
    mode = "auto" if args.auto else "manual"
    row = rl.promote(args.candidate_id, by=args.by, mode=mode,
                     confirm_manual=args.confirm_manual)
    print(f"已晋升 {row['rule_id']} → {row['file']}")
    print(f"  规则版本：{row['version_before']} → {row['version_after']}")
    print(f"  快照：{row['snapshot']}")
    print(f"  方式：{'自动通道' if mode == 'auto' else '人工审批'}（by {args.by}）")
    print("\n注意：规则集变了，历史报告会转为 stale（黄），需重新审查才能用新规则。")
    return 0


def cmd_rollback(args):
    row = rl.rollback(args.rule_id, by=args.by, reason=args.reason or "")
    print(f"已回滚 {args.rule_id}：移除 {', '.join(row['removed'])}")
    print(f"  规则版本：{row['version_before']} → {row['version_after']}")
    print(f"  快照：{row['snapshot']}")
    return 0


def cmd_audit(args):
    """漂移复评：已晋升规则在当前语料上重跑门禁，不通过就自动回滚。"""
    rows = rl.audit(args.rule, by=args.by)
    if not rows:
        print("没有需要复评的已晋升规则")
        return 0
    print(f"复评 {len(rows)} 条已晋升规则：")
    for r in rows:
        m = r["metrics"]
        tag = "OK" if r["ok"] else ("已回滚" if r["rolled_back"] else "异常")
        print(f"  {r['rule_id']:<22}{tag:<6} "
              f"命中 {m['hit_doc_count']} 份｜非来源 {m['offtarget_count']} 条｜"
              f"来源内误报 {m.get('internal_offtarget_count', 0)} 条")
        for why in r["reasons"]:
            print(f"      · {why[:100]}")
    return 0 if all(r["ok"] for r in rows) else 1


def cmd_log(args):
    rows = rl.read_jsonl(rl.LOG_PATH)
    if not rows:
        print("暂无晋升/回滚记录")
        return 0
    # 审计日志会记录多种操作，别把非 promote 的一律当成回滚（第一版就打印错了）
    labels = {"promote": "晋升", "rollback": "回滚", "rename": "改名"}
    for r in rows:
        op = r.get("op", "?")
        tag = labels.get(op, op)
        extra = f"  {r.get('from')} → {r.get('to')}" if op == "rename" else ""
        if op in ("rollback", "audit") and r.get("reason"):
            extra += f"  ｜原因：{r['reason'][:80]}"
        print(f"{r['ts']}  {tag}  {r['rule_id']:<20} by {r.get('by')} "
              f"[{r.get('mode', '-')}]  {r['version_before']} → {r['version_after']}{extra}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="自学习闭环：候选晋升 / 回滚 / 审计")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status"); st.set_defaults(func=cmd_status)
    sh = sub.add_parser("shadow"); sh.add_argument("candidate_id")
    sh.set_defaults(func=cmd_shadow)
    pr = sub.add_parser("promote"); pr.add_argument("candidate_id")
    pr.add_argument("--by", required=True, help="操作人（审计需要）")
    pr.add_argument("--auto", action="store_true",
                    help="走自动通道（仅 logic/style 且受每周配额限制）")
    pr.add_argument("--confirm-manual", action="store_true",
                    help="显式确认人工审批（safety/compliance/fact 必须）")
    pr.set_defaults(func=cmd_promote)
    rb = sub.add_parser("rollback"); rb.add_argument("rule_id")
    rb.add_argument("--by", required=True)
    rb.add_argument("--reason", default="", help="回滚原因（写进审计）")
    rb.set_defaults(func=cmd_rollback)
    au = sub.add_parser("audit", help="漂移复评：已晋升规则重跑门禁，不通过自动回滚")
    au.add_argument("--rule", default=None, help="只复评指定规则")
    au.add_argument("--by", default="audit")
    au.set_defaults(func=cmd_audit)
    lg = sub.add_parser("log"); lg.set_defaults(func=cmd_log)
    args = ap.parse_args()
    try:
        return args.func(args)
    except rl.LearningError as e:
        print(f"拒绝：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
