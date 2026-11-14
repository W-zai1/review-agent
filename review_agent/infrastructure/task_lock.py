"""任务级跨进程续跑租约锁。

续跑会读取检查点、更新任务状态并写最终报告；两个 CLI 进程同时恢复同一个
review_id 会产生两份执行与竞争写。锁文件用 `O_EXCL` 原子创建，持锁进程定期
刷新 mtime；正常退出删除，崩溃后超过租约期限可被下一进程接管。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path


class TaskLockUnavailable(RuntimeError):
    """同一任务已有另一个存活进程在续跑。"""


class TaskResumeLock:
    """基于原子创建 + 心跳租约的 task/resume 临界区。"""

    def __init__(self, root: Path | str, review_id: str, *,
                 lease_seconds: float = 30.0,
                 heartbeat_seconds: float = 5.0) -> None:
        self.path = Path(root) / f".{review_id}.resume.lock"
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = min(
            float(heartbeat_seconds), self.lease_seconds / 2)
        self._token = uuid.uuid4().hex
        self._fd = None
        self._stop = threading.Event()
        self._heartbeat = None

    def __enter__(self) -> "TaskResumeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(3):
            try:
                fd = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not self._is_stale():
                    raise TaskLockUnavailable("该任务已有续跑进程")
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise TaskLockUnavailable(
                        "该任务已有续跑进程") from exc
                continue
            payload = json.dumps({
                "token": self._token,
                "pid": os.getpid(),
                "created_at": time.time(),
            }, sort_keys=True).encode("utf-8")
            try:
                os.write(fd, payload)
                os.fsync(fd)
            except Exception:
                os.close(fd)
                try:
                    self.path.unlink()
                except OSError:
                    pass
                raise
            self._fd = fd
            self._heartbeat = threading.Thread(
                target=self._beat, name="task-resume-lease", daemon=True)
            self._heartbeat.start()
            return self
        raise TaskLockUnavailable("该任务已有续跑进程")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=max(1.0, self.heartbeat_seconds * 2))
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("token") == self._token:
                self.path.unlink()
        except (OSError, ValueError, TypeError):
            pass

    def _is_stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > self.lease_seconds
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                os.utime(self.path, None)
            except OSError:
                return


__all__ = ["TaskLockUnavailable", "TaskResumeLock"]
