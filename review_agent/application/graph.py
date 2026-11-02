# -*- coding: utf-8 -*-
"""Evidence Graph：把 RunState 投影成报告可读的关系图（批次 3 / B5）。

不建图数据库、不做图遍历：一次纯函数投影就足够回答六个问题：

    1. 这条发现依据哪条规则？        answers[id]["rule"]
    2. 它有哪些材料/证据？           answers[id]["materials"]
    3. 哪些证据来自外部来源？        answers[id]["external"]
    4. 它是哪个节点产出的？          answers[id]["produced_by"]
    5. 有没有经过交叉复核？          answers[id]["reviewed_by"]
    6. 最终裁定是什么？              answers[id]["verdict"]

图的节点与边都来自不可变领域对象；报告层只负责渲染，不再二次推断。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..domain import EvidenceKind, FindingOrigin
from ..runtime import RunState, surviving_findings

_PRODUCER: Final[Mapping[FindingOrigin, str]] = {
    FindingOrigin.RULE: "rule-analyzer",
    FindingOrigin.MODEL: "semantic-reviewer",
    FindingOrigin.HEURISTIC: "rule-analyzer",
    FindingOrigin.RESEARCH: "fact-checker",
    FindingOrigin.OPEN_DISCOVERY: "semantic-reviewer",
}


def build_graph(state: RunState) -> dict[str, Any]:
    """把一次 RunState 投影为 `nodes / edges / answers`。"""
    findings = surviving_findings(state)
    evidence = list(state.evidence)
    disputes = list(state.disputes)
    decision = state.decision

    by_finding: dict[str, list[Any]] = {}
    for item in evidence:
        for fid in item.finding_ids:
            by_finding.setdefault(fid, []).append(item)

    decision_by_finding: dict[str, str] = {}
    if decision is not None:
        for fid in decision.finding_ids:
            decision_by_finding[fid] = decision.verdict.value

    answers: dict[str, dict[str, Any]] = {}
    for finding in findings:
        rows = by_finding.get(finding.id, [])
        answers[finding.id] = {
            "rule": finding.rule.rule_id if finding.rule else None,
            "materials": [row.id for row in rows],
            "external": [row.id for row in rows
                         if row.kind is EvidenceKind.EXTERNAL_SOURCE],
            "produced_by": _PRODUCER.get(finding.origin, finding.origin.value),
            # v1 的每个 finding 都经过 cross_review 阶段；争议本身是复核产物，
            # 不是另一个生产节点。
            "reviewed_by": "cross-reviewer",
            "verdict": decision_by_finding.get(finding.id),
        }

    rule_ids = sorted({f.rule.rule_id for f in findings if f.rule})
    edges: list[dict[str, str]] = []
    for finding in findings:
        if finding.rule:
            edges.append({"from": finding.id, "to": f"RULE-{finding.rule.rule_id}",
                          "rel": "derived_from"})
        for row in by_finding.get(finding.id, []):
            edges.append({"from": finding.id, "to": row.id, "rel": "supported_by"})
    if decision is not None:
        for fid in decision.finding_ids:
            edges.append({"from": decision.id, "to": fid, "rel": "decided_on"})
    for dispute in disputes:
        for fid in dispute.issue_ids:
            edges.append({"from": dispute.id, "to": fid, "rel": "disputed_on"})

    return {
        "nodes": {
            "findings": [
                {"id": f.id, "rule_id": f.rule.rule_id if f.rule else None,
                 "dimension": f.dimension, "severity": f.severity.value,
                 "origin": f.origin.value, "quote": f.quote[:160]}
                for f in findings
            ],
            "evidence": [
                {"id": e.id, "kind": e.kind.value,
                 "verification": e.verification.value,
                 "strength": e.strength.value,
                 "finding_ids": list(e.finding_ids),
                 "source_ref": e.source_ref}
                for e in evidence
            ],
            "rules": [{"id": f"RULE-{rid}", "rule_id": rid} for rid in rule_ids],
            "disputes": [
                {"id": d.id, "kind": d.kind.value, "status": d.status.value,
                 "issue_ids": list(d.issue_ids)}
                for d in disputes
            ],
            "decisions": (
                [{"id": decision.id, "verdict": decision.verdict.value,
                  "finding_ids": list(decision.finding_ids)}]
                if decision is not None else []
            ),
        },
        "edges": edges,
        "answers": answers,
    }
