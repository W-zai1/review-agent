#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rule_miner.py —— 从人工标注里挖掘候选规则

用法：
    python tools/rule_miner.py mine            # 聚类并生成候选（不写规则库）
    python tools/rule_miner.py mine --limit 1  # 本轮最多生成 1 条
    python tools/rule_miner.py gate AUTO-LOGIC-XXXX   # 跑门禁，给候选定性
    python tools/rule_miner.py list            # 看候选队列与指标
    python tools/rule_miner.py show AUTO-LOGIC-XXXX   # 看单条候选的全部依据

它**不会**写 rules/。写库是 rule_promote.py 的事，且必须过审批。
"""
import argparse
import sys

# 以 `python tools/rule_miner.py` 运行时，sys.path[0] 就是 tools/，
# 因此同目录的 rule_learning 可直接导入（不要在这里加路径拼接）
import rule_learning as rl  # noqa: E402


def _fmt_metrics(m):
    if not m:
        return "未评估"
    return (f"语料 {m.get('corpus_size', 0)} 份｜命中来源文档 "
            f"{m.get('hit_doc_count', 0)} 份｜新增 {len(m.get('hits') or [])} 条"
            f"（其中非来源 {m.get('offtarget_count', 0)} 条）"
            f"｜压掉其他结论 {m.get('suppressed_others', 0)} 条")


CODE_SUFFIX = (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".cpp", ".c")


def cmd_mine(args):
    # 代码文件默认只跑安全/风格维度，logic 类自动规则不会生效（第四轮 V7）
    code_docs = [a["target_path"] for a in rl.trusted_negatives()
                 if str(a.get("target_path", "")).lower().endswith(CODE_SUFFIX)]
    if code_docs:
        print(f"注意：{len(code_docs)} 条标注来自代码文件，而代码审查只跑 安全/风格 两维；"
              "从这些标注学到的 logic 规则在代码审查里不会生效。")
    created, skipped = rl.mine(limit=args.limit)
    print(f"从 {len(rl.trusted_negatives())} 条可信漏检标注中挖掘：")
    if created:
        print(f"\n生成候选 {len(created)} 条（状态=proposed，尚未过门禁）：")
        for c in created:
            print(f"  {c['candidate_id']}  [{c['dimension']}] 锚点「{c['anchor']}」"
                  f"  来源 {len(c['source_docs'])} 份文档")
            print(f"      pattern: {c['pattern'][:90]}")
    else:
        print("  未生成新候选")
    if skipped:
        print("\n跳过：")
        for s in skipped:
            print(f"  [{s['dimension']}]「{s['anchor']}」—— {s['reason']}")
    if created:
        print("\n下一步：python tools/rule_miner.py gate <candidate_id>")
        print("     门禁通过后：python tools/rule_promote.py shadow <candidate_id>")
    return 0


def cmd_gate(args):
    cand = rl.load_candidate(args.candidate_id)
    if cand.get("status") == "promoted":
        print(f"{args.candidate_id} 已晋升，无需再过门禁")
        return 0
    metrics = rl.evaluate(cand)
    passed, reasons = rl.gate(cand, metrics)
    cand["metrics"] = metrics
    cand["gate"] = {"passed": passed, "reasons": reasons}
    cand["status"] = "passed" if passed else "rejected"
    cand["gated_at"] = rl.now_iso()
    rl.save_candidate(cand)

    print(f"{args.candidate_id} 门禁结果：{'通过' if passed else '拒绝'}")
    print(f"  {_fmt_metrics(metrics)}")
    hits = metrics.get("hits") or []
    if hits:
        print("  命中明细：")
        for h in hits[:8]:
            print(f"    {h['doc']}  ←  {h['quote'][:52]}")
    if reasons:
        print("  拒绝原因：")
        for r in reasons:
            print(f"    · {r}")
    return 0 if passed else 1


def cmd_list(args):
    cands = rl.iter_candidates()
    if not cands:
        print("候选队列为空。先记录人工漏检标注：python tools/annotate.py --help")
        return 0
    print(f"候选队列 {len(cands)} 条（规则库当前版本 {rl.rules_version()}）\n")
    for c in cands:
        gate = c.get("gate") or {}
        mark = {"promoted": "已晋升", "rejected": "已拒绝",
                "passed": "可晋升", "rolled_back": "已回滚"}.get(c.get("status"), "待评估")
        print(f"  {c['candidate_id']:<20} [{c['dimension']:<10}] {mark:<5} "
              f"「{c['anchor']}」")
        print(f"       {_fmt_metrics(c.get('metrics'))}")
        if gate.get("reasons"):
            print(f"       拒绝原因：{gate['reasons'][0][:90]}")
    return 0


def cmd_show(args):
    c = rl.load_candidate(args.candidate_id)
    print(f"候选 {c['candidate_id']}")
    print(f"  状态：{c.get('status')}    维度：{c['dimension']}    严重度：{c['severity']}")
    print(f"  锚点：{c['anchor']}")
    print(f"  pattern：{c['pattern']}")
    print(f"  require：{c['require'] or '(无)'}")
    print(f"  confidence：{c['confidence']}")
    print(f"  生成时间：{c.get('created_at')}")
    print("\n  来源（人工标注，逐条可回查）：")
    for p in c.get("provenance") or []:
        print(f"    · {p['doc']} ｜「{p['quote'][:50]}」 ｜by {p['by']} @ {p['ts']}")
    print(f"\n  门禁：{'通过' if (c.get('gate') or {}).get('passed') else '未通过'}")
    for r in (c.get("gate") or {}).get("reasons") or []:
        print(f"    · {r}")
    print(f"  指标：{_fmt_metrics(c.get('metrics'))}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="自学习闭环：候选规则挖掘")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mine", help="从人工漏检标注聚类生成候选")
    m.add_argument("--limit", type=int, default=None)
    m.set_defaults(func=cmd_mine)
    g = sub.add_parser("gate", help="对候选跑门禁（全语料回归）")
    g.add_argument("candidate_id")
    g.set_defaults(func=cmd_gate)
    ls = sub.add_parser("list", help="列出候选队列")
    ls.set_defaults(func=cmd_list)
    sh = sub.add_parser("show", help="查看单条候选详情")
    sh.add_argument("candidate_id")
    sh.set_defaults(func=cmd_show)
    args = ap.parse_args()
    try:
        return args.func(args)
    except rl.LearningError as e:
        print(f"错误：{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
