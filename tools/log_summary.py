#!/usr/bin/env python3
"""log_summary.py —— 批量统计成功率 / 耗时 / token (Demo 工具)

聚合 logs/ 下所有 R-* 的 cost.json：
    python log_summary.py
"""
import os
import sys
import json
import glob


def summarize(logs_root):
    rows = []
    for d in sorted(glob.glob(os.path.join(logs_root, "R-*"))):
        cost = os.path.join(d, "cost.json")
        if not os.path.exists(cost):
            continue
        with open(cost, encoding="utf-8") as f:
            rows.append(json.load(f))
    if not rows:
        return {"reports": 0, "note": "暂无日志"}
    total_calls = sum(r.get("tool_calls", 0) for r in rows)
    total_ok = sum(r.get("tool_success", 0) for r in rows)
    total_fail = sum(r.get("tool_failures", 0) for r in rows)
    total_skip = sum(r.get("tool_skipped", 0) for r in rows)
    completed = total_ok + total_fail
    rate = (f"{total_ok}/{completed} = {total_ok / completed * 100:.0f}%"
            if completed else "N/A（全部 skipped）")
    total_lat = sum(r.get("total_latency_ms", 0) for r in rows)
    total_in = sum(r.get("llm_tokens", {}).get("input", 0) for r in rows)
    total_out = sum(r.get("llm_tokens", {}).get("output", 0) for r in rows)
    total_llm_calls = sum(r.get("llm_calls", 0) for r in rows)
    total_llm_fail = sum(r.get("llm_failures", 0) for r in rows)
    models = sorted({r.get("llm_model") for r in rows if r.get("llm_model")})
    return {
        "reports": len(rows),
        "tool_calls": total_calls,
        "tool_success": total_ok,
        "tool_failures": total_fail,
        "tool_skipped": total_skip,
        "success_rate": rate,
        "total_latency_ms": total_lat,
        "avg_latency_ms": int(total_lat / len(rows)) if rows else 0,
        "llm_calls": total_llm_calls,
        "llm_failures": total_llm_fail,
        "llm_models": models,
        "llm_tokens": {"input": total_in, "output": total_out},
    }


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(json.dumps(summarize(os.path.join(root, "logs")), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
