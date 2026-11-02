# -*- coding: utf-8 -*-
"""影子报告：新流水线（v1）能自己说清的那部分结论。

为什么单独一份、而不是直接复用旧报告：
    旧报告的结构是「S1~S6 边走边拼」，v1 的结论在 `RunState` 里（findings / evidence /
    disputes / decision / artifacts）。两者不是同一种东西，硬拼会得到一份
    「一半由状态推导、一半由旧流程抄来」的报告。

所以 v1 产出的是一份**自描述的影子报告**：

    * 只包含它真的算过的东西（裁定、发现、证据、争议、覆盖率、节点审计）
    * `limitations` 逐条说清「只到这一步」（未接入能力 / 未开联网 / 覆盖不足 / 日志缺失）
    * `unsupported_blocks` 显式列出「旧报告有、这份没有」的块
    * 落在 `outputs/v1/` 而不是 `outputs/`，不冒充正式报告

**2026-11-01：`unsupported_blocks` 已清零**（`law_library` A1、`research` A2）。
这张清单从「切流验收单」变成了「回归看门狗」：以后谁把某块能力从 v1 上摘下来，
它就会重新长出内容——那说明切流判据被破坏了，不是"报告少了一段"。
"""
from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from typing import Any

from ..adapters.issue_codec import EncodedIssue, decode_issue
from ..domain import Severity
from ..runtime import RunState, surviving_findings
from ..runtime.nodes.research import research_limitations

SCHEMA_VERSION = "1.0-v1-shadow"
ENGINE = "architecture-v1"

# 旧报告有、v1 还没有的块。切流前这份清单必须清零（或逐项给出替代方案）。
#
# 2026-11-01：两项都清了。
#   · `law_library` —— 两条链路共用 `review.law_library_block()`（A1）
#   · `research`    —— `FactCheckNode` + `research.web` 工具，报告段由
#                      `tools/research.build_block()` 产出（与旧链路同一函数，A2）
# 剩下的差异不再是「块缺失」，而是块内更细的边界（例如 `integrity.logs_hash`），
# 那些写在 `limitations` 里——它们不是「没做」，是「做了但只到这一步」。
UNSUPPORTED_BLOCKS: tuple[tuple[str, str], ...] = ()

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
                 logs_hash: str | None,
                 law_library: Mapping[str, Any] | None = None,
                 research: Mapping[str, Any] | None = None,
                 snapshot: Mapping[str, Any] | None = None) -> list[str]:
    """按「读者最需要先知道什么」排序，口径与旧报告的 limitations 对齐。

    旧报告的限制条目散在 run_one 里按场景拼；这里按**类别**给，规则固定：
        未支持块 → 模式边界 → 文本覆盖 → 证据丢弃 → 日志缺失 → 节点告警
    """
    items = [f"未接入：{key}（{why}）" for key, why in UNSUPPORTED_BLOCKS]
    if snapshot is None:
        # 没有原文快照 = 报告离开本机就无法复核 quote。这一条必须写在纸上：
        # 「允许别人独立验证这个结论」是本项目的最高原则。
        items.append(
            "未附原文快照（state/text/<sha256>.txt）：本报告只在同一仓库内可复核"
            "引用；把 JSON 单独带出去后，无法证明 quote 确实来自原文。")
    if law_library is not None:
        # 法条库一旦接进来，「引的是哪几部法、哪一版」就必须自报——
        # 否则一条「依据：第十九条」会被读成"全部法律都查过了"。
        lib = law_library if isinstance(law_library, Mapping) else {}
        if lib.get("available"):
            laws = lib.get("laws") or []
            desc = "、".join(
                f"《{law.get('name')}》{law.get('status')}·"
                f"{law.get('effective_date')}施行·{law.get('articles')} 条"
                for law in laws)
            items.append(
                f"法律依据仅限已收录的法条库：{desc}，共 {lib.get('articles_total', 0)} 条。"
                "库外法律/条款不作引用，也不以模型记忆代替条文；"
                "命中条文 ≠ 条文适用（例外情形仍需人工判断）。")
            for note in lib.get("skipped") or []:
                if note not in items:
                    items.append(str(note))
        else:
            items.append(f"法条库不可用（{lib.get('error') or '未知原因'}）："
                         "本次未做法条级依据比对，涉及法律条款的结论仅为线索。")
    if _review_mode(state) == "rule-only":
        items.append("本次为纯规则审：未调用模型，语义类问题不在覆盖范围内")
    # 库外核验：没做要说「没做」，做了要说「查到什么/查不到什么」。
    # 这一段以前是 `unsupported_blocks` 的一项，现在是同口径的 limitations。
    items.extend(research_limitations(research))
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
    law_library: Mapping[str, Any] | None = None,
    target_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把 RunState 收敛成一份可读的影子报告（纯函数，不读文件、不看时间）。

    `logs_hash`：`logs/v1/<review_id>/` 的目录哈希；传 None 表示这次没写日志，
    limitations 里会明说（旧报告有这一层，不能默默没有）。
    `law_library`：由调用方注入（它要读法条库文件，本函数不碰磁盘）。
    传 None 表示这次没接——报告里该键为 `null`，limitations 照实说「未做法条级比对」。
    `target_snapshot`：`{"ref": "state/text/<sha256>.txt", "hash": "<sha256>"}`，
    由调用方写盘后注入（本函数不碰磁盘）。传 None = 没写快照，limitations 会自报
    「本报告只在同一仓库内可复核引用」。
    """
    findings = surviving_findings(state)
    decision = state.decision
    meta = dict(target_meta or state.artifacts.get("target_meta") or {})
    text = state.artifacts.get("target_text") or ""
    snapshot = dict(target_snapshot or {}) or None
    coverage = _coverage(state)
    inputs = dict(integrity_inputs or {})
    # 库外核验块由 `FactCheckNode` 写进 artifacts（与旧报告同名同形）。
    # 这里不重新拼：块是**结论**，重拼就会多出一套口径。
    # `fixes` 是节点与报告层之间的**传输字段**（finding_id → 改写建议），
    # 并进 `issues[].fix` 后立刻摘掉：对外的 `research` 段必须与旧报告同形。
    research = state.artifacts.get("research")
    fixes: Mapping[str, Any] = {}
    if isinstance(research, Mapping):
        research = dict(research)
        raw_fixes = research.pop("fixes", None)
        if isinstance(raw_fixes, Mapping):
            fixes = dict(raw_fixes)
    else:
        research = None
    issues: list[dict[str, Any]] = []
    for finding in findings:
        issue = decode_issue(EncodedIssue(finding=finding))
        if finding.id in fixes:
            # 外部检索已经给出了「改成什么」，用它覆盖通用建议——
            # 否则整改单会把「可直接改」误判成「需人工改写」。
            issue["fix"] = dict(fixes[finding.id])
        issues.append(issue)
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
            "chars": meta.get("chars") or len(text),
            "hash": state.artifacts.get("target_hash"),
            "encoding": meta.get("encoding"),
            # 原文快照（批次 3 / B1）：报价离开本机也能自证「quote 真的来自原文」。
            # `hash` 是**文件字节**的哈希，`snapshot_hash` 是**解码后文本**的哈希——
            # 两者不同是刻意的：前者证明"审的是这个文件"，后者证明"报告里的原文是这段"。
            "snapshot_ref": (snapshot or {}).get("ref"),
            "snapshot_hash": (snapshot or {}).get("hash"),
        },
        # 与旧报告同名的顶层键：前端的高亮/定位就是靠它（web/app.js 读 target_text）。
        # 不带这一段，v1 报告在控制台里只能看条目、点不开原文。
        "target_text": text,
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
        "issues": issues,
        "evidence": [
            {"evidence_id": ev.id,
             "issue_id": ev.finding_ids[0] if ev.finding_ids else None,
             "kind": ev.kind.value,
             "quote": ev.quote,
             "location": ev.location.label,
             "verification": ev.verification.value,
             "verified_by": ev.verified_by,
             # 外部来源证据必须能回答「引的是哪一页」：没有它，
             # 「外部核验过」只是一句无法复核的自我声明
             "source_ref": ev.source_ref,
             "note": ev.note,
             # 第二根轴（批次 3 / B1）：这条证据够不够支撑结论。
             # 旧报告（legacy）的 evidence[] 不加这个键——它要逐字节可比；
             # v1 是前向格式，缺省 UNASSESSED 也照写，读者不会误读成"已核验"。
             "strength": ev.strength.value,
             "contradicted_by": list(ev.contradicted_by),
             "source": ({"url": ev.source.url, "domain": ev.source.domain,
                         "official": ev.source.official,
                         "snapshot_hash": ev.source.snapshot_hash}
                        if ev.source is not None else None)}
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
        # 与旧报告同名同形的法条库段；没注入就是 None（由 unsupported_blocks 自报）
        "law_library": dict(law_library) if law_library is not None else None,
        # 库外核验段（未启用时是 `{"enabled": false, "reason": ...}`，
        # 与旧报告的 --no-research 形态一致）
        "research": dict(research) if research is not None else None,
        "limitations": _limitations(state, coverage=coverage, logs_hash=logs_hash,
                                    law_library=law_library, research=research,
                                    snapshot=snapshot),
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
    if target.get("snapshot_ref"):
        lines.append(f"- 原文快照：`{target['snapshot_ref']}`"
                     f"（sha256 `{str(target.get('snapshot_hash') or '')[:16]}…`，"
                     "可用于离线复核每条 quote）")
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
    limitations = report.get("limitations") or []
    if limitations:
        # 限制以前只在 JSON 里，Markdown 版本看不到——而读者恰恰是看 Markdown 的人。
        # 一份"只到这一步"的报告如果不把这句话印在纸上，就会被当成"全都查过了"。
        lines.append("")
        lines.append("## 限制（这份报告只到这一步）")
        for item in limitations:
            lines.append(f"- {item}")
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
    research = report.get("research") or {}
    if research.get("enabled"):
        status_cn = {"supported": "一致", "refuted": "不一致",
                     "unverified": "未找到可比来源"}
        lines.append("")
        lines.append("## 库外事实核验（外部来源）")
        lines.append(f"- 检索 {research.get('checked', 0)} 条主张："
                     f"支持 {len(research.get('supported') or [])} / "
                     f"反驳 {len(research.get('refuted') or [])} / "
                     f"未找到 {len(research.get('unverified') or [])}")
        for item in research.get("items") or []:
            first = (item.get("sources") or [{}])[0]
            lines.append(
                f"- {status_cn.get(item.get('status'), str(item.get('status')))}："
                f"`{str(item.get('quote') or '')[:60]}` → "
                f"{item.get('display') or item.get('value') or '—'}"
                f"（{first.get('title') or '无标题'} {first.get('url') or ''}）")
        if any(i.get("needs_confirm") for i in research.get("items") or []):
            lines.append("- 注意：单一来源的反驳只进「待人工确认」，未写进整改单"
                         "（统计口径不同就可能出现这种差异）")
    evidence = report.get("evidence") or []
    if evidence:
        lines.append("")
        lines.append(f"## 证据校验（共 {len(evidence)} 条）")
        for row in evidence:
            extra = ""
            if row.get("kind") and row.get("kind") != "quote":
                # 外部来源证据：不显示类型与来源，读者会把它当成「原文里的话」
                extra = f"［{row.get('kind')}］"
                if row.get("source_ref"):
                    extra += f" {row['source_ref']}"
            strength = row.get("strength")
            if strength and strength != "unassessed":
                # 第二根轴：锚定通过 ≠ 证据成立。强度非缺省时必须印出来，
                # 否则「anchored」会被读成「证据充分」。
                extra += f"｜强度 {strength}"
            if row.get("contradicted_by"):
                extra += f"｜反证 {', '.join(row['contradicted_by'])}"
            lines.append(f"- `{row.get('evidence_id')}` → {row.get('issue_id')}："
                         f"{row.get('verification')}"
                         f"（{row.get('verified_by') or '无校验方'}）{extra}")
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
