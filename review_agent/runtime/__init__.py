# -*- coding: utf-8 -*-
"""运行时：状态、节点协议、阶段推进、编排。"""
from __future__ import annotations

from .node import (
    Node,
    NodeContext,
    NodeResult,
    RunState,
    Workflow,
    advance,
    merge_results,
    stage_requirements,
    surviving_findings,
)
from .orchestrator import (
    COMPLETED,
    FAILED,
    INCOMPLETE,
    CheckpointStore,
    FileCheckpointStore,
    MemoryCheckpointStore,
    NodeEvent,
    Orchestrator,
    RunOutcome,
    next_finding_seq,
    state_hash,
)

from .serialization import (
    FileRunStore,
    state_from_dict,
    state_to_dict,
)

__all__ = [
    "Node", "NodeContext", "NodeResult", "RunState", "Workflow",
    "advance", "merge_results", "stage_requirements", "surviving_findings",
    "COMPLETED", "FAILED", "INCOMPLETE", "CheckpointStore",
    "FileCheckpointStore", "MemoryCheckpointStore", "NodeEvent",
    "Orchestrator", "RunOutcome", "next_finding_seq", "state_hash",
    "FileRunStore", "state_from_dict", "state_to_dict",
]
