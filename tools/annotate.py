#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""annotate.py —— 记录一条人工标注（自学习闭环唯一的可信信号）

用法：
    # 漏检：这句话有问题，但系统没报 → 这是新规则的种子
    python tools/annotate.py --kind false_negative --by wang \\
        --target targets/x.txt --dimension logic --quote "同比下降 -3.2%"

    # 误报：报告里这条其实不是问题 → 规则太松的证据
    python tools/annotate.py --kind false_positive --by wang \\
        --report R-20261024-083 --issue ISS-0003

    # 确认：这条确实是问题（用来守住已有效规则，防止被"优化"掉）
    python tools/annotate.py --kind confirmed --by wang \\
        --report R-20261024-083 --issue ISS-0001

标注写入 `state/annotations.jsonl`（append-only），不修改任何报告。
`quote` 必须是原文逐字片段——否则学习信号无法回查，等于允许任意文本进规则库。
"""
import argparse
import sys

import rule_learning as rl  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="记录人工标注（误报 / 漏检 / 确认）")
    ap.add_argument("--kind", required=True,
                    choices=["false_negative", "false_positive", "confirmed"])
    ap.add_argument("--by", required=True, help="标注人（来源可信度靠它）")
    ap.add_argument("--target", help="被审文件（漏检标注必填）")
    ap.add_argument("--quote", help="原文逐字片段（漏检标注必填）")
    ap.add_argument("--dimension", choices=sorted(rl.DIMENSIONS),
                    help="漏检标注的问题维度")
    ap.add_argument("--report", help="报告 id（误报/确认标注必填）")
    ap.add_argument("--issue", help="issue_id（误报/确认标注必填）")
    ap.add_argument("--note", default="", help="备注")
    args = ap.parse_args()
    try:
        item = rl.add_annotation(
            kind=args.kind, by=args.by, target_path=args.target, quote=args.quote,
            dimension=args.dimension, report_id=args.report, issue_id=args.issue,
            note=args.note)
    except rl.LearningError as e:
        print(f"拒绝：{e}")
        return 2
    print("已记录标注：")
    print(f"  类型：{item['kind']}   维度：{item['dimension'] or '-'}")
    print(f"  对象：{item['target_path']}")
    if item["quote"]:
        print(f"  片段：「{item['quote'][:60]}」")
    if item["issue_id"]:
        print(f"  异议：{item['report_id']} / {item['issue_id']}")
    print("  存储：state/annotations.jsonl")
    if item["kind"] == "false_negative":
        print("\n下一步：python tools/rule_miner.py mine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
