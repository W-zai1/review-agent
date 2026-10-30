# -*- coding: utf-8 -*-
"""Application 层：一次审查的组装、执行、保存、恢复、任务与报告查询。"""
from __future__ import annotations

from .audit import (
    build_stream as build_audit_stream,
    load_stream as load_audit_stream,
    to_legacy as audit_to_legacy,
    write_stream as write_audit_stream,
)
from .reports import (
    DEFAULT_ORDER,
    SOURCE_LEGACY,
    SOURCE_V1,
    ReportRef,
    ReportService,
)
from .service import PipelinePorts, ReviewApplication
from .shadow_report import (
    ENGINE,
    LOGS_HASH_LIMITATION,
    SCHEMA_VERSION,
    UNSUPPORTED_BLOCKS,
    build_shadow_report,
    render_shadow_md,
)
from .tasks import (
    TASK_SCHEMA,
    FileTaskStore,
    Runner,
    TaskOutcome,
    TaskRecord,
    TaskService,
    TaskStore,
)

__all__ = [
    # 组装 / 执行
    "PipelinePorts", "ReviewApplication",
    # 影子报告
    "ENGINE", "LOGS_HASH_LIMITATION", "SCHEMA_VERSION", "UNSUPPORTED_BLOCKS",
    "build_shadow_report", "render_shadow_md",
    # 任务
    "TASK_SCHEMA", "FileTaskStore", "Runner", "TaskOutcome", "TaskRecord",
    "TaskService", "TaskStore",
    # 报告
    "DEFAULT_ORDER", "SOURCE_LEGACY", "SOURCE_V1", "ReportRef", "ReportService",
    # 审计事件流
    "build_audit_stream", "load_audit_stream", "audit_to_legacy",
    "write_audit_stream",
]
