# -*- coding: utf-8 -*-
"""专业节点实现（v1：固定节点全部就位）。

Document Analyzer / Planner / Rule Analyzer / Semantic Reviewer /
Fact Checker / Evidence Analyzer / Evidence Assessor / Cross Reviewer / Decision Engine。
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
    backfill_evidence_ids,
    evidence_to_report,
    next_evidence_seq,
    verify_quote,
)
from .evidence_assess import (
    NAME as EVIDENCE_ASSESS_NODE_NAME,
)
from .evidence_assess import EvidenceAssessorNode, assess_evidence
from .planner import NAME as PLANNER_NODE_NAME
from .planner import PlannerNode, build_plan
from .research import (
    NAME as RESEARCH_NODE_NAME,
)
from .research import (
    ClaimVerdict,
    FactCheckNode,
    ResearchClaim,
    ResearchOutcome,
    evidence_from_verdict,
    finding_from_verdict,
    research_limitations,
)
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
    "anchor_findings", "backfill_evidence_ids", "evidence_to_report",
    "next_evidence_seq", "verify_quote",
    "EVIDENCE_ASSESS_NODE_NAME", "EvidenceAssessorNode", "assess_evidence",
    "RULES_NODE_NAME", "RuleAnalysisResult", "RuleAnalyzerNode", "RuleExecutor",
    "RuleStep", "RuleStepOutcome", "plan_rule_steps", "run_rule_plan",
    "PLANNER_NODE_NAME", "PlannerNode", "build_plan",
    "RESEARCH_NODE_NAME", "ClaimVerdict", "FactCheckNode", "ResearchClaim",
    "ResearchOutcome", "evidence_from_verdict", "finding_from_verdict",
    "research_limitations",
    "SEMANTIC_NODE_NAME", "SemanticExecutor", "SemanticReviewerNode",
    "SemanticStep", "SemanticStepOutcome", "build_meta", "coverage_from_meta",
    "covered_chars", "plan_semantic_steps",
    "DOCUMENT_NODE_NAME", "CONTRACT_MARKERS", "DocumentAnalyzerNode",
    "classify_target",
    "normalize_quote", "quote_overlap", "same_clause",
]
