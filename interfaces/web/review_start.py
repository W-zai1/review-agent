"""异步审查启动：幂等、并发闸门、任务记录与 JOBS 视图的唯一实现。

Handler 只负责路由与响应；「同一个请求是不是已经有任务在跑」「现在能不能
再起一单」这些判断必须看任务记录（`state/tasks/`）而不是重启即失忆的
`JOBS`。这里仍然把执行交给 `TaskService.start_async_with()`，Runner 仍是
server 的子进程适配器——没有为了抽离而改执行链。

模块不 import server：TASKS / JOBS / 锁 / 路径 / TaskService / 解析器 /
Runner 工厂 / job 视图全部由调用方注入。
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from review_agent.application import active_tasks, find_active
from review_agent.domain import NotFoundError, RunStatus

from . import review_requests


class ReviewStarter:
    """把一次已归一化的 HTTP 请求变成 TaskService 里的异步任务。"""

    def __init__(self, *, jobs, jobs_lock, tasks, jobs_dir, task_service,
                 task_stale_after, review_timeout, target_types,
                 resolve_target, resolve_agent, resolve_scenario,
                 resolve_format_spec, resolve_format_custom, clean_purpose,
                 assemble_purpose, idempotency_key, build_runner, job_view):
        self.jobs = jobs
        self.jobs_lock = jobs_lock
        self.tasks = tasks
        self.jobs_dir = Path(jobs_dir)
        self.task_service = task_service
        self.task_stale_after = task_stale_after
        self.review_timeout = review_timeout
        self.target_types = target_types
        self.resolve_target = resolve_target
        self.resolve_agent = resolve_agent
        self.resolve_scenario = resolve_scenario
        self.resolve_format_spec = resolve_format_spec
        self.resolve_format_custom = resolve_format_custom
        self.clean_purpose = clean_purpose
        self.assemble_purpose = assemble_purpose
        self.idempotency_key = idempotency_key
        self.build_runner = build_runner
        self.job_view = job_view

    def start(self, data):
        file, target, error = review_requests.resolve_target(
            data, self.resolve_target)
        if error:
            return {"ok": False, "error": error}
        request, error = review_requests.assemble_request(
            data, file=file, target=target, target_types=self.target_types,
            resolve_agent=self.resolve_agent,
            resolve_scenario=self.resolve_scenario,
            resolve_format_spec=self.resolve_format_spec,
            resolve_format_custom=self.resolve_format_custom,
            clean_purpose=self.clean_purpose,
            assemble_purpose=self.assemble_purpose,
            idempotency_key=self.idempotency_key,
            normalize_flags=True)
        if error:
            return {"ok": False, "error": error}
        key = request.idempotency_key
        with self.jobs_lock:
            now = time.time()
            # 1) 幂等命中：同一个键的未结束任务直接复用（返回同一个 job_id）。
            existing = find_active(self.tasks, key)
            if existing is not None:
                for old_id, old in list(self.jobs.items()):
                    if old.get("review_id") == existing.review_id:
                        self.jobs[old_id] = self.job_view(existing, old)
                        print(f"[review] 幂等命中：沿用任务 {existing.review_id}"
                              f"（job {old_id}）", flush=True)
                        return {"ok": True, "job_id": old_id, "deduped": True,
                                "review_id": existing.review_id,
                                "status": self.jobs[old_id]["status"]}
            # 2) 并发闸门：看任务记录，不看内存里的 running。后者在重启后是空的，
            #    而且同步接口根本不占位（等于没闸门）。
            busy = active_tasks(self.tasks, stale_after=self.task_stale_after)
            if busy:
                return {"ok": False, "error": "已有审查任务进行中，请稍候",
                        "review_id": busy[0].review_id}
            # 3) 清掉一小时前结束的内存任务（只清视图与进度文件，不动任务记录）。
            for old_id, old in list(self.jobs.items()):
                if old.get("finished") and now - old["finished"] > 3600:
                    self.jobs.pop(old_id, None)
                    try:
                        (self.jobs_dir / f"{old_id}.jsonl").unlink()
                    except OSError:
                        pass
            job_id = uuid.uuid4().hex[:12]
            progress = self.jobs_dir / f"{job_id}.jsonl"
            try:
                progress.unlink()
            except OSError:
                pass
            # 任务身份 / 状态 / 幂等 / 超时全部交给 TaskService；
            # server 只把「这一次怎么跑」封成 Runner（内部仍是 review.py 子进程）。
            runner = self.build_runner(review_requests.build_runner_spec(
                request, job_id=job_id, progress=progress))
            try:
                record = self.task_service.start_async_with_manifest(
                    runner, request.target, profile_id=request.agent,
                    purpose=(request.purpose_text or None), engine="v1",
                    use_llm=request.llm is True, timeout=self.review_timeout,
                    idempotency_key=key,
                    execution_options=review_requests.build_execution_options(
                        request))
            except NotFoundError as exc:
                return {"ok": False, "error": str(exc)}
            except OSError as exc:
                return {"ok": False, "error": f"无法写入任务记录：{exc}"}
            review_id = record.review_id
            if record.status != RunStatus.QUEUED.value:
                # 幂等竞态：前置 find_active 之后、_open 之前，另一个请求先建了同一任务。
                existing = next((jid for jid, job in self.jobs.items()
                                 if job.get("review_id") == review_id), None)
                return {"ok": True, "job_id": existing or job_id, "deduped": True,
                        "review_id": review_id, "status": record.status}
            # 只补视图；runner 可能已经先写过（setdefault 不覆盖它的进度）。
            self.jobs.setdefault(job_id, {
                "id": job_id,
                "review_id": review_id,
                "file": request.file,
                "status": "running",
                "task_status": RunStatus.QUEUED.value,
                "created": now,
                "finished": None,
                "report_id": None,
                "error": None,
                "llm": request.llm,
                "research": request.research,
                "type": request.type,
                "agent": request.agent,
                "scenario": request.scenario,
                "format_spec": request.format_spec,
                # 本次任务实际使用的超时：/api/review/status 可见，排查
                # 「为什么跑了很久才失败 / 为什么 300s 就断了」时有据可查。
                "timeout": self.review_timeout,
            })
        print(f"[review] 任务 {job_id}（{review_id}）已创建：{request.file}"
              f"（超时 {self.review_timeout}s）", flush=True)
        return {"ok": True, "job_id": job_id, "review_id": review_id}
