#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_failure_injection.py —— 三类故障必须「有明确错误或能恢复」。

阶段 4 的验收原话是：模型不可用、磁盘只读、进程中断，都要给出明确错误或恢复，
**不允许「静默半成品」**。这份用例就是按这三类各打一发：

    1. 模型不可用（注入一个必抛的语义审执行器）
       → 必须停在 analysis 阶段，不许带着「没审完」的状态走到裁定
    2. 磁盘不可写（把输出目录换成同名文件，制造 `NotADirectoryError`）
       → 必须抛错，且**不留下半份报告**（半成品比没有更危险：它看起来是结论）
    3. 进程中断（任务记录已落 `running` 后「进程消失」）
       → 新实例必须还看得见这个任务，并能把它收敛成 aborted；
         不允许「报告没有、任务也查不到」的失忆状态

不用 mock 替换被测对象：故障从**注入点**进（执行器 / 目录 / 重新构造服务），
被测代码本身照常跑。前两个用真目标文件，需要 `agents/`（公开 clone 不含），
缺失时 skip 并写明原因。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.application import FileTaskStore, TaskService  # noqa: E402
from review_agent.domain import RunStatus, Stage  # noqa: E402

HAS_AGENTS = (ROOT / "agents").is_dir()


class _RedirectedWorkspace:
    """把 review 模块的产物目录重定向到临时目录（不污染仓库）。"""

    NAMES = ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")

    def __init__(self):
        import review

        self.review = review
        self._saved = {name: getattr(review, name) for name in self.NAMES}
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger", "state"):
            (base / name).mkdir()
        review.OUTPUTS = base / "outputs"
        review.LOGS = base / "logs"
        review.LEDGER = base / "ledger"
        review.STATE = base / "state"
        review.GAP_LOG = base / "state" / "gap_log.jsonl"
        self.base = base

    def restore(self):
        for name, value in self._saved.items():
            setattr(self.review, name, value)
        self.tmp.cleanup()


@unittest.skipUnless(HAS_AGENTS, "agents/ 不在仓库（公开 clone 不含），跳过真实故障注入")
class ModelUnavailableTests(unittest.TestCase):
    def setUp(self):
        self.ws = _RedirectedWorkspace()
        self.addCleanup(self.ws.restore)
        (ROOT / "targets").mkdir(exist_ok=True)
        self.target = ROOT / "targets" / "_fault_model.txt"
        self.target.write_text(
            "本公司产品市场占有率达到 90%，质量绝对没有问题。", encoding="utf-8")
        self.addCleanup(lambda: self.target.unlink(missing_ok=True))

    def test_model_failure_stops_before_decision(self):
        def boom(_step):
            raise RuntimeError("模型不可用：连接超时")

        state, outcome, _session = self.ws.review.run_typed_pipeline(
            self.target, self.ws.review.load_rules(), semantic_review_fn=boom)

        self.assertFalse(outcome.ok, "模型全失败时不许给出完成态")
        self.assertEqual(state.decision, None,
                         "没有完成分析的运行不该产出裁定")
        self.assertNotIn(Stage.DECISION, state.completed_stages)
        self.assertNotIn(Stage.FINALIZE, state.completed_stages)
        self.assertTrue(outcome.errors, "失败必须说明原因")
        self.assertIn("模型不可用", " ".join(outcome.errors))


class DiskUnwritableTests(unittest.TestCase):
    def setUp(self):
        self.ws = _RedirectedWorkspace()
        self.addCleanup(self.ws.restore)

    def test_report_write_failure_leaves_no_partial_report(self):
        review = self.ws.review
        # 把 outputs/ 这个目录占成一个同名文件：报告目录 mkdir 会抛
        # NotADirectoryError，等价于「输出目录不可写」，但不需要真的改磁盘权限
        # （Windows 上改 ACL 不稳定，换台机器就跑不了）
        blocked = self.ws.base / "outputs"
        for child in sorted(blocked.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
        for child in sorted(blocked.rglob("*"), reverse=True):
            if child.is_dir():
                child.rmdir()
        blocked.rmdir()
        blocked.write_text("我不是目录", encoding="utf-8")

        from review_agent.domain import ReviewProfile, Stage
        from review_agent.runtime import RunOutcome, RunState

        state = RunState(
            review_id="R-20261030-001", run_id="R-20261030-001-RUN01",
            stage=Stage.INTAKE,
            profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
            artifacts={"target_text": "原文"},
        )
        with self.assertRaises(OSError):
            review._write_shadow_report(state, RunOutcome(state=state,
                                                          status="completed"))

        leftovers = list(self.ws.base.rglob("R-20261030-001.json"))
        self.assertEqual(leftovers, [], "写盘失败后不许留下半份报告")


class ProcessInterruptionTests(unittest.TestCase):
    """任务记录在「进程消失」后仍然可查、可收敛——这是中断恢复的第一层。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FileTaskStore(Path(self.tmp.name) / "tasks")

    def _hanging_runner(self, target, *, review_id, profile_id, purpose,
                        engine, use_llm):
        # 模拟「进程在这一步被杀」：记录已经落成 running，执行没返回
        raise KeyboardInterrupt("模拟进程中断")

    def test_running_task_survives_and_can_be_settled(self):
        # 第一个「进程」：建任务 → 执行前落 running → 被杀
        first = TaskService(
            runner=self._hanging_runner, store=self.store,
            allocate=lambda day: "R-20261030-001")
        target = Path(self.tmp.name) / "t.txt"
        target.write_text("待审内容", encoding="utf-8")
        with self.assertRaises(KeyboardInterrupt):
            first.start_review(target)

        # 第二个「进程」：全新的服务实例，只能靠落盘文件知道发生了什么
        second = TaskService(
            runner=self._hanging_runner, store=self.store,
            allocate=lambda day: "R-20261030-002")
        runs = second.list_runs()
        self.assertEqual([r.review_id for r in runs], ["R-20261030-001"])
        self.assertEqual(runs[0].status, RunStatus.FAILED.value,
                         "异常中止必须留痕，而不是停在 running 骗人")
        self.assertIn("中断", runs[0].error)

        settled = second.mark("R-20261030-001", RunStatus.ABORTED,
                              error="进程中断后人工收敛")
        self.assertEqual(settled.status, RunStatus.ABORTED.value)
        self.assertEqual(second.get_task("R-20261030-001").status,
                         RunStatus.ABORTED.value)


if __name__ == "__main__":
    unittest.main()
