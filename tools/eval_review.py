#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eval_review.py —— 审查质量评测（recall / 标注命中率 / 跨次稳定性）

为什么需要它：
  压力测试报告只统计了「14 条里命中几条」，既没有 precision，也没有重复运行的稳定性。
  单次结果无法区分「能力稳定」与「这次走运」——同一份文本跑 N 次，看哪些错误每次都命中、
  哪些时有时无，才能判断改动是否真的有效。

用法：
  python tools/eval_review.py                                    # 默认对抗样本，3 次，启用 LLM
  python tools/eval_review.py --runs 5 --no-llm                  # 纯规则基线
  python tools/eval_review.py --sample targets/stress_mid_001.txt --labels targets/x.labels.json

隔离：每次运行都把 OUTPUTS / LOGS / LEDGER 指向临时目录，
      不会在真实项目里产生历史报告或台账节点。
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import review          # noqa: E402
import rules_store      # noqa: E402


# ---------------- 标注匹配 ----------------

def issue_haystack(issue):
    """匹配文本：判定依据比 quote 更能反映"模型在说什么"。"""
    return "\n".join(str(issue.get(k) or "") for k in
                     ("quote", "evidence", "suggestion", "rule_text"))


def label_matches(label, text, issue=None):
    """AND-of-OR：每个子数组内任一正则命中即可，所有子数组都要命中。

    issue 传入时，若其维度在 exclude_dimensions 里则直接判否。
    原因：风格规则会把整句问题句原样引用，看起来"命中了"，其实只是复读了句子，
    并没有识别出矛盾——不排除会让召回虚高。
    """
    if issue is not None:
        dim = str(issue.get("dimension") or "")
        if dim and dim in (label.get("exclude_dimensions") or []):
            return False
    for group in label.get("match") or []:
        if not any(re.search(pat, text) for pat in group):
            return False
    return bool(label.get("match"))


def normalize_issue(issue):
    """跨次对比用的指纹：规则 + 归一化后的片段。"""
    q = re.sub(r"\s+", "", str(issue.get("quote") or ""))[:40]
    return f"{issue.get('rule_id')}::{q}"


# ---------------- 运行 ----------------

def run_once(sample, labels, use_llm, type_override=None):
    """在临时目录里跑一次完整审查，返回精简结果（不落真实项目）。"""
    work = Path(tempfile.mkdtemp(prefix="ra-eval-"))
    try:
        outs, logs, ledger = work / "outputs", work / "logs", work / "ledger"
        for d in (outs, logs, ledger):
            d.mkdir(parents=True, exist_ok=True)
        saved = (review.OUTPUTS, review.LOGS, review.LEDGER)
        review.OUTPUTS, review.LOGS, review.LEDGER = outs, logs, ledger
        started = time.perf_counter()
        try:
            report = review.run_one(sample, rules_store.load_active_rules(),
                                    use_llm=use_llm, type_override=type_override)
        finally:
            review.OUTPUTS, review.LOGS, review.LEDGER = saved
        elapsed = time.perf_counter() - started
    finally:
        shutil.rmtree(work, ignore_errors=True)

    issues = report.get("issues") or []
    hay = [(i, issue_haystack(i)) for i in issues]
    hit_labels = [lb["id"] for lb in labels
                  if any(label_matches(lb, t, i) for i, t in hay)]
    labeled_issues = [i for i, t in hay
                      if any(label_matches(lb, t, i) for lb in labels)]
    co = report.get("coverage") or {}
    llm = report.get("llm") or {}
    return {
        "report_id": report.get("review_id"),
        "verdict": report.get("verdict"),
        "target_type": report.get("target_type"),
        "model": llm.get("model"),
        "issues": len(issues),
        "hit_labels": sorted(hit_labels),
        "labeled_issues": len(labeled_issues),
        # 先报再撤的比例：模型自己否定的条数 / 留下的条数
        "excluded_count": len(report.get("excluded") or []),
        "retract_ratio": round(
            len(report.get("excluded") or [])
            / max(1, len(issues) + len(report.get("excluded") or [])), 2),
        "unlabeled_issues": len(issues) - len(labeled_issues),
        "unlabeled_samples": [
            {"rule_id": i.get("rule_id"), "severity": i.get("severity"),
             "quote": str(i.get("quote") or "")[:44]}
            for i in issues
            if not any(label_matches(lb, issue_haystack(i), i) for lb in labels)
        ][:12],
        "fingerprints": sorted({normalize_issue(i) for i in issues}),
        "coverage": {
            "chars_total": co.get("chars_total"),
            "chars_reviewed": co.get("chars_reviewed"),
            "text_coverage_pct": co.get("text_coverage_pct"),
            "complete": co.get("complete"),
            "chunked": co.get("chunked"),
            "chunks": co.get("chunks"),
            "input_truncated_dims": co.get("input_truncated_dimensions"),
            "output_truncated_dims": co.get("output_truncated_dimensions"),
        },
        "tokens": llm.get("tokens"),
        "latency_s": round(elapsed, 1),
        "limitations_head": (report.get("limitations") or [])[:2],
    }


def jaccard(a, b):
    a, b = set(a), set(b)
    return round(len(a & b) / len(a | b) * 100, 1) if (a | b) else 100.0


# ---------------- 汇总 ----------------

def summarize(runs, labels):
    ids = [lb["id"] for lb in labels]
    per_run = [set(r["hit_labels"]) for r in runs]
    union = set().union(*per_run) if per_run else set()
    stable = set.intersection(*per_run) if per_run else set()
    never = [i for i in ids if i not in union]
    flaky = [i for i in ids if i in union and i not in stable]

    pair = []
    for i in range(len(per_run)):
        for j in range(i + 1, len(per_run)):
            pair.append(jaccard(per_run[i], per_run[j]))
    fp_pairs = []
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            fp_pairs.append(jaccard(runs[i]["fingerprints"], runs[j]["fingerprints"]))

    recalls = [round(len(s) / len(ids) * 100) for s in per_run]
    issue_counts = [r["issues"] for r in runs]
    retract = [r.get("retract_ratio", 0) for r in runs]
    labeled = [r["labeled_issues"] for r in runs]
    precision = [round(l / c * 100) if c else 0 for l, c in zip(labeled, issue_counts)]
    return {
        "label_total": len(ids),
        "recall_pct": recalls,
        "recall_avg": round(sum(recalls) / len(recalls), 1) if recalls else 0,
        "labeled_hit_rate_pct": precision,
        "issue_counts": issue_counts,
        "stable": sorted(stable),
        "flaky": sorted(flaky),
        "never": never,
        "retract_ratio": retract,
        "label_set_jaccard_pct": pair,
        "issue_set_jaccard_pct": fp_pairs,
    }


def render_md(res, labels, sample, runs, summary):
    by_id = {lb["id"]: lb["desc"] for lb in labels}
    lines = [
        "# 审查质量评测报告", "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 样本：`{sample}`",
        f"- 运行次数：{len(runs)}",
        f"- 标注错误数：{summary['label_total']}",
        f"- 模型：{runs[0].get('model') or '?'}（每次运行名称相同，编号各自从 001 起）",
        "- 说明：每次运行使用独立临时目录（OUTPUTS/LOGS/LEDGER），不写入真实项目，"
        "因此报告编号不连续、不可用于追溯；真实报告请从控制台查看。",
        "", "## 一、逐次结果", "",
        "| # | 报告 | 异议数 | 命中标注 | 标注命中率 | 召回 | 类型 | token(in/out) | 耗时 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(runs, 1):
        tok = r.get("tokens") or {}
        lines.append(
            f"| {i} | {r['report_id']} | {r['issues']} | {r['labeled_issues']} | "
            f"{round(r['labeled_issues'] / r['issues'] * 100) if r['issues'] else 0}% | "
            f"{round(len(r['hit_labels']) / summary['label_total'] * 100)}% | "
            f"{r['target_type']} | {tok.get('input', 0)}/{tok.get('output', 0)} | {r['latency_s']}s |")

    lines += ["", "## 二、稳定性（同一份文本多次运行）", "",
              f"- 每次召回：{summary['recall_pct']}%（平均 {summary['recall_avg']}%）",
              f"- 标注集合两两 Jaccard：{summary['label_set_jaccard_pct']}%",
              f"- 全量 issue 集合两两 Jaccard：{summary['issue_set_jaccard_pct']}%",
              f"- **每次都命中（稳定能力）**：{len(summary['stable'])} 条 —— {', '.join(summary['stable']) or '无'}",
              f"- **时有时无（掷骰子）**：{len(summary['flaky'])} 条 —— {', '.join(summary['flaky']) or '无'}",
              f"- **从未命中（能力缺口）**：{len(summary['never'])} 条 —— {', '.join(summary['never']) or '无'}",
              "", "## 三、未被标注命中的异议（需人工裁定）", "",
              "> 这些不等于误报：可能是真问题但不在标注集内，也可能是噪声。"
              "列出来是为了让人能快速扫一眼，而不是替人下结论。", "",
              "| # | 规则 | 严重度 | 片段 |", "|---|---|---|---|"]
    for i, r in enumerate(runs, 1):
        lines.append(f"| 运行{i} | | | 共 {r['unlabeled_issues']} 条未命中标注 |")
        for s in r["unlabeled_samples"]:
            lines.append(f"| | {s['rule_id']} | {s['severity']} | {s['quote']} |")

    lines += ["", "## 四、覆盖与截断（本次改动的验收项）", ""]
    for i, r in enumerate(runs, 1):
        c = r["coverage"]
        lines.append(f"- 运行{i}：覆盖 {c.get('chars_reviewed')}/{c.get('chars_total')} 字符"
                     f"（{c.get('text_coverage_pct')}%），complete={c.get('complete')}，"
                     f"输入截断={c.get('input_truncated_dims') or '无'}，"
                     f"输出截断={c.get('output_truncated_dims') or '无'}")
        for lim in r["limitations_head"]:
            lines.append(f"  - 边界：{lim}")

    lines += ["", "## 五、标注明细（判定标准）", "", "| 标注 | 说明 | 是否稳定命中 |", "|---|---|---|"]
    for lb in labels:
        state = ("稳定" if lb["id"] in summary["stable"]
                 else "时有时无" if lb["id"] in summary["flaky"] else "从未命中")
        lines.append(f"| {lb['id']} | {by_id[lb['id']]} | {state} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="审查质量评测：recall / 命中率 / 稳定性")
    ap.add_argument("--sample", default="targets/adversarial_meeting_001.txt")
    ap.add_argument("--labels", default=None, help="默认取 <sample>.labels.json")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--no-llm", action="store_true", help="只跑规则审")
    ap.add_argument("--type", default=None, help="强制指定目标类型，绕开类型推断")
    ap.add_argument("--out-dir", default="outputs/bench")
    ap.add_argument("--cache", action="store_true",
                    help="允许命中语义审缓存（默认关闭：否则稳定性评测必然 100%）")
    ap.add_argument("--init-labels", type=int, default=0, metavar="N",
                    help="为样本生成 N 条空白标注模板（盲测用），不跑审查")
    args = ap.parse_args()

    if not args.cache:
        # 稳定性评测的前提是"每次真的重新调用"；命中缓存会让 Jaccard 恒为 100%
        os.environ["REVIEW_AGENT_LLM_CACHE"] = "0"

    sample = (ROOT / args.sample).resolve() if not Path(args.sample).is_absolute() \
        else Path(args.sample)
    if not sample.exists():
        sys.exit(f"样本不存在：{sample}")
    labels_path = Path(args.labels) if args.labels else \
        sample.with_suffix(".labels.json")
    if args.init_labels:
        # 盲测第一步：**先不跑模型**，只产出空白标签模板，避免被模型输出带偏
        template = {
            "sample": args.sample,
            "note": ("盲测协议：先人工通读原文、独立写出你认定的错误与判定依据，"
                     "再把它编码成 match（AND-of-OR 正则）。写标签时不要看任何模型输出。"),
            "errors": [{"id": f"B{i:02d}", "desc": "", "match": [[]],
                        "exclude_dimensions": ["style"]}
                       for i in range(1, args.init_labels + 1)],
        }
        out = sample.with_suffix(".labels.json")
        if out.exists():
            sys.exit(f"标注文件已存在，未覆盖：{out}")
        out.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"已生成盲标模板：{out}\n填写 desc 与 match 后运行：")
        print(f"  python tools/eval_review.py --sample {args.sample} --runs 1")
        return

    if not labels_path.exists():
        sys.exit(f"缺少标注文件：{labels_path}")
    labels = json.loads(labels_path.read_text(encoding="utf-8"))["errors"]

    print(f"样本 {sample.name} · 标注 {len(labels)} 条 · 运行 {args.runs} 次 · "
          f"LLM={not args.no_llm} · 缓存={'开启' if args.cache else '关闭（保证稳定性可测）'}")
    runs = []
    for i in range(1, args.runs + 1):
        r = run_once(sample, labels, use_llm=(not args.no_llm),
                     type_override=args.type)
        runs.append(r)
        print(f"  第 {i} 次：{r['report_id']} | {r['issues']} 条异议 | "
              f"命中 {len(r['hit_labels'])}/{len(labels)} | "
              f"覆盖 {r['coverage'].get('chars_reviewed')}/"
              f"{r['coverage'].get('chars_total')} 字符 | {r['latency_s']}s")

    summary = summarize(runs, labels)
    print("\n=== 汇总 ===")
    print(f"召回：{summary['recall_pct']}%（平均 {summary['recall_avg']}%）")
    print(f"标注命中率（异议里有多少能对上标注）：{summary['labeled_hit_rate_pct']}%")
    print(f"先报再撤比例：{summary.get('retract_ratio')}")
    print(f"稳定命中 {len(summary['stable'])} 条：{', '.join(summary['stable']) or '无'}")
    print(f"时有时无 {len(summary['flaky'])} 条：{', '.join(summary['flaky']) or '无'}")
    print(f"从未命中 {len(summary['never'])} 条：{', '.join(summary['never']) or '无'}")
    print(f"标注集合两两 Jaccard：{summary['label_set_jaccard_pct']}%")

    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    payload = {"sample": (sample.relative_to(ROOT).as_posix()
                 if sample.is_relative_to(ROOT) else str(sample)),
               "runs": runs, "summary": summary, "labels": labels}
    (out_dir / f"eval-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_md(summary, labels, args.sample, runs, summary)
    (out_dir / f"eval-{stamp}.md").write_text(md, encoding="utf-8")
    print(f"\n已写入 {args.out_dir}/eval-{stamp}.{{json,md}}")


if __name__ == "__main__":
    main()
