#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""golden_eval.py —— 金标准测试集批量评测（recall / 误报，纯规则基线）

为什么需要它（2026-10-28，用户主轴指令③）：
  此前只有 4 条人工标注、没有标准答案集——改一条规则根本不知道是涨了召回
  还是误伤了干净文本，自学习闭环在空转。本脚本把「改规则 → 对数字」变成固定动作。

用例约定：
  tests/golden/cases/<name>.txt        待审文本
  tests/golden/labels/<name>.labels.json  标注（eval_review 的 AND-of-OR match 格式）
                                          **没有标注文件 = 干净对照**（期望 0 条 issue）
  每个用例都是独立小文本，走完整 run_one（纯规则，无 LLM），
  eval_review.run_once 的临时目录隔离保证不污染真实 outputs/ledger。

用法：
  python tools/golden_eval.py                    # 跑并与 tests/golden/baseline.json 对比
  python tools/golden_eval.py --save-baseline    # 把本次结果存为基线（首次/有意改进后）
  python tools/golden_eval.py --list             # 只列用例不跑
退出码：0 = 不差于基线；1 = 召回下降或误报上升（改规则前请先看这个数）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import eval_review  # noqa: E402

SUITE = ROOT / "tests" / "golden"
CASES = SUITE / "cases"
LABELS = SUITE / "labels"
BASELINE = SUITE / "baseline.json"


def run_suite():
    rows = []
    labels_total = labels_hit = fp_total = 0
    for case in sorted(CASES.glob("*.txt")):
        lab_path = LABELS / f"{case.stem}.labels.json"
        labels = []
        if lab_path.exists():
            labels = json.loads(lab_path.read_text(encoding="utf-8"))["errors"]
        r = eval_review.run_once(case, labels, use_llm=False)
        hit = len(r["hit_labels"])
        total = len(labels)
        fps = r["unlabeled_samples"]
        labels_total += total
        labels_hit += hit
        fp_total += r["unlabeled_issues"]
        rows.append({
            "case": case.name,
            "kind": "positive" if total else "clean",
            "labels_total": total,
            "labels_hit": hit,
            "unlabeled": r["unlabeled_issues"],
            "unlabeled_detail": [
                f"{s['rule_id']}|{s['quote']}" for s in fps],
            "issue_count": r["issues"],
        })
    recall = round(labels_hit / labels_total * 100, 1) if labels_total else 0.0
    # 精确率按"采纳的 issue"算：命中标注的 issue 数 = labels_hit（一条标注算一条），
    # 分母是 命中 + 未被任何标注覆盖的 issue。误报为 0 时精确率是 100%。
    reported = labels_hit + fp_total
    precision = round(labels_hit / reported * 100, 1) if reported else 0.0
    return {"recall_pct": recall, "precision_pct": precision,
            "labels_total": labels_total,
            "labels_hit": labels_hit, "fp_total": fp_total,
            "cases": rows,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}


def render(res, baseline):
    lines = [
        "# 金标准测试集·基线报告", "",
        f"- 时间：{res['ts']}",
        f"- 用例：{len(res['cases'])}（阳性 {sum(1 for c in res['cases'] if c['kind'] == 'positive')}，"
        f"干净对照 {sum(1 for c in res['cases'] if c['kind'] == 'clean')}）",
        f"- 标注错误：{res['labels_total']} 条，命中 {res['labels_hit']} 条 → **召回 {res['recall_pct']}%**",
        f"- 精确率：**{res.get('precision_pct', 0)}%**（命中 {res['labels_hit']} / 报出 "
        f"{res['labels_hit'] + res['fp_total']} 条）",
        f"- 误报（干净对照上的 issue + 阳性用例里标注外的 issue）：**{res['fp_total']} 条**",
        "",
        "| 用例 | 类型 | 命中/标注 | 误报 | 未命中/多报明细 |",
        "|---|---|---|---|---|",
    ]
    for c in res["cases"]:
        detail = "；".join(c["unlabeled_detail"][:4]) or "—"
        lines.append(f"| {c['case']} | {c['kind']} | {c['labels_hit']}/{c['labels_total']} "
                     f"| {c['unlabeled']} | {detail} |")
    if baseline:
        lines += ["", f"与基线对比：召回 {baseline['recall_pct']}% → {res['recall_pct']}%，"
                  f"误报 {baseline['fp_total']} → {res['fp_total']}"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="金标准测试集批量评测")
    ap.add_argument("--save-baseline", action="store_true",
                    help="把本次结果存为基线（首次构建或有意改进后）")
    ap.add_argument("--list", action="store_true", help="只列用例不跑")
    ap.add_argument("--out-dir", default="outputs/bench")
    args = ap.parse_args()

    if args.list:
        for c in sorted(CASES.glob("*.txt")):
            kind = "positive" if (LABELS / f"{c.stem}.labels.json").exists() else "clean"
            print(f"{kind:8} {c.name}")
        return

    baseline = None
    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    res = run_suite()
    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out_dir / f"golden-{stamp}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render(res, baseline)
    (out_dir / f"golden-{stamp}.md").write_text(md, encoding="utf-8")

    print(f"用例 {len(res['cases'])} · 标注 {res['labels_total']} 命中 {res['labels_hit']}"
          f" → 召回 {res['recall_pct']}% · 精确率 {res.get('precision_pct', 0)}%"
          f" · 误报 {res['fp_total']}")
    for c in res["cases"]:
        if (c["kind"] == "positive" and c["labels_hit"] < c["labels_total"]) \
                or (c["kind"] == "clean" and c["unlabeled"] > 0):
            print(f"  [未达] {c['case']} hit {c['labels_hit']}/{c['labels_total']} "
                  f"fp {c['unlabeled']} :: {'；'.join(c['unlabeled_detail'][:3])}")
    print(f"已写入 {args.out_dir}/golden-{stamp}.{{json,md}}")

    if args.save_baseline:
        BASELINE.write_text(json.dumps(res, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"基线已保存 {BASELINE.relative_to(ROOT)}")
        return

    if baseline:
        worse = (res["labels_hit"] < baseline["labels_hit"]
                 or res["fp_total"] > baseline["fp_total"])
        print(f"对比基线：召回 {baseline['recall_pct']}% → {res['recall_pct']}%，"
              f"误报 {baseline['fp_total']} → {res['fp_total']}")
        sys.exit(1 if worse else 0)


if __name__ == "__main__":
    main()
