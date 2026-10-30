# -*- coding: utf-8 -*-
"""影子报告：新流水线（v1）能自己说清的那部分结论。

为什么单独一份、而不是直接复用旧报告：
    旧报告里有一半内容来自新流水线**还没有的**能力——目的比对（purpose）、
    法条库（law_library）、库外核验（research）、整改单（action_plan）、
    报告哈希链（integrity.chain）。这些块不是「格式」，是**结论**；
    新链路没算就不能写，写了就是伪造。

所以 v1 产出的是一份**自描述的影子报告**：

    * 只包含它真的算过的东西（裁定、发现、证据、争议、覆盖率、节点审计）
    * `unsupported_blocks` 显式列出「旧报告有、这份没有」的块
    * 落在 `outputs/v1/` 而不是 `outputs/`，不冒充正式报告

这张「未支持清单」同时也是切流的验收单：它清零之前，v1 不能替代 legacy。
"""
from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from ..adapters.issue_codec import EncodedIssue, decode_issue
from ..domain import Severity
from ..runtime import RunState, surviving_findings

SCHEMA_VERSION = "1.0-v1-shadow"
ENGINE = "architecture-v1"

# 旧报告有、v1 还没有的块。切流前这份清单必须清零（或逐项给出替代方案）。
UNSUPPORTED_BLOCKS: tuple[tuple[str, str], ...] = (
    ("law_library", "法条库引用与条文覆盖未接入"),
    ("research", "库外事实核验（S5.6）未接入"),
)

# v1 已经有的 integrity 链里缺的那一小块：报告哈希链有，日志哈希没有。
LOGS_HASH_LIMITATION = ("integrity.logs_hash 为空：v1 未写决策/工具日志"
                        "（旧报告会对 logs/ 目录取哈希）")


def _stats(findings: Sequence[Any]) -> dict[str, int]:
    stats = {key.value: 0 for key in Severity}
    for finding in findings:
        stats[finding.severity.value] = stats.get(finding.severity.value, 0) + 1
    return stats


def _origin_counts(findings: Sequence[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        key = finding.origin.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _coverage(state: RunState) -> dict[str, Any]:
    artifacts = state.artifacts
    text = artifacts.get("target_text") or ""
    coverage: dict[str, Any] = {
        "chars_total": len(text),
        "analysis": artifacts.get("analysis_stats"),
        "semantic": (artifacts.get("semantic_stats") or {}).get("coverage"),
    }
    evidence_dropped = artifacts.get("evidence_dropped") or ()
    coverage["evidence_dropped"] = len(tuple(evidence_dropped))
    return coverage


def _review_mode(state: RunState) -> str:
    """有语义审产物 = rule+llm；否则 rule-only。"""
    return "rule+llm" if state.artifacts.get("semantic_stats") else "rule-only"


def _limitations(state: RunState, *, coverage: Mapping[str, Any],
                 logs_hash: str | None) -> list[str]:
    """按「读者最需要先知道什么」排序，口径与旧报告的 limitations 对齐。

    旧报告的限制条目散在 run_one 里按场景拼；这里按**类别**给，规则固定：
        未支持块 → 模式边界 → 文本覆盖 → 证据丢弃 → 日志缺失 → 节点告警
    """
    items = [f"未接入：{key}（{why}）" for key, why in UNSUPPORTED_BLOCKS]
    if _review_mode(state) == "rule-only":
        items.append("本次为纯规则审：未调用模型，语义类问题不在覆盖范围内")
    semantic = coverage.get("semantic")
    if isinstance(semantic, Mapping):
        if not semantic.get("complete", True):
            items.append(f"语义审仅覆盖 {semantic.get('text_coverage_pct', 0)}% 原文；"
                         "未覆盖部分未被审查")
        for failed in semantic.get("failed_chunks") or []:
            if isinstance(failed, Mapping):
                items.append(f"「{failed.get('dim')} × 块{failed.get('chunk')}」"
                             "未完成模型复核")
    dropped = int(coverage.get("evidence_dropped") or 0)
    if dropped:
        items.append(f"证据阶段丢弃 {dropped} 条引用无法回查原文的发现；"
                     "它们不进报告，详见 state 的 evidence_dropped")
    if logs_hash is None:
        items.append(LOGS_HASH_LIMITATION)
    for warning in state.warnings:
        items.append(f"节点告警：{warning}")
    return items


def build_shadow_report(
    state: RunState,
    *,
    target_meta: Mapping[str, Any] | None = None,
    events: Sequence[Any] = (),
    checkpoints: Sequence[Any] = (),
    integrity_inputs: Mapping[str, Any] | None = None,
    logs_hash: str | None = None,
) -> dict[str, Any]:
    """把 RunState 收敛成一份可读的影子报告（纯函数，不读文件、不看时间）。

    `logs_hash`：`logs/v1/<review_id>/` 的目录哈希；传 None 表示这次没写日志，
    limitations 里会明说（旧报告有这一层，不能默默没有）。
    """
    findings = surviving_findings(state)
    decision = state.decision
    meta = dict(target_meta or state.artifacts.get("target_meta") or {})
    coverage = _coverage(state)
    inputs = dict(integrity_inputs or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "review_id": state.review_id,
        "run_id": state.run_id,
        "review_mode": _review_mode(state),
        "target": {
            "path": meta.get("path") or state.artifacts.get("target_path"),
            "type": meta.get("type"),
            "type_confidence": meta.get("type_confidence"),
            "chars": meta.get("chars") or len(state.artifacts.get("target_text") or ""),
            "hash": state.artifacts.get("target_hash"),
            "encoding": meta.get("encoding"),
        },
        "verdict": decision.verdict.value if decision else None,
        "decision": (None if decision is None else {
            "rationale": decision.rationale,
            "risk_level": decision.risk_level.value,
            "reasons": list(decision.reasons),
            "rule_refs": list(decision.rule_refs),
            "requires_human_review": decision.requires_human_review,
            "policy_id": decision.policy_id,
        }),
        "stats": _stats(findings),
        "by_origin": _origin_counts(findings),
        "issues": [decode_issue(EncodedIssue(finding=f)) for f in findings],
        "evidence": [
            {"evidence_id": ev.id,
             "issue_id": ev.finding_ids[0] if ev.finding_ids else None,
             "quote": ev.quote,
             "location": ev.location.label,
             "verification": ev.verification.value,
             "verified_by": ev.verified_by}
            for ev in state.evidence
        ],
        "disputes": [
            {"id": d.id, "point": d.point, "issue_ids": list(d.issue_ids),
             "status": d.status.value, "between": list(d.between)}
            for d in state.disputes
        ],
        "coverage": coverage,
        "purpose": None,            # 由调用方挂（需要目的解析结果）；没填目的就是 None
        "action_plan": None,        # 由调用方挂（需要整改模块与事实库），没挂就是 None
        "limitations": _limitations(state, coverage=coverage, logs_hash=logs_hash),
        "unsupported_blocks": [{"block": key, "reason": why}
                               for key, why in UNSUPPORTED_BLOCKS],
        "integrity": {
            "target_hash": inputs.get("target_hash")
            or state.artifacts.get("target_hash"),
            "ruleset_hash": inputs.get("ruleset_hash"),
            "rules_dir_hash": inputs.get("rules_dir_hash"),
            "rules_version": inputs.get("rules_version"),
            "agent_dir_hash": inputs.get("agent_dir_hash"),
            "agent_version": inputs.get("agent_version"),
            "knowledge_hash": inputs.get("knowledge_hash"),
            "knowledge_version": inputs.get("knowledge_version"),
            "report_hash": None,
            "logs_hash": logs_hash,
            "prev_report_hash": None,
        },
        "audit": {
            "events": [e.as_dict() if hasattr(e, "as_dict") else dict(e)
                       for e in events],
            "checkpoints": [
                {"stage": getattr(c, "stage", "").value
                 if hasattr(getattr(c, "stage", None), "value") else str(c),
                 "sequence": getattr(c, "sequence", None),
                 "state_hash": getattr(c, "state_hash", None)}
                for c in checkpoints
            ],
            "warnings": list(state.warnings),
            "completed_stages": [s.value for s in state.completed_stages],
        },
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def render_shadow_md(report: Mapping[str, Any]) -> str:
    """影子报告的 Markdown 版：先讲结论与边界，再列条目。"""
    lines: list[str] = []
    target = report.get("target") or {}
    lines.append(f"# 审查报告（{report.get('engine')}）")
    lines.append("")
    lines.append(f"- 审查号：`{report.get('review_id')}`")
    lines.append(f"- 对象：`{target.get('path') or '未知'}`"
                 f"（{target.get('type') or '未知类型'}，{target.get('chars') or 0} 字符）")
    lines.append(f"- 裁定：**{report.get('verdict') or '未裁定'}**")
    stats = report.get("stats") or {}
    lines.append("- 统计：" + " / ".join(f"{k} {v}" for k, v in stats.items()))
    decision = report.get("decision") or {}
    if decision:
        lines.append(f"- 风险等级：{decision.get('risk_level')}；"
                     f"需人工复核：{'是' if decision.get('requires_human_review') else '否'}")
        for reason in decision.get("reasons") or []:
            lines.append(f"  - {reason}")
    lines.append("")
    lines.append("## 边界（这份报告没有做什么）")
    for item in report.get("unsupported_blocks") or []:
        lines.append(f"- **{item['block']}**：{item['reason']}")
    lines.append("")
    issues = report.get("issues") or []
    lines.append(f"## 发现（共 {len(issues)} 条）")
    if not issues:
        lines.append("")
        lines.append("未发现问题。注意：本报告只覆盖已接入的检查，未覆盖项见上方边界。")
    for issue in issues:
        lines.append("")
        lines.append(f"### {issue.get('issue_id')} · {issue.get('dimension')} · "
                     f"{issue.get('severity')} · {issue.get('rule_id')}")
        lines.append(f"- 原文：`{issue.get('quote')}`")
        lines.append(f"- 位置：{issue.get('location')}")
        lines.append(f"- 依据：{issue.get('evidence')}")
        if issue.get("suggestion"):
            lines.append(f"- 建议：{issue['suggestion']}")
    purpose = report.get("purpose") or {}
    if purpose.get("goals"):
        lines.append("")
        lines.append(f"## 审查目的比对（{purpose.get('judge', {}).get('mode') or '规则'}）")
        lines.append(f"- 目的：{purpose.get('text') or ''}")
        status_cn = {"found": "找到证据", "not_found": "未找到证据",
                     "partial": "部分达成", "unknown": "未判定"}
        for goal in purpose["goals"]:
            status = status_cn.get(goal.get("status"), str(goal.get("status")))
            label = goal.get("question") or goal.get("id") or "（未命名目标）"
            note = goal.get("note") or ""
            lines.append(f"- **{label}**：{status}{f'（{note}）' if note else ''}")
        if any(g.get("status") == "not_found" for g in purpose["goals"]):
            lines.append("- 注意：未找到证据 ≠ 不存在，已写入 limitations")
    evidence = report.get("evidence") or []
    if evidence:
        lines.append("")
        lines.append(f"## 证据校验（共 {len(evidence)} 条）")
        for row in evidence:
            lines.append(f"- `{row.get('evidence_id')}` → {row.get('issue_id')}："
                         f"{row.get('verification')}"
                         f"（{row.get('verified_by') or '无校验方'}）")
    plan = report.get("action_plan") or {}
    if plan.get("top"):
        lines.append("")
        lines.append(f"## 整改建议（{plan.get('headline')}）")
        for item in plan["top"]:
            lines.append(f"- `{item.get('issue_id')}` {item.get('rule_id')}"
                         f"（{item.get('severity')}）：{item.get('summary') or item.get('how') or ''}")
            if item.get("how"):
                lines.append(f"  - 怎么改：{item['how']}")
            if item.get("consequence"):
                lines.append(f"  - 不改会怎样：{item['consequence']}")
    disputes = report.get("disputes") or []
    if disputes:
        lines.append("")
        lines.append(f"## 争议（共 {len(disputes)} 处，待人工裁定）")
        for dispute in disputes:
            lines.append(f"- {dispute.get('point')}")
    return "\n".join(lines) + "\n"
