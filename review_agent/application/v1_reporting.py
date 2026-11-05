# -*- coding: utf-8 -*-
"""V1 报告组装与审计日志落盘。

从 `review.py` 抽出的第 3 刀（第二阶段 P1）：报告“怎么组装、怎么写日志、
怎么挂整改建议”是一段完整的应用层逻辑，不应该继续留在组合根里。

这里不 import `review.py`，也不 import `tools/`：
- 报告构建、渲染、法条块、目的比对、整改建议都由调用方注入；
- 路径、台账、哈希、时钟也由调用方注入。
这样 review.py 仍是唯一的组合根，而本模块可以独立测试。
"""
from __future__ import annotations

import json
from pathlib import Path

from .audit import (
    build_stream as build_audit_stream,
    to_legacy as audit_to_legacy,
    write_stream as write_audit_stream,
)


class V1Reporter:
    """写 V1 报告与 `logs/v1/<review_id>/`，并补完整性链。"""

    def __init__(self, *, store, outputs, logs, root, sha256_dir, now_iso,
                 build_report, render_markdown, law_library_block,
                 purpose_analyze, attach_remediation, load_facts):
        self.store = store
        self.outputs = outputs
        self.logs = logs
        self.root = root
        self.sha256_dir = sha256_dir
        self.now_iso = now_iso
        self.build_report = build_report
        self.render_markdown = render_markdown
        self.law_library_block = law_library_block
        self.purpose_analyze = purpose_analyze
        self.attach_remediation = attach_remediation
        self.load_facts = load_facts

    def write_logs(self, session, outcome, review_id, *, purpose_info=None,
                   active_rules=(), coverage_meta=None):
        """把审计事件流写成 `logs/v1/<review_id>/`，返回目录哈希。"""
        log_dir = self.logs / "v1" / review_id
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = build_audit_stream(
            run_id=outcome.state.run_id,
            node_events=outcome.events,
            tool_logs=session.tool_logs,
            decisions=session.decisions,
            checkpoints=outcome.checkpoints,
        )
        write_audit_stream(log_dir / "events.jsonl", stream)
        legacy = audit_to_legacy(stream)
        steps = self.progress_steps(
            outcome, purpose_info=purpose_info,
            research_enabled=bool(
                (outcome.state.artifacts.get("research") or {}).get("enabled")),
            active_rules=active_rules,
            coverage_meta=coverage_meta,
        )
        for name, items in (("decisions.jsonl", legacy["decisions"]),
                            ("tools.jsonl", legacy["tools"])):
            log_dir.joinpath(name).write_text(
                "".join(json.dumps(item, ensure_ascii=False) + "\n"
                        for item in items),
                encoding="utf-8")
        log_dir.joinpath("task.json").write_text(json.dumps({
            "review_id": review_id,
            "engine": "architecture-v1",
            "final_status": outcome.status,
            "errors": list(outcome.errors),
            # 文件里保留 V1 节点级明细（audit_stream 的既有契约）；
            # API 层再把 steps_legacy 投影成 S1–S6 给控制台。
            "steps": legacy["steps"],
            "steps_legacy": steps,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tool_logs = list(getattr(session, "tool_logs", ()) or ())
        tool_ok = sum(1 for row in tool_logs if row.get("status") == "success")
        cost = {
            "total_latency_ms": 0,
            "llm_tokens": {
                "input": int(getattr(session, "llm_usage", {}).get("input", 0)),
                "output": int(getattr(session, "llm_usage", {}).get("output", 0)),
            },
            "llm_model": None,
            "llm_calls": int(getattr(session, "llm_usage", {}).get("calls", 0)),
            "llm_failures": int(getattr(session, "llm_usage", {}).get("failures", 0)),
            "tool_calls": len(tool_logs),
            "tool_success": tool_ok,
            "tool_failures": len(tool_logs) - tool_ok,
            "success_rate": round(tool_ok / len(tool_logs), 3) if tool_logs else 1.0,
        }
        log_dir.joinpath("cost.json").write_text(
            json.dumps(cost, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.sha256_dir(str(log_dir))

    def progress_steps(self, outcome, *, purpose_info=None, research_enabled=False,
                       active_rules=(), coverage_meta=None):
        """把 V1 节点执行投影成旧控制台认识的 S1–S6 进度行。"""
        state = outcome.state
        meta = state.artifacts.get("target_meta") or {}
        semantic = state.artifacts.get("semantic_stats") or {}
        mode = "rule+llm" if semantic else "rule-only"
        anchored = sum(
            1 for ev in state.evidence if ev.verification.value == "anchored")
        details = {
            "S1": {"类型判定": meta.get("type") or "-",
                   "字符数": len(state.artifacts.get("target_text") or ""),
                   "编码": meta.get("encoding") or "-"},
            "S2": {"生效规则": (coverage_meta or {}).get(
                "active_rules", len(active_rules))},
            "S3": {"审查模式": mode,
                   "各维结果": "未调用模型（纯规则审）" if mode == "rule-only"
                               else "见 semantic_stats 各维统计"},
            "S4": {"quote 可回溯": f"{anchored}/{len(state.evidence)}",
                   "证据数": len(state.evidence)},
            "S5": {"争议": len(state.disputes)},
            "S6": {"裁定": state.decision.verdict.value if state.decision else "-"},
        }
        if purpose_info:
            details["S5.5"] = {"目的比对": "已执行"}
        if research_enabled:
            details["S5.6"] = {"库外核验": "已执行"}
        steps = [
            ("S1", "接收解析"), ("S2", "规则匹配"), ("S3", "分维审查"),
            ("S4", "证据锚定"), ("S5", "交叉复核"),
        ]
        if purpose_info:
            steps.append(("S5.5", "目的比对"))
        if research_enabled:
            steps.append(("S5.6", "库外核验"))
        steps.append(("S6", "裁定汇总"))
        status = "success" if outcome.ok else "failed"
        ts = self.now_iso()
        return [
            {"step": step, "name": name, "status": status, "ts": ts,
             "latency_ms": 0,
             "detail": {**details.get(step, {}), "engine": "v1"}}
            for step, name in steps
        ]

    def write_report(self, state, outcome, *, integrity_inputs=None, session=None,
                     purpose_info=None, purpose_notes=None,
                     scenario=None, format_spec=None,
                     agent=None, active_rules=(), coverage_meta=None,
                     official=False, target_type=None,
                     target_type_source=None, rule_execution=None):
        """把 V1 报告写到正式 `outputs/` 或影子 `outputs/v1/`，并补完整性链。"""
        out_dir = self.outputs if official else self.outputs / "v1"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"{state.review_id}.json"
        if self.store.already_written(json_path, state.run_id):
            return json_path

        logs_hash = (self.write_logs(
            session, outcome, state.review_id,
            purpose_info=purpose_info,
            active_rules=active_rules,
            coverage_meta=coverage_meta)
            if session is not None else None)
        # 法条库：与旧报告共用 `law_library_block()`，保证「同一个 session → 同一个块」。
        # session 为 None（测试里直接造 state）时给 None，报告里照实自报“未接入”。
        law_lib = (self.law_library_block(session)[0]
                   if session is not None else None)
        # 原文快照必须先写：报告里的 snapshot_ref 指向它，写不进去就照实给 None
        snapshot = self.store.write_target_snapshot(
            state.artifacts.get("target_text"))
        report = self.build_report(
            state,
            target_meta=state.artifacts.get("target_meta"),
            events=outcome.events,
            checkpoints=outcome.checkpoints,
            integrity_inputs=integrity_inputs,
            logs_hash=logs_hash,
            law_library=law_lib,
            target_snapshot=snapshot,
            scenario=scenario,
            format_spec=format_spec,
            agent=agent,
            active_rules=active_rules,
            coverage_meta=coverage_meta,
            target_type=target_type,
            target_type_source=target_type_source,
            rule_execution=rule_execution,
        )
        self._attach_purpose(report, purpose_info, state.artifacts.get("target_text"))
        if purpose_notes:
            report["limitations"] = [
                *(str(x) for x in purpose_notes if str(x).strip()),
                *(report.get("limitations") or []),
            ]
        if session is not None:
            by_id = {
                str(item.get("issue_id")): item
                for item in getattr(session, "issues", ()) or ()
                if item.get("issue_id")
            }
            for issue in report.get("issues") or ():
                source = by_id.get(str(issue.get("issue_id")))
                if not source:
                    continue
                for key in ("citation", "citations", "open_discovery",
                            "rule_text", "fix", "consequence"):
                    if key in source and key not in issue:
                        issue[key] = source[key]
        open_items = [
            {"rule_id": i.get("rule_id"), "severity": i.get("severity"),
             "quote": (i.get("quote") or "")[:120],
             "confidence": i.get("confidence"), "source": "model"}
            for i in report.get("issues") or () if i.get("open_discovery")
        ]
        report["open_discoveries"] = open_items
        if open_items:
            report["limitations"].append(
                f"模型另报 {len(open_items)} 条「规则清单外发现」（rule_id 以 OPEN- 开头）："
                "来源=模型推测、未经规则库定义、不参与规则覆盖率统计，须人工确认后再采信。")
        try:
            facts = self.load_facts()
            watch = [str(x.get("entity") or "") for x in facts.get("watchlist") or []]
            seen_watch = [name for name in watch
                          if name and name in (state.artifacts.get("target_text") or "")]
            if seen_watch:
                report["limitations"].append(
                    f"文中出现的库外高频实体：{'、'.join(seen_watch[:6])}——"
                    "其数字/日期**未做真伪判定**（不是已核验通过）。")
        except Exception:                              # noqa: BLE001 - 报告增强不应阻断
            pass
        raw_target = report.get("target_path")
        if raw_target:
            try:
                target_rel = Path(str(raw_target)).resolve().relative_to(
                    self.root).as_posix()
                report["target_path"] = target_rel
                if isinstance(report.get("target"), dict):
                    report["target"]["path"] = target_rel
            except (ValueError, OSError):
                pass
        self._attach_action_plan(report)      # 必须早于 report_hash：哈希要覆盖整改建议
        return self.store.write_report(
            report, official=official, render_markdown=self.render_markdown)

    def _attach_purpose(self, report, purpose_info, text):
        """把「审查目的 → 目标 → 原文证据」接进影子报告。"""
        if not purpose_info:
            return report
        block = self.purpose_analyze(text or "", purpose_info, report["issues"])
        if block:
            report["purpose"] = block
            not_found = sum(1 for g in block.get("goals") or []
                            if g.get("status") == "not_found")
            if not_found:
                report["limitations"].append(
                    f"目的比对：{not_found} 个目标未在原文找到证据（未找到 ≠ 不存在）")
        return report

    def _attach_action_plan(self, report):
        """给影子报告的每条发现挂整改建议（改哪句 / 怎么改 / 不改会怎样）。"""
        try:
            assertions = self.load_facts().get("assertions", [])
        except Exception:                                  # noqa: BLE001 - 事实库坏了不该拖垮报告
            assertions = []
        report["action_plan"] = self.attach_remediation(
            report["issues"], assertions)
        for issue in report["issues"]:
            fix = issue.get("fix") or {}
            if fix.get("consequence"):
                issue["consequence"] = fix["consequence"]
        return report
