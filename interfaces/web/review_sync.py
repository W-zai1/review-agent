"""同步 `POST /api/review` 的 HTTP 适配层。

Handler 里剩下的只是「请求归一 → 占并发闸门 → 交给 TaskService 跑一次
子进程 → 把结果折成响应」。任务记录、超时、状态机仍在 TaskService；
子进程隔离仍由注入的 Runner 负责，这里不直接起进程。

所有依赖（解析器、Runner 工厂、任务服务、错误解析、时钟）都从构造参数
注入：既避免本模块反向 import server，也让各条失败分支可以直接单测。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from review_agent.application import active_tasks
from review_agent.domain import NotFoundError, RunStatus
from interfaces.web import review_requests


class SyncReview:
    def __init__(self, *, tasks, task_stale_after, target_types,
                 resolve_target, resolve_agent, resolve_scenario,
                 resolve_format_spec, resolve_format_custom,
                 clean_purpose, assemble_purpose, idempotency_key,
                 build_runner, task_service, read_json, parse_review_error,
                 jobs_dir, timeout, clock=time.time):
        self._tasks = tasks
        self._task_stale_after = task_stale_after
        self._target_types = target_types
        self._resolve_target = resolve_target
        self._resolve_agent = resolve_agent
        self._resolve_scenario = resolve_scenario
        self._resolve_format_spec = resolve_format_spec
        self._resolve_format_custom = resolve_format_custom
        self._clean_purpose = clean_purpose
        self._assemble_purpose = assemble_purpose
        self._idempotency_key = idempotency_key
        self._build_runner = build_runner
        self._task_service = task_service
        self._read_json = read_json
        self._parse_review_error = parse_review_error
        self._jobs_dir = Path(jobs_dir)
        self._timeout = timeout
        self._clock = clock

    def run(self, data):
        file, target, error = review_requests.resolve_target(
            data, self._resolve_target)
        if error:
            return {"ok": False, "error": error}

        # 同步接口也要占并发闸门：旧实现只查内存 JOBS 里的 running，而同步调用
        # 根本不写 JOBS，于是「同步 + 异步同时跑」是允许的——两条进程同时写
        # ledger/outputs。
        busy = active_tasks(self._tasks, stale_after=self._task_stale_after)
        if busy:
            return {"ok": False, "error": "已有审查任务进行中，请稍候",
                    "review_id": busy[0].review_id}

        request, error = review_requests.assemble_request(
            data, file=file, target=target, target_types=self._target_types,
            resolve_agent=self._resolve_agent,
            resolve_scenario=self._resolve_scenario,
            resolve_format_spec=self._resolve_format_spec,
            resolve_format_custom=self._resolve_format_custom,
            clean_purpose=self._clean_purpose,
            assemble_purpose=self._assemble_purpose,
            idempotency_key=self._idempotency_key,
            normalize_flags=False)
        if error:
            return {"ok": False, "error": error}

        job_id = "sync-" + uuid.uuid4().hex[:8]
        progress = self._jobs_dir / f"{job_id}.jsonl"
        try:
            progress.unlink()
        except OSError:
            pass
        runner = self._build_runner(request, job_id, progress)

        t0 = self._clock()
        try:
            record = self._task_service().start_review_with_manifest(
                runner, target, profile_id=request.agent,
                purpose=(request.purpose_text or None), engine="v1",
                use_llm=request.llm is True, timeout=self._timeout,
                execution_options=review_requests.build_execution_options(
                    request))
        except NotFoundError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": f"无法写入任务记录：{exc}"}
        elapsed = round(self._clock() - t0, 1)

        review_id = record.review_id
        status = RunStatus(record.status)
        if status is RunStatus.COMPLETED:
            report_path = record.report_path
            if not report_path or not Path(str(report_path)).exists():
                return {"ok": False, "error": "审查完成但未生成报告",
                        "timeout": self._timeout, "elapsed": elapsed,
                        "review_id": review_id}
            report = self._read_json(Path(str(report_path)))
            return {"ok": True, "report": report,
                    "id": report.get("review_id") or review_id,
                    "elapsed": elapsed, "timeout": self._timeout}

        error = record.error or "运行失败"
        payload = {"ok": False, "error": error, "timeout": self._timeout,
                   "elapsed": elapsed, "review_id": review_id}
        if error.startswith("任务超时"):
            # 对外沿用「审查超时」措辞；任务记录里保留 TaskService 的统一文案。
            payload["error"] = f"审查超时（{self._timeout}s）"
        info = self._parse_review_error(error) or {}
        if info:
            payload["error_kind"] = info.get("kind")
            payload["error_status"] = info.get("http_status")
        return payload
