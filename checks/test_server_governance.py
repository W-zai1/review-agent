#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_server_governance.py —— 服务端的"不外发"与"删得干净"（批次 2 / C5）。

两条都是**默认值事故**，不是功能缺失：

    1. 用户没勾"调用模型"时，`server.py` 以前**不传** `--llm/--no-llm`，
       `review.py` 于是按 `config/llm.local.json` 的 `enabled: true` 走 ——
       全文照发外部模型。屏幕上的"未勾选"和实际行为相反。
    2. "删除报告"以前只搬 `outputs/<id>.json` 与 `logs/<id>/`，
       而 `outputs/v1/`、`state/runs/*.state.json`（含 `target_text` 全文）、
       `logs/v1/<id>/` 原封不动 —— 用户以为删了，原文还在三个地方。

这里不测 HTTP，只测那两段纯逻辑（`_llm_flag` / `_record_egress` / `_purge_derived`
与被它们改写的 `_delete_report`），因为它们才是"默认值"落地的位置。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import server                                            # noqa: E402

RID = "R-20261101-001"


class LlmFlagTests(unittest.TestCase):
    def test_checked_means_llm(self):
        self.assertEqual(server._llm_flag(True), "--llm")

    def test_unchecked_means_no_llm(self):
        self.assertEqual(server._llm_flag(False), "--no-llm")

    def test_missing_value_means_no_llm(self):
        """接口调用方没带这个字段时，缺省必须落在"不外发"一侧。"""
        self.assertEqual(server._llm_flag(None), "--no-llm")

    def test_only_true_enables_egress(self):
        """只认严格 True：字符串 "false"/"0" 这类真值不能被当成"用户同意了"。"""
        for value in ("false", "0", 0, [], {}, 1.0):
            if value is True:
                continue
            self.assertEqual(server._llm_flag(value), "--no-llm", msg=repr(value))


class EgressLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = server.EGRESS_LOG
        server.EGRESS_LOG = Path(self.tmp.name) / "state" / "egress.log"
        self.addCleanup(self._restore)

    def _restore(self):
        server.EGRESS_LOG = self._saved

    def test_records_metadata_only(self):
        server._record_egress("llm", target="targets/uploads/a.md", job_id="job1",
                              detail="review.py --llm")
        lines = server.EGRESS_LOG.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["kind"], "llm")
        self.assertEqual(row["target"], "targets/uploads/a.md")
        self.assertEqual(row["job_id"], "job1")
        self.assertIn("ts", row)
        self.assertNotIn("prompt", json.dumps(row), "审计日志本身不该记 prompt 内容")

    def test_can_be_disabled(self):
        server._record_egress("llm", target="x", enabled=False)
        self.assertFalse(server.EGRESS_LOG.exists())

    def test_append_only(self):
        for i in range(3):
            server._record_egress("research", target=f"t{i}")
        self.assertEqual(len(server.EGRESS_LOG.read_text(encoding="utf-8").splitlines()), 3)

    def test_write_failure_does_not_raise(self):
        """记录失败不能把审查跑挂了：它只是审计，不是主流程。"""
        server.EGRESS_LOG = Path(self.tmp.name) / "nested" / "file" / "egress.log"
        (Path(self.tmp.name) / "nested").write_text("我是文件不是目录", encoding="utf-8")
        server._record_egress("llm", target="x")     # 不该抛


class _StubHandler:
    """`_delete_report` 只用到收藏表这两个方法；其余全走模块级函数。"""

    def __init__(self):
        self.favs = []

    def _favorites(self):
        return list(self.favs)

    def _save_favorites(self, favs):
        self.favs = list(favs)


class DeleteCascadeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.names = ("OUTPUTS", "LOG_DIR", "TRASH", "LEDGER", "FAVORITES",
                      "STATE_DIR", "LLM_CACHE_DIR")
        self._saved = {name: getattr(server, name) for name in self.names}
        self.addCleanup(self._restore)

        self.outputs = self.base / "outputs"
        self.logs = self.base / "logs"
        self.state_runs = self.base / "state" / "runs"
        self.llm_cache = self.base / "cache" / "llm"
        self.research_cache = self.base / "state" / "research_cache"
        self.text_dir = self.base / "state" / "text"
        self.trash = self.outputs / ".trash"
        for path in (self.outputs, self.outputs / "v1", self.logs, self.logs / "v1",
                     self.state_runs, self.llm_cache, self.research_cache,
                     self.text_dir, self.trash):
            path.mkdir(parents=True, exist_ok=True)
        server.OUTPUTS = self.outputs
        server.LOG_DIR = self.logs
        server.TRASH = self.trash
        server.LEDGER = self.base / "ledger"
        server.STATE_DIR = self.base / "state"
        server.LLM_CACHE_DIR = self.llm_cache
        server.FAVORITES = self.base / "config" / "favorites.json"

    def _restore(self):
        for name, value in self._saved.items():
            setattr(server, name, value)

    def _seed(self):
        (self.outputs / f"{RID}.json").write_text('{"review_id": "%s"}' % RID,
                                                  encoding="utf-8")
        (self.outputs / f"{RID}.md").write_text("# 报告", encoding="utf-8")
        (self.logs / RID).mkdir()
        (self.logs / RID / "task.json").write_text("{}", encoding="utf-8")
        # 三处"原文副本"
        snapshot = self.text_dir / "snapshot.txt"
        snapshot.write_text("原文", encoding="utf-8")
        (self.outputs / "v1" / f"{RID}.json").write_text(json.dumps({
            "target_text": "原文",
            "target": {"snapshot_ref": str(snapshot)},
        }, ensure_ascii=False), encoding="utf-8")
        (self.outputs / "v1" / f"{RID}.md").write_text("# v1", encoding="utf-8")
        (self.state_runs / f"{RID}-RUN01.state.json").write_text(
            '{"artifacts": {"target_text": "原文"}}', encoding="utf-8")
        (self.logs / "v1" / RID).mkdir(parents=True)
        (self.logs / "v1" / RID / "events.jsonl").write_text("{}\n", encoding="utf-8")
        (self.llm_cache / "exclusive.json").write_text(json.dumps({
            "issues": [], "_owners": [RID]}), encoding="utf-8")
        (self.llm_cache / "shared.json").write_text(json.dumps({
            "issues": [], "_owners": [RID, "R-OTHER"]}), encoding="utf-8")
        (self.research_cache / "exclusive.json").write_text(json.dumps({
            "verdict": {}, "_owners": [RID]}), encoding="utf-8")
        (self.base / "state" / "gap_log.jsonl").write_text(
            json.dumps({"review_id": RID, "op": "new", "gap_id": "G1"}, ensure_ascii=False)
            + "\n"
            + json.dumps({"review_id": "R-OTHER", "op": "new", "gap_id": "G2"},
                         ensure_ascii=False) + "\n",
            encoding="utf-8")

    def delete(self):
        return server.Handler._delete_report(_StubHandler(), RID, {"confirm": RID})

    def test_cascade_moves_every_copy(self):
        self._seed()
        result = self.delete()
        self.assertTrue(result["ok"], result)
        moved = " ".join(result["moved"])
        for fragment in (f"{RID}.json", f"{RID}.md", f"logs/{RID}/"
                        "outputs/v1/", "state/runs/", "logs/v1/"):
            self.assertIn(fragment.split("/")[0], moved, result["moved"])
        # 原位置必须都不在了
        self.assertFalse((self.outputs / "v1" / f"{RID}.json").exists())
        self.assertFalse((self.state_runs / f"{RID}-RUN01.state.json").exists())
        self.assertFalse((self.logs / "v1" / RID).exists())
        # 内容进了回收站（可恢复），不是被无声抹掉
        self.assertTrue(any(p.name.startswith("v1_") for p in self.trash.iterdir()))

    def test_requires_confirm(self):
        self._seed()
        result = server.Handler._delete_report(_StubHandler(), RID, {})
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("need_confirm"))
        self.assertTrue((self.outputs / f"{RID}.json").exists(), "未确认时不能动文件")

    def test_unknown_id_is_reported(self):
        result = self.delete()
        self.assertFalse(result["ok"])
        self.assertIn("未找到", result["error"])

    def test_delete_survives_missing_derived_files(self):
        """只有正式报告、没有 v1/state 副本时也要能删（老报告就是这样）。"""
        (self.outputs / f"{RID}.json").write_text("{}", encoding="utf-8")
        result = self.delete()
        self.assertTrue(result["ok"], result)

    def test_cascade_purges_owned_caches_snapshot_and_gap_rows(self):
        self._seed()
        result = self.delete()
        self.assertTrue(result["ok"], result)

        # 独占缓存被移走；共享缓存只摘掉当前审查号。
        self.assertFalse((self.llm_cache / "exclusive.json").exists())
        self.assertFalse((self.research_cache / "exclusive.json").exists())
        shared = json.loads((self.llm_cache / "shared.json").read_text(encoding="utf-8"))
        self.assertEqual(shared["_owners"], ["R-OTHER"])

        # 未被其他报告引用的原文快照也被移走。
        self.assertFalse((self.text_dir / "snapshot.txt").exists())

        # 缺口日志只删本报告的行，其他报告的原样保留。
        lines = (self.base / "state" / "gap_log.jsonl").read_text(
            encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["review_id"], "R-OTHER")
        self.assertIn("state/gap_log.jsonl", " ".join(result["moved"]))


if __name__ == "__main__":
    unittest.main()
