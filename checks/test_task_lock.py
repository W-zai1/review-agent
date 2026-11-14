"""跨进程任务续跑锁：同一 review_id 同时只能有一个所有者。"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from review_agent.infrastructure.task_lock import (  # noqa: E402
    TaskLockUnavailable,
    TaskResumeLock,
)


class TaskResumeLockTests(unittest.TestCase):
    def test_second_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TaskResumeLock(Path(tmp), "R-20261114-949"):
                with self.assertRaises(TaskLockUnavailable):
                    with TaskResumeLock(Path(tmp), "R-20261114-949"):
                        pass

    def test_second_process_is_rejected_until_first_exits(self):
        review_id = "R-20261114-950"
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "ready"
            stop = Path(tmp) / "stop"
            script = (
                "import time\n"
                "from review_agent.infrastructure.task_lock import TaskResumeLock\n"
                "from pathlib import Path\n"
                f"lock = TaskResumeLock(r'{Path(tmp)}', '{review_id}')\n"
                "with lock:\n"
                f"    Path(r'{ready}').write_text(str(lock.path), encoding='utf-8')\n"
                f"    deadline = time.monotonic() + 10\n"
                f"    while not Path(r'{stop}').exists() and time.monotonic() < deadline:\n"
                "        time.sleep(0.02)\n"
            )
            owner = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=ROOT, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )

            def cleanup_owner():
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=5)
                for stream in (owner.stdout, owner.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()

            self.addCleanup(cleanup_owner)
            deadline = time.monotonic() + 5
            while not ready.exists() and owner.poll() is None:
                if time.monotonic() >= deadline:
                    self.fail("子进程未能取得续跑锁")
                time.sleep(0.02)
            if not ready.exists():
                self.fail(owner.stderr.read())
            parent_lock = TaskResumeLock(Path(tmp), review_id)
            self.assertEqual(ready.read_text(encoding="utf-8"),
                             str(parent_lock.path))

            with self.assertRaises(TaskLockUnavailable):
                with parent_lock:
                    pass

            stop.write_text("stop", encoding="utf-8")
            _, stderr = owner.communicate(timeout=5)
            self.assertEqual(owner.returncode, 0, stderr)
            with TaskResumeLock(Path(tmp), review_id):
                pass

    def test_stale_owner_can_be_replaced(self):
        review_id = "R-20261114-951"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f".{review_id}.resume.lock"
            path.write_text('{"token":"dead"}', encoding="utf-8")
            old = time.time() - 60
            import os

            os.utime(path, (old, old))
            with TaskResumeLock(
                    Path(tmp), review_id, lease_seconds=1,
                    heartbeat_seconds=0.1):
                self.assertTrue(path.exists())
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
