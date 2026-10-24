#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地大模型真实测评（LLM Bench）。

对「本地大模型 + review-agent 审查链路」做端到端真实实测，产出可复现的
JSON / Markdown 证据。五个阶段：

  S1 环境      读取项目 LLM 配置（端点 / 模型 / 可用性）
  S2 预算探针  扫描 max_tokens，量化「思考型模型 token 饥饿」现象
               （reasoning 吃满预算 → content 为空）
  S3 项目封装  走 llm_client.LLMClient.chat()，验证正文非空且可解析
  S4 单维审查  真实调用 llm_review.review_dimension()，检查 issue 质量
  S5 全量审查  调用 review.run_one(use_llm=True)，校验 review_mode == "rule+llm"

用法：
    python tools/llm_bench.py
    python tools/llm_bench.py --model qwen3.5:9b --target targets/answer_001.txt
    python tools/llm_bench.py --skip-budget --out outputs/bench.json
    python tools/llm_bench.py --dim safety          # 指定单维测量的维度

退出码：0 = 全部通过；1 = 存在失败阶段。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import llm_client          # noqa: E402
import llm_review          # noqa: E402
import review              # noqa: E402

BAR = "─" * 74
PROBE_PROMPT = ("请判断下面这句话是否存在事实错误，只回答“有错误”或“无错误”："
                "地球是平的。")


# --------------------------------------------------------------------------
# S1 环境
# --------------------------------------------------------------------------
def s1_env(model_override=None, max_tokens=None, timeout=None):
    """读取项目 LLM 配置（强制启用），可覆盖模型 / token 预算 / 超时。"""
    try:
        cfg = llm_client.load_config(use_llm=True)
    except Exception as e:                                   # noqa: BLE001
        return None, {"ok": False, "error": f"配置加载失败：{e}"}

    if model_override:
        cfg.model = model_override
    if max_tokens:
        cfg.max_tokens = max_tokens
    if timeout:
        cfg.timeout = timeout

    info = {
        "ok": bool(cfg.available),
        "base_url": cfg.base_url,
        "endpoint": cfg.endpoint,
        "model": cfg.model,
        "available": cfg.available,
        "enabled": cfg.enabled,
        "api_key_set": bool(cfg.api_key),
        "timeout": cfg.timeout,
        "max_tokens": cfg.max_tokens,
        "max_retries": cfg.max_retries,
        "max_input_chars": cfg.max_input_chars,
        "source": cfg.source,
    }
    return cfg, info


# --------------------------------------------------------------------------
# S2 预算探针：直连端点，扫描 max_tokens
# --------------------------------------------------------------------------
def raw_chat(endpoint, model, prompt, max_tokens, timeout, api_key=""):
    """绕开项目封装直接打 OpenAI 兼容端点，返回正文/思考链长度。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        obj = json.loads(resp.read().decode("utf-8", "replace"))
    elapsed = int((time.perf_counter() - t0) * 1000)
    choices = obj.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
    usage = obj.get("usage") or {}
    return {
        "max_tokens": max_tokens,
        "latency_ms": elapsed,
        "content_len": len(content),
        "content_preview": content[:120],
        "reasoning_len": len(reasoning),
        "finish_reason": choices[0].get("finish_reason") if choices else None,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }


def s2_budget(cfg, budgets=(8, 64, 256, 1024, 2048)):
    rows = []
    for mt in budgets:
        try:
            row = raw_chat(cfg.endpoint, cfg.model, PROBE_PROMPT, mt,
                           timeout=cfg.timeout, api_key=cfg.api_key)
            row["ok"] = True
        except Exception as e:                                # noqa: BLE001
            row = {"ok": False, "max_tokens": mt, "error": f"{type(e).__name__}: {e}"}
        rows.append(row)
        print(f"    max_tokens={mt:<6} content_len={row.get('content_len', '-'):<6} "
              f"reasoning_len={row.get('reasoning_len', '-'):<6} "
              f"finish={row.get('finish_reason')} "
              f"({row.get('latency_ms', '-')}ms)")
    return rows


# --------------------------------------------------------------------------
# S3 项目封装
# --------------------------------------------------------------------------
def s3_wrapper(cfg):
    client = llm_client.LLMClient(cfg)
    t0 = time.perf_counter()
    res = client.chat([{"role": "user", "content": "只回复两个字：可用"}])
    content = (res.content or "").strip()
    return {
        "ok": bool(content),
        "model": res.model,
        "latency_ms": res.latency_ms,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "usage": res.usage,
        "content_len": len(content),
        "content_preview": content[:200],
        "note": None if content else "封装返回空正文：检查 max_tokens 是否小于思考链长度",
    }


# --------------------------------------------------------------------------
# S4 单维度真实审查
# --------------------------------------------------------------------------
def s4_dimension(cfg, target_path, dim=None):
    text = target_path.read_text(encoding="utf-8", errors="replace")
    ttype = review.detect_type(target_path, text)
    rules = review.load_rules()
    dims = review.DIM_FOR_TYPE.get(ttype, [])
    target_dim = dim or next(
        (d for d in dims if any(r.get("dimension") == d for r in rules)), None)
    if not target_dim:
        return {"ok": False, "error": "没有可测维度（该类型下无启用中的规则）"}

    dim_rules = [r for r in rules if r.get("dimension") == target_dim]
    client = llm_client.LLMClient(cfg)
    t0 = time.perf_counter()
    res = llm_review.review_dimension(client, target_dim, dim_rules, text, cfg)
    issues = res["issues"]
    return {
        "ok": True,
        "dimension": target_dim,
        "target_type": ttype,
        "rules_count": len(dim_rules),
        "issues_count": len(issues),
        "dropped_count": len(res["dropped"]),
        "raw_chars": res["raw_chars"],
        "latency_ms": res["latency_ms"],
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "usage": res["usage"],
        "model": res["model"],
        "issues": issues,
        "dropped": res["dropped"],
    }


# --------------------------------------------------------------------------
# S5 全量审查
# --------------------------------------------------------------------------
def s5_full_review(cfg, target_path):
    rules = review.load_rules()
    t0 = time.perf_counter()
    report = review.run_one(target_path, rules, use_llm=True)
    llm_meta = report.get("llm") or {}
    # 成本明细不在 report 顶层，而在 logs/<review_id>/cost.json
    cost = {}
    cost_path = review.LOGS / str(report.get("review_id")) / "cost.json"
    if cost_path.exists():
        try:
            cost = json.loads(cost_path.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            cost = {}
    return {
        "ok": True,
        "review_id": report.get("review_id"),
        "verdict": report.get("verdict"),
        "review_mode": report.get("review_mode"),
        "issues_count": len(report.get("issues") or []),
        "llm_calls": llm_meta.get("calls"),
        "llm_failures": llm_meta.get("failures"),
        "llm_model": llm_meta.get("model"),
        "llm_tokens": llm_meta.get("tokens"),
        "llm_available": llm_meta.get("available"),
        "llm_reason": llm_meta.get("reason"),
        "llm_errors": llm_meta.get("errors") or [],
        "dimensions": llm_meta.get("dimensions") or {},
        "cost": cost,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "output_json": f"outputs/{report.get('review_id')}.json",
        "output_md": f"outputs/{report.get('review_id')}.md",
    }


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------
def render_markdown(data):
    s1, s3 = data["s1_env"], data["s3_wrapper"]
    s4, s5 = data["s4_dimension"], data["s5_full_review"]
    lines = [
        "# 本地大模型真实测评报告",
        "",
        f"- 生成时间：{data['generated_at']}",
        f"- 测评对象：{data['config'].get('endpoint')} · `{data['config'].get('model')}`",
        f"- 被审样本：`{data['target']}`",
        "",
        "## 总判定",
        "",
        f"**{data['verdict']['text']}**",
        "",
        "| 阶段 | 结果 | 关键指标 |",
        "|---|---|---|",
        f"| S1 环境 | {'✅' if s1.get('available') else '❌'} | available={s1.get('available')}"
        f" · max_tokens={s1.get('max_tokens')} |",
        f"| S2 预算探针 | {'✅' if data['s2_budget'] else '—'} | 见下表 |",
        f"| S3 项目封装 | {'✅' if s3.get('ok') else '❌'} | content_len={s3.get('content_len')}"
        f" · {s3.get('latency_ms')}ms |",
        f"| S4 单维审查 | {'✅' if s4.get('ok') else '❌'} | {s4.get('dimension')} 维产出 "
        f"{s4.get('issues_count')} 条 issue |",
        f"| S5 全量审查 | {'✅' if s5.get('review_mode') == 'rule+llm' else '❌'} | "
        f"mode={s5.get('review_mode')} · calls={s5.get('llm_calls')} |",
        "",
        "## S2 token 预算扫描（关键结论）",
        "",
        "| max_tokens | content 长度 | reasoning 长度 | finish_reason | 耗时 |",
        "|---|---|---|---|---|",
    ]
    for row in data.get("s2_budget") or []:
        if not row.get("ok"):
            lines.append(f"| {row.get('max_tokens')} | - | - | ERROR | - |")
            continue
        lines.append(
            f"| {row['max_tokens']} | {row['content_len']} | {row['reasoning_len']} | "
            f"{row['finish_reason']} | {row['latency_ms']}ms |")
    lines += [
        "",
        "> 思考型模型（deepseek-r1 / qwen3 系）会先生成 `reasoning` 思考链；"
        "预算小于思考链长度时 `content` 恒为空——这正是原 `/api/llm/test` 硬编码 "
        "`max_tokens=8` 导致「连接成功但回复空白」的原因。",
        "",
        "## S4 单维度审查产出",
        "",
    ]
    if s4.get("ok"):
        lines.append(f"维度 **{s4['dimension']}**（对象类型 `{s4['target_type']}`），"
                     f"喂入 {s4['rules_count']} 条规则，产出 {s4['issues_count']} 条 issue，"
                     f"过滤 {s4['dropped_count']} 条。")
        lines += ["", "| rule_id | quote | confidence | evidence |", "|---|---|---|---|"]
        for it in s4.get("issues") or []:
            q = (it.get("quote") or "").replace("|", "\\|")[:60]
            ev = (it.get("evidence") or "").replace("|", "\\|")[:80]
            lines.append(f"| {it.get('rule_id')} | {q} | {it.get('confidence')} | {ev} |")
    else:
        lines.append(f"未执行：{s4.get('error')}")
    lines += [
        "",
        "## S5 全量审查",
        "",
        f"- 报告编号：`{s5.get('review_id')}`",
        f"- 裁定：**{s5.get('verdict')}**",
        f"- 审查模式：**{s5.get('review_mode')}**",
        f"- LLM 调用次数：{s5.get('llm_calls')}（失败 {s5.get('llm_failures')}）",
        f"- Token 用量：{s5.get('llm_tokens')}",
        f"- 产物：`{s5.get('output_json')}` / `{s5.get('output_md')}`",
        "",
    ]
    dims = s5.get("dimensions") or {}
    if dims:
        lines += ["### 逐维度调用明细", "",
                  "| 维度 | 状态 | 耗时(ms) | issue | 错误 |", "|---|---|---|---|---|"]
        for d, info in dims.items():
            if isinstance(info, dict):
                lines.append(
                    f"| {d} | {info.get('status', '-')} | {info.get('latency_ms', '-')} | "
                    f"{info.get('issues', '-')} | {str(info.get('error') or '-')[:60]} |")
    if s5.get("llm_errors"):
        lines += ["", "### 错误", ""] + [f"- {e}" for e in s5["llm_errors"]]
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="本地大模型真实测评")
    ap.add_argument("--model", help="覆盖模型名，如 deepseek-r1:8b")
    ap.add_argument("--target", default="targets/answer_001.txt", help="被审样本")
    ap.add_argument("--dim", help="单维度测试的维度名，默认自动选")
    ap.add_argument("--max-tokens", type=int, help="覆盖 max_tokens")
    ap.add_argument("--timeout", type=int, help="覆盖超时秒数")
    ap.add_argument("--out", help="JSON 结果输出路径")
    ap.add_argument("--md", help="Markdown 报告输出路径")
    ap.add_argument("--skip-budget", action="store_true", help="跳过耗时的 S2 扫描")
    args = ap.parse_args()

    target_path = (ROOT / args.target) if not Path(args.target).is_absolute() else Path(args.target)
    if not target_path.exists():
        print(f"找不到被审样本：{target_path}")
        return 1

    print(BAR)
    print("本地大模型真实测评 · review-agent LLM Bench")
    print(BAR)

    print("\n[S1] 环境")
    cfg, env = s1_env(args.model, args.max_tokens, args.timeout)
    if cfg is None:
        print(f"  ❌ {env}")
        return 1
    for k in ("endpoint", "model", "available", "max_tokens", "timeout", "source"):
        print(f"  {k:<12}= {env.get(k)}")
    if not env["available"]:
        print("  ❌ LLM 不可用（缺 API Key 或非本地端点），终止")
        return 1

    budget_rows = []
    if not args.skip_budget:
        print("\n[S2] token 预算探针（直连端点）")
        budget_rows = s2_budget(cfg)

    print("\n[S3] 项目封装 llm_client.LLMClient.chat()")
    s3 = s3_wrapper(cfg)
    print(f"  ok={s3['ok']} content_len={s3['content_len']} "
          f"latency={s3['latency_ms']}ms usage={s3['usage']}")
    if s3["content_len"]:
        print(f"  正文: {s3['content_preview'][:100]}")

    print("\n[S4] 单维度真实审查")
    s4 = s4_dimension(cfg, target_path, args.dim)
    if s4.get("ok"):
        print(f"  维度={s4['dimension']} 类型={s4['target_type']} 规则={s4['rules_count']} "
              f"→ issue={s4['issues_count']} 过滤={s4['dropped_count']} "
              f"耗时={s4['elapsed_ms']}ms usage={s4['usage']}")
        for it in s4["issues"][:5]:
            print(f"    · [{it.get('rule_id')}] conf={it.get('confidence')} "
                  f"{(it.get('quote') or '')[:46]}")
    else:
        print(f"  ❌ {s4.get('error')}")

    print("\n[S5] 全量审查 run_one(use_llm=True)")
    s5 = s5_full_review(cfg, target_path)
    print(f"  {s5['review_id']} | 裁定={s5['verdict']} | 模式={s5['review_mode']} | "
          f"LLM调用={s5['llm_calls']} 失败={s5['llm_failures']} | "
          f"token={s5['llm_tokens']} | {s5['elapsed_ms']}ms")
    if s5.get("llm_errors"):
        for e in s5["llm_errors"]:
            print(f"    ! {e}")

    ok = (s3["ok"] and s4.get("ok") and s5.get("review_mode") == "rule+llm")
    verdict = {
        "passed": ok,
        "text": ("✅ 真实调用成功：本地模型已被审查链路实际调用，"
                 f"报告模式为 {s5.get('review_mode')}"
                 if ok else
                 "❌ 未达成 rule+llm：大模型未被有效调用或全部维度失败"),
    }
    print(f"\n{BAR}\n判定：{verdict['text']}\n{BAR}")

    data = {
        "bench_version": "1.0",
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "target": str(target_path.relative_to(ROOT)).replace("\\", "/"),
        "config": env,
        "s1_env": env,
        "s2_budget": budget_rows,
        "s3_wrapper": s3,
        "s4_dimension": s4,
        "s5_full_review": s5,
        "verdict": verdict,
    }
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # 放在 outputs/bench/ 子目录：避免被 outputs 根下的历史报告扫描当成一份报告
    bench_dir = ROOT / "outputs" / "bench"
    out_json = Path(args.out) if args.out else bench_dir / f"llm-bench-{stamp}.json"
    out_md = Path(args.md) if args.md else bench_dir / f"llm-bench-{stamp}.md"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_markdown(data), encoding="utf-8")
    print(f"\n证据文件：\n  {out_json}\n  {out_md}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
