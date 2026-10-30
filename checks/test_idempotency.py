#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_idempotency.py —— 「同一次运行不产生第二份报告」（阶段 4）。

幂等键是 `(run_id, stage=finalize)`。为什么不能按内容比对：报告里有
`audit.latency_ms`、`integrity.logs_hash` 这类每次都会变的东西，内容永远不相等，
拿去当幂等键等于没有幂等。

这条约束不是洁癖——中断恢复之后会重跑最后一个阶段，如果没有幂等：
    · `outputs/v1/<id>.json` 被重写（读者手里的那份和一个小时后下载的不是同一份）
    · `ledger/v1-chain.log` 多一条同结论记录，链就无法回答「到底跑了几次」

坏文件不算「已经写过」：读不出来就重写，不拿坏文件当既成事实。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import integrity  # noqa: E402
import review  # noqa: E402
from review_agent.domain import ReviewProfile, Stage  # noqa: E402
from review_agent.runtime import RunOutcome, RunState  # noqa: E402

REVIEW_ID = "R-20261030-001"


def make_state(run_seq=1):
    return RunState(
        review_id=REVIEW_ID,
        run_id=f"{REVIEW_ID}-RUN{run_seq:02d}",
        stage=Stage.INTAKE,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
        artifacts={"target_text": "本公司产品市场占有率达到 90%。"},
    )


def make_outcome(state):
    return RunOutcome(state=state, status="completed")


class ShadowReportIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._saved = {name: getattr(review, name)
                       for name in ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger", "state"):
            (self.base / name).mkdir()
        review.OUTPUTS = self.base / "outputs"
        review.LOGS = self.base / "logs"
        review.LEDGER = self.base / "ledger"
        review.STATE = self.base / "state"
        review.GAP_LOG = self.base / "state" / "gap_log.jsonl"
        self.addCleanup(self._restore)

    def _restore(self):
        for name, value in self._saved.items():
            setattr(review, name, value)

    def report_path(self):
        return self.base / "outputs" / "v1" / f"{REVIEW_ID}.json"

    def chain_lines(self):
        chain = self.base / "ledger" / "v1-chain.log"
        if not chain.exists():
            return []
        return [line for line in chain.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def write(self, state):
        return review._write_shadow_report(state, make_outcome(state))

    def test_same_run_writes_once(self):
        state = make_state()
        first = self.write(state)
        content = first.read_text(encoding="utf-8")

        second = self.write(state)

        self.assertEqual(first, second, "同一次运行应返回同一份报告")
        self.assertEqual(first.read_text(encoding="utf-8"), content,
                         "第二次调用不该重写报告文件")
        self.assertEqual(len(self.chain_lines()), 1, "链上不该出现第二条同结论记录")

    def test_different_run_is_a_new_report(self):
        self.write(make_state(run_seq=1))
        self.write(make_state(run_seq=2))
        self.assertEqual(len(self.chain_lines()), 2, "换一次运行就该有新记录")
        ok, message = integrity.verify_ledger(
            str(self.base / "ledger" / "v1-chain.log"))
        self.assertTrue(ok, message)

    def test_corrupt_existing_report_is_rewritten(self):
        path = self.report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{坏文件", encoding="utf-8")

        written = self.write(make_state())

        report = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(report["run_id"], f"{REVIEW_ID}-RUN01")
        self.assertEqual(len(self.chain_lines()), 1)

    def test_report_without_run_id_is_not_treated_as_written(self):
        path = self.report_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"review_id": REVIEW_ID}), encoding="utf-8")

        written = self.write(make_state())

        report = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(report["run_id"], f"{REVIEW_ID}-RUN01")


if __name__ == "__main__":
    unittest.main()
