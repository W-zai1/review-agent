"""任务状态视图与取消动作的 HTTP 适配层。

任务记录（`state/tasks/`）仍是唯一事实来源；`JOBS` 只是重启后可能过期的
内存视图。这里只做两件事：把记录投影成前端认识的 job 形状，以及把
「取消」翻译成 TaskService 的调用。server 只保留路由级的薄包装。

不 import server：JOBS、锁、TASKS、PROCS、输出目录与 TaskService 全部由
调用方注入，测试可以直接构造这个类。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from review_agent.application import TaskRecord
from review_agent.domain import InvalidTransition, NotFoundError, RunStatus


def job_view(record: TaskRecord, job: dict | None = None) -> dict:
    """把任务记录折成前端认识的 job 形状。"""
    status = RunStatus(record.status)
    if status in (RunStatus.COMPLETED,):
        shown = "done"
    elif status in (RunStatus.CANCELLED, RunStatus.ABORTED):
        shown = "cancelled"
    elif status in (RunStatus.FAILED,):
        shown = "error"
    else:
        shown = "running"
    view = dict(job or {})
    view.update({
        "id": view.get("id"),
        "review_id": record.review_id,
        "status": shown,
        "task_status": status.value,
        "error": record.error,
        "report_id": record.report_path and Path(record.report_path).stem
                     or view.get("report_id"),
        "finished": view.get("finished") or (
            time.time() if status.is_terminal else None),
    })
    return view


class TaskLifecycle:
    """server 对任务状态/取消的唯一实现。"""

    def __init__(self, *, jobs, jobs_lock, tasks, jobs_dir, outputs, procs,
                 procs_lock, task_service, read_json):
        self.jobs = jobs
        self.jobs_lock = jobs_lock
        self.tasks = tasks
        self.jobs_dir = Path(jobs_dir)
        self.outputs = Path(outputs)
        self.procs = procs
        self.procs_lock = procs_lock
        self.task_service = task_service
        self.read_json = read_json

    def status(self, job_id):
        if not job_id:
            return {"ok": False, "error": "缺少 job 参数"}
        with self.jobs_lock:
            job = dict(self.jobs.get(job_id) or {})
        if not job:
            return {"ok": False, "error": "任务不存在或已过期"}
        # 任务记录是唯一事实来源：JOBS 可能来自 .jobs/*.jsonl（重启恢复），
        # 与 state/tasks 可能不一致；状态/错误/报告一律以记录为准。
        review_id = str(job.get("review_id") or "")
        if review_id:
            try:
                record = self.tasks.get(review_id)
            except (OSError, ValueError, KeyError, TypeError):
                record = None
            if record is not None:
                job = job_view(record, job)
        steps = []
        progress = self.jobs_dir / f"{job_id}.jsonl"
        if progress.exists():
            try:
                for line in progress.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        steps.append(json.loads(line))
                    except Exception:
                        continue
            except Exception:
                pass
        report = None
        if job.get("status") == "done" and job.get("report_id"):
            rid = job["report_id"]
            path = self.outputs / f"{rid}.json"
            if not path.exists():
                path = self.outputs / "v1" / f"{rid}.json"
            if path.exists():
                try:
                    report = self.read_json(path)
                except Exception:
                    report = None
        return {"ok": True, "job": job, "steps": steps, "report": report}

    def cancel(self, data):
        """取消正在跑（或排队）的审查；状态以任务记录为准。"""
        d = data or {}
        job_id = str(d.get("job") or "").strip()
        review_id = str(d.get("review_id") or "").strip()
        if not review_id and job_id:
            with self.jobs_lock:
                review_id = str((self.jobs.get(job_id) or {}).get("review_id") or "")
        if not review_id:
            return {"ok": False, "error": "找不到任务：请给 job 或 review_id"}
        record = self.tasks.get(review_id)
        if record is None:
            return {"ok": False, "error": f"找不到任务：{review_id}"}
        status = RunStatus(record.status)
        if status is RunStatus.CANCELLED:
            cancelled = record                      # 幂等：连点两下不该报错
        elif status.is_terminal:
            return {"ok": False,
                    "error": f"任务已是终态（{status.value}），不能取消"}
        else:
            # 先真的停进程：恢复出来的旧任务没有本进程令牌，只能靠 PROCS 兜底；
            # 本进程发起的任务还会被 runner 的令牌监视器再确认一次。
            with self.procs_lock:
                proc = self.procs.get(review_id)
            if proc is not None:
                try:
                    proc.terminate()
                except OSError as exc:
                    print(f"[review] 取消时终止子进程失败：{exc}", flush=True)
            try:
                cancelled = self.task_service.cancel(review_id, reason="用户取消")
            except InvalidTransition as exc:
                return {"ok": False, "error": str(exc)}
            except NotFoundError as exc:
                return {"ok": False, "error": str(exc)}
        with self.jobs_lock:
            for jid, job in self.jobs.items():
                if job.get("review_id") != review_id:
                    continue
                job.update(job_view(cancelled, job))
                job.update({"status": "cancelled",
                            "finished": job.get("finished") or time.time()})
        print(f"[review] 任务 {review_id} 已取消（{cancelled.status}）", flush=True)
        return {"ok": True, "review_id": review_id,
                "status": cancelled.status, "job": job_id or None}
