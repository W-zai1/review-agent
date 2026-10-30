# -*- coding: utf-8 -*-
"""专业节点实现（v1：六个节点全部就位）。

Document Analyzer / Rule Analyzer / Semantic Reviewer / Cross Reviewer /
Evidence Analyzer / Decision Engine。
"""
from __future__ import annotations

from .cross_review import NAME as CROSS_NODE_NAME
from .cross_review import (
    CrossReviewerNode,
    CrossReviewResult,
    dispute_to_report_dict,
    review_findings,
)
from .decision import NAME as DECISION_NODE_NAME
from .decision import DecisionEngineNode, build_decision
from .document import NAME as DOCUMENT_NODE_NAME
from .document import CONTRACT_MARKERS, DocumentAnalyzerNode, classify_target
from .evidence import (
    NAME as EVIDENCE_NODE_NAME,
)
from .evidence import (
    AnchorResult,
    EvidenceAnalyzerNode,
    anchor_findings,
    evidence_to_report,
    verify_quote,
)
from .planner import NAME as PLANNER_NODE_NAME
from .planner import PlannerNode, build_plan
from .rules import NAME as RULES_NODE_NAME
from .rules import (
    RuleAnalysisResult,
    RuleAnalyzerNode,
    RuleExecutor,
    RuleStep,
    RuleStepOutcome,
    plan_rule_steps,
    run_rule_plan,
)
from .semantic import (
    NAME as SEMANTIC_NODE_NAME,
)
from .semantic import (
    SemanticExecutor,
    SemanticReviewerNode,
    SemanticStep,
    SemanticStepOutcome,
    build_meta,
    coverage_from_meta,
    covered_chars,
    plan_semantic_steps,
)
from .text import normalize_quote, quote_overlap, same_clause

__all__ = [
    "CROSS_NODE_NAME", "CrossReviewerNode", "CrossReviewResult",
    "dispute_to_report_dict", "review_findings",
    "DECISION_NODE_NAME", "DecisionEngineNode", "build_decision",
    "EVIDENCE_NODE_NAME", "AnchorResult", "EvidenceAnalyzerNode",
    "anchor_findings", "evidence_to_report", "verify_quote",
    "RULES_NODE_NAME", "RuleAnalysisResult", "RuleAnalyzerNode", "RuleExecutor",
    "RuleStep", "RuleStepOutcome", "plan_rule_steps", "run_rule_plan",
    "PLANNER_NODE_NAME", "PlannerNode", "build_plan",
    "SEMANTIC_NODE_NAME", "SemanticExecutor", "SemanticReviewerNode",
    "SemanticStep", "SemanticStepOutcome", "build_meta", "coverage_from_meta",
    "covered_chars", "plan_semantic_steps",
    "DOCUMENT_NODE_NAME", "CONTRACT_MARKERS", "DocumentAnalyzerNode",
    "classify_target",
    "normalize_quote", "quote_overlap", "same_clause",
]
