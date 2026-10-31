# -*- coding: utf-8 -*-
"""能力层：工具协议、注册表、执行管线。

阶段 1 只落协议；旧 tools/ 里的实现（regex_check、law_index、integrity…）在阶段 3
逐个包成 BaseTool 子类并注册，迁移一个、替换一个，不要求一次性搬完。
"""
from __future__ import annotations

from .builtin_tools import (
    DocxFormatArgs,
    DocxFormatTool,
    IntegrityHashTool,
    LawCiteArgs,
    LawCiteTool,
    LogicCheckTool,
    MathCheckTool,
    MatrixMatchArgs,
    MatrixMatchTool,
    PatternScanTool,
    PatternScanArgs,
    RuleClassifyArgs,
    RuleClassifyTool,
    TextCheckArgs,
    TextLintArgs,
    TextLintTool,
    TextRegexArgs,
    TextRegexTool,
    batch1_tools,
)
from .semantic_ingest import (  # noqa: F401 - 能力层公共出口
    Candidate,
    IngestContext,
    IngestResult,
    ingest_candidates,
)
from .tools import (
    DEFAULT_POLICY,
    BaseTool,
    DuplicateToolError,
    Permission,
    ToolArgumentError,
    ToolContext,
    ToolError,
    ToolInvocation,
    ToolPermissionError,
    ToolRegistry,
    ToolResult,
    default_permissions,
)

__all__ = [
    "DEFAULT_POLICY", "BaseTool", "DuplicateToolError", "Permission",
    "ToolArgumentError", "ToolContext", "ToolError", "ToolInvocation",
    "ToolPermissionError", "ToolRegistry", "ToolResult", "default_permissions",
    "Candidate", "IngestContext", "IngestResult", "ingest_candidates",
    "IntegrityHashTool", "LogicCheckTool", "MathCheckTool", "PatternScanTool",
    "PatternScanArgs", "RuleClassifyArgs", "RuleClassifyTool", "TextCheckArgs",
    "TextRegexArgs", "TextRegexTool", "batch1_tools",
    "MatrixMatchArgs", "MatrixMatchTool", "LawCiteArgs", "LawCiteTool",
    "DocxFormatArgs", "DocxFormatTool", "TextLintArgs", "TextLintTool",
]
