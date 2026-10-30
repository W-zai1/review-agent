#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_task_service.py —— 任务与报告服务（阶段 2 收尾）。

这两个服务此前不存在，直接后果是：

    * 「跑一次审查」没有任务身份：状态只在 server 的内存 `JOBS` 里，
      进程一重启就不知道那次到底跑完没有；
    * 「报告在哪」由每个入口各拼一遍路径：CLI 拼 `outputs/`、v1 拼
      `outputs/v1/`、server 再拼一遍，加一种引擎就多一处漏。

这里钉住的不变量：

    任务  —— review_id/run_id 格式与归属、状态受领域枚举约束、失败必须留痕、
             建任务先落 running 再执行、列表按更新时间倒序
    报告  —— v1 优先于 legacy、找不到返回 None、没注入校验函数时 verify() 返回
             None（没验证 ≠ 通过）、同一 review_id 只出现一次

除了服务层用例，末尾有一个**真实集成用例**：用真 Agent / 真规则跑一遍
`start_review → get_report → verify`。它需要 `agents/`（公开 clone 不含），
缺失时 skip 并写明原因——不伪装成跑过了。
"""
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.application import (  # noqa: E402
    FileTaskStore,
    ReportService,
    TaskOutcome,
    TaskRecord,
    TaskService,
)
from review_agent.domain import NotFoundError, RunStatus, Stage  # noqa: E402
from review_agent.domain.ids import format_run_id  # noqa: E402


def make_record(**over) -> TaskRecord:
    base = dict(
        review_id="R-20261030-001",
        run_id="R-20261030-001-RUN01",
        status=RunStatus.RUNNING.value,
        stage=Stage.INTAKE.value,
        target="targets/a.txt",
        created_at="2026-10-30T10:00:00",
        updated_at="2026-10-30T10:00:00",
    )
    base.update(over)
    return TaskRecord(**base)


class CounterAllocator:
    """按调用顺序发号。真实实现扫 outputs/ + ledger，这里只验证「每次都给新号」。"""

    def __init__(self, day="20261030"):
        self.day = day
        self.calls = []
        self._seq = 0

    def __call__(self, date: datetime.date) -> str:
        self.calls.append(date)
        self._seq += 1
        return f"R-{self.day}-{self._seq:03d}"


class StubRunner:
    """服务层用例用的 runner：只记录调用、返回预设结果。

    真实执行由末尾的集成用例覆盖；这里不该再去跑一遍流水线，
    否则「服务把 record 写对了没有」会被引擎的失败盖住。
    """

    def __init__(self, outcome=None, error=None):
        self.outcome = outcome
        self.error = error
        self.calls = []

    def __call__(self, target, *, review_id, profile_id, purpose, engine,
                 use_llm):
        self.calls.append(dict(target=target, review_id=review_id,
                               profile_id=profile_id, purpose=purpose,
                               engine=engine, use_llm=use_llm))
        if self.error is not None:
            raise self.error
        return self.outcome or TaskOutcome(
            status=RunStatus.COMPLETED, stage=Stage.FINALIZE,
            verdict="warn", findings=2, evidence=2,
            report_path=f"outputs/v1/{review_id}.json")


# ---------------------------------------------------------------------------
# 记录
# ---------------------------------------------------------------------------
class TaskRecordTests(unittest.TestCase):
    def test_round_trip(self):
        record = make_record()
        restored = TaskRecord.from_dict(json.loads(json.dumps(record.to_dict())))
        self.assertEqual(restored, record)

    def test_schema_must_match(self):
        data = make_record().to_dict()
        data["schema"] = 99
        with self.assertRaises(ValueError):
            TaskRecord.from_dict(data)

    def test_run_must_belong_to_review(self):
        with self.assertRaises(ValueError):
            make_record(run_id="R-20261030-002-RUN01")

    def test_illegal_status_is_rejected(self):
        with self.assertRaises(ValueError):
            make_record(status="whatever")

    def test_illegal_stage_is_rejected(self):
        with self.assertRaises(ValueError):
            make_record(stage="s9")

    def test_illegal_run_format_is_rejected(self):
        with self.assertRaises(ValueError):
            make_record(run_id="R-20261030-001-RUN99x")


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
class FileTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "tasks"
        self.store = FileTaskStore(self.root)

    def test_save_and_get(self):
        record = make_record()
        self.store.save(record)
        self.assertEqual(self.store.get(record.review_id), record)

    def test_missing_returns_none(self):
        self.assertIsNone(self.store.get("R-20261030-999"))

    def test_no_tmp_file_left_behind(self):
        self.store.save(make_record())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["R-20261030-001.json"])

    def test_all_skips_corrupt_file(self):
        self.store.save(make_record())
        (self.root / "R-20261030-002.json").write_text("{not json", encoding="utf-8")
        (self.root / "R-20261030-003.json").write_text(
            json.dumps({"schema": 1}), encoding="utf-8")
        self.assertEqual([r.review_id for r in self.store.all()],
                         ["R-20261030-001"])

    def test_empty_root_lists_nothing(self):
        self.assertEqual(self.store.all(), [])


# ---------------------------------------------------------------------------
# 任务服务
# ---------------------------------------------------------------------------
class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.target = self.base / "a.txt"
        self.target.write_text("第一句。市场占有率达到 90%。", encoding="utf-8")
        self.store = FileTaskStore(self.base / "tasks")
        self.allocator = CounterAllocator()

    def service(self, runner):
        return TaskService(runner=runner, store=self.store,
                           allocate=self.allocator, root=self.base)

    def test_start_review_records_everything(self):
        runner = StubRunner()
        record = self.service(runner).start_review(
            self.target, profile_id="doc-review", purpose="找出夸大表述")

        self.assertEqual(record.review_id, "R-20261030-001")
        self.assertEqual(record.run_id, format_run_id(record.review_id, 1))
        self.assertEqual(record.status, RunStatus.COMPLETED.value)
        self.assertEqual(record.stage, Stage.FINALIZE.value)
        self.assertEqual(record.verdict, "warn")
        self.assertEqual(record.findings, 2)
        self.assertEqual(record.target, "a.txt")          # 相对 root 存
        self.assertTrue(record.report_path.endswith("R-20261030-001.json"))
        self.assertTrue(record.created_at and record.updated_at)

    def test_runner_receives_the_same_identity(self):
        runner = StubRunner()
        record = self.service(runner).start_review(
            self.target, profile_id="doc-review", purpose="p", engine="v1")
        call = runner.calls[0]
        self.assertEqual(call["review_id"], record.review_id)
        self.assertEqual(call["profile_id"], "doc-review")
        self.assertEqual(call["purpose"], "p")
        self.assertEqual(call["engine"], "v1")
        self.assertFalse(call["use_llm"])

    def test_running_is_persisted_before_execution(self):
        """执行前就得有一条 running 记录——否则进程被杀时「有没有这个任务」无从判断。"""
        seen = {}

        def failing(target, *, review_id, profile_id, purpose, engine, use_llm):
            seen["record"] = self.store.get(review_id)
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.service(failing).start_review(self.target)

        self.assertIsNotNone(seen["record"])
        self.assertEqual(seen["record"].status, RunStatus.RUNNING.value)

    def test_failure_is_recorded_and_reraised(self):
        runner = StubRunner(error=RuntimeError("模型不可用"))
        with self.assertRaises(RuntimeError):
            self.service(runner).start_review(self.target)

        record = self.store.get("R-20261030-001")
        self.assertEqual(record.status, RunStatus.FAILED.value)
        self.assertIn("模型不可用", record.error)
        self.assertIn("RuntimeError", record.error)

    def test_missing_target_creates_no_record(self):
        with self.assertRaises(NotFoundError):
            self.service(StubRunner()).start_review(self.base / "nope.txt")
        self.assertEqual(self.allocator.calls, [])       # 连号都没分配
        self.assertEqual(self.store.all(), [])

    def test_get_task(self):
        service = self.service(StubRunner())
        record = service.start_review(self.target)
        self.assertEqual(service.get_task(record.review_id), record)
        self.assertIsNone(service.get_task("R-20261030-777"))

    def test_list_runs_is_newest_first_with_limit(self):
        service = self.service(StubRunner())
        first = service.start_review(self.target)
        second = service.start_review(self.target)
        # 同一秒创建：靠 review_id 倒序保证结果稳定
        self.assertEqual([r.review_id for r in service.list_runs()],
                         [second.review_id, first.review_id])
        self.assertEqual([r.review_id for r in service.list_runs(limit=1)],
                         [second.review_id])

    def test_mark_changes_status(self):
        service = self.service(StubRunner())
        record = service.start_review(self.target)
        updated = service.mark(record.review_id, RunStatus.ABORTED,
                               error="用户取消")
        self.assertEqual(updated.status, RunStatus.ABORTED.value)
        self.assertEqual(updated.error, "用户取消")
        self.assertEqual(service.get_task(record.review_id), updated)

    def test_mark_unknown_task_is_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service(StubRunner()).mark("R-20261030-404",
                                            RunStatus.ABORTED)


# ---------------------------------------------------------------------------
# 报告服务
# ---------------------------------------------------------------------------
def _write_report(root: Path, review_id: str, *, verdict="warn",
                  issues=2, report_hash="abc") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    report = {
        "review_id": review_id,
        "verdict": verdict,
        "core_conclusion": f"共 {issues} 条异议",
        "issues": [{"id": f"ISS-{i:04d}"} for i in range(1, issues + 1)],
        "integrity": {"report_hash": report_hash},
    }
    path = root / f"{review_id}.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return path


class ReportServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.v1 = self.base / "v1"
        self.legacy = self.base / "outputs"
        self.legacy.mkdir(parents=True)
        self.service = ReportService({"v1": self.v1, "legacy": self.legacy})

    def test_v1_wins_over_legacy(self):
        _write_report(self.v1, "R-20261030-001", verdict="fail")
        _write_report(self.legacy, "R-20261030-001", verdict="pass")
        report, source = self.service.get_report_with_source(
            "R-20261030-001-RUN01")
        self.assertEqual(source, "v1")
        self.assertEqual(report["verdict"], "fail")

    def test_legacy_is_the_fallback(self):
        _write_report(self.legacy, "R-20261030-002", verdict="pass")
        report, source = self.service.get_report_with_source(
            "R-20261030-002-RUN01")
        self.assertEqual(source, "legacy")
        self.assertEqual(report["verdict"], "pass")

    def test_missing_report_returns_none(self):
        self.assertIsNone(self.service.get_report("R-20261030-404-RUN01"))
        self.assertIsNone(self.service.locate("R-20261030-404-RUN01"))

    def test_invalid_run_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.get_report("R-20261030-001")

    def test_verify_without_hasher_is_unverified_not_true(self):
        _write_report(self.v1, "R-20261030-003")
        self.assertIsNone(self.service.verify("R-20261030-003-RUN01"))

    def test_verify_detects_match_and_mismatch(self):
        _write_report(self.v1, "R-20261030-004", report_hash="good")
        reports = ReportService({"v1": self.v1},
                                compute_hash=lambda report: "good")
        self.assertTrue(reports.verify("R-20261030-004-RUN01"))

        tampered = ReportService({"v1": self.v1},
                                 compute_hash=lambda report: "other")
        self.assertFalse(tampered.verify("R-20261030-004-RUN01"))

    def test_verify_without_stored_hash_is_false(self):
        self.v1.mkdir(parents=True)
        (self.v1 / "R-20261030-005.json").write_text(
            json.dumps({"review_id": "R-20261030-005"}), encoding="utf-8")
        reports = ReportService({"v1": self.v1},
                                compute_hash=lambda report: "whatever")
        self.assertFalse(reports.verify("R-20261030-005-RUN01"))

    def test_list_reports_dedupes_and_sorts(self):
        _write_report(self.v1, "R-20261030-001")
        _write_report(self.legacy, "R-20261030-001")     # 同一 id 不应出现两次
        _write_report(self.legacy, "R-20261030-002")
        rows = self.service.list_reports()
        self.assertEqual([r["review_id"] for r in rows],
                         ["R-20261030-002", "R-20261030-001"])
        self.assertEqual({r["review_id"]: r["source"] for r in rows},
                         {"R-20261030-001": "v1", "R-20261030-002": "legacy"})
        self.assertEqual(rows[0]["issues"], 2)
        self.assertEqual(rows[0]["verdict"], "warn")

    def test_broken_report_is_listed_as_unreadable(self):
        self.v1.mkdir(parents=True)
        (self.v1 / "R-20261030-009.json").write_text("{oops", encoding="utf-8")
        rows = self.service.list_reports()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["readable"])

    def test_unknown_verdict_is_normalized_to_none(self):
        _write_report(self.v1, "R-20261030-010", verdict="maybe")
        rows = self.service.list_reports()
        self.assertIsNone(rows[0]["verdict"])


# ---------------------------------------------------------------------------
# 真实集成：真 Agent / 真规则跑一遍
# ---------------------------------------------------------------------------
@unittest.skipUnless((ROOT / "agents").is_dir(),
                     "agents/ 不在仓库（公开 clone 不含 Agent 配置），跳过真实集成用例")
class TaskServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        import review

        self.review = review
        self._saved = {name: getattr(review, name)
                       for name in ("OUTPUTS", "LOGS", "LEDGER", "STATE",
                                    "GAP_LOG")}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger", "state"):
            (base / name).mkdir()
        review.OUTPUTS = base / "outputs"
        review.LOGS = base / "logs"
        review.LEDGER = base / "ledger"
        review.STATE = base / "state"
        review.GAP_LOG = base / "state" / "gap_log.jsonl"
        self.addCleanup(self._restore)

        # 目标必须在仓库内：流水线会算 target 相对仓库根的路径
        (ROOT / "targets").mkdir(exist_ok=True)
        self.target = ROOT / "targets" / "_task_service_it.txt"
        self.target.write_text(
            "本公司产品市场占有率达到 90%，质量绝对没有问题。交付周期 15 个工作日。",
            encoding="utf-8")

    def _restore(self):
        for name, value in self._saved.items():
            setattr(self.review, name, value)
        try:
            self.target.unlink()
        except OSError:
            pass

    def test_start_get_list_and_verify(self):
        service = self.review.make_task_service()
        record = service.start_review(self.target)

        self.assertEqual(record.status, RunStatus.COMPLETED.value)
        self.assertEqual(record.stage, Stage.FINALIZE.value)
        self.assertTrue(record.report_path, "完成的任务必须指出报告在哪")
        self.assertIn(record.verdict, {"pass", "warn", "fail"})

        # 记录落盘、能查、能列
        self.assertEqual(service.get_task(record.review_id), record)
        self.assertEqual([r.review_id for r in service.list_runs()],
                         [record.review_id])

        # 报告能按 run_id 取到，且哈希可复算
        reports = self.review.make_report_service()
        report, source = reports.get_report_with_source(record.run_id)
        self.assertEqual(source, "v1")
        self.assertEqual(report["review_id"], record.review_id)
        self.assertEqual(report["issues"] and len(report["issues"]), record.findings)
        self.assertIn("unsupported_blocks", report)
        self.assertIs(reports.verify(record.run_id), True)

    def test_two_tasks_get_distinct_ids(self):
        service = self.review.make_task_service()
        first = service.start_review(self.target)
        second = service.start_review(self.target)
        self.assertNotEqual(first.review_id, second.review_id)
        self.assertEqual(len(service.list_runs()), 2)


if __name__ == "__main__":
    unittest.main()
