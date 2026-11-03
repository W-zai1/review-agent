#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_ledger_concurrency.py —— 台账「删除」必须是追加，不是重写。

背景（这是本文件存在的唯一理由）：
    旧实现删报告时把整个 `chain.log` 读进内存、改一行、`os.replace` 整文件写回。
    语义没错（seq / report_hash / prev_hash 都没动），机制有三个洞：

    1. **不持锁**：窗口期内若有审查进程在锁内 append，新行被整文件重写静默吞掉；
       而 seq 按「行数 + 1」推，吞一行就意味着之后所有 seq 重复、链断。
    2. **临时名固定**（`chain.tmp`）：两个并发删除互相覆盖。
    3. **Windows 上 `os.replace` 撞上别人正 append 打开的文件会 PermissionError**，
       而 `do_DELETE` 没有 try/except → 连接被掐。

一次性复现 1 很难（要卡在 100 微秒的窗口里），所以这里不试图复现竞态，
而是**直接钉住"不再重写"这个性质**：

    * 墓碑之后，链上原有的每一行必须逐字节不变；
    * 并发 append + tombstone 混合跑完，链仍完整、seq 不重号；
    * 墓碑指向不存在的号 / 重复删除 / prev 不符，都必须显式报错而不是写坏数据。

最后一条（prev 不符）是给「忘了拿锁」准备的：拿着锁却读错了位置，写下去就是断链。
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from review_agent.infrastructure import ledger  # noqa: E402


def make_entry(n, prev="0" * 64, review_id=None):
    return {
        "seq": n,
        "review_id": review_id or f"R-20261101-{n:03d}",
        "target_hash": f"{n:064x}",
        "report_hash": f"{(n * 7):064x}",
        "prev_hash": prev,
        "rules_dir_hash": "d" * 64,
        "rules_version": "v1-test",
        "ts": "2026-11-01T10:00:00+08:00",
    }


class LedgerAppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "chain.log"

    def append_chain(self, count, start_prev="0" * 64, start_seq=1):
        """按真实调用顺序写一串：读 prev → 写 → 用刚写的 hash 当下一行 prev。"""
        prev = start_prev
        seq = start_seq
        for _ in range(count):
            entry = make_entry(seq, prev)
            ledger.append(self.path, entry, expect_prev=prev)
            prev = entry["report_hash"]
            seq += 1
        return prev, seq

    def test_append_fills_seq_and_prev(self):
        ledger.append(self.path, {"review_id": "R-20261101-001",
                                  "report_hash": "a" * 64})
        second = ledger.append(self.path, {"review_id": "R-20261101-002",
                                           "report_hash": "b" * 64})
        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["prev_hash"], "a" * 64)
        ok, message, stats = ledger.verify(self.path)
        self.assertTrue(ok, message)
        self.assertEqual(stats["records"], 2)

    def test_wrong_seq_is_rejected(self):
        self.append_chain(2)
        with self.assertRaises(ledger.LedgerError) as ctx:
            ledger.append(self.path, make_entry(1, "0" * 64))
        self.assertIn("seq", str(ctx.exception))

    def test_stale_prev_is_rejected(self):
        """拿着旧 prev 来写（=忘了拿锁）必须报错，不能写出一条断链的行。"""
        self.append_chain(1)
        with self.assertRaises(ledger.LedgerError) as ctx:
            ledger.append(self.path, make_entry(2, "0" * 64), expect_prev="0" * 64)
        self.assertIn("prev", str(ctx.exception))
        self.assertEqual(len(ledger.read_records(self.path)), 1)


class TombstoneIsAppendOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "chain.log"
        prev = "0" * 64
        for seq in range(1, 6):
            entry = make_entry(seq, prev)
            ledger.append(self.path, entry)
            prev = entry["report_hash"]

    def raw_lines(self):
        return self.path.read_text(encoding="utf-8").splitlines()

    def test_tombstone_does_not_touch_existing_bytes(self):
        before = self.raw_lines()
        ledger.tombstone(self.path, 3, actor="web", reason="误删")
        after = self.raw_lines()
        self.assertEqual(after[:len(before)], before,
                         "墓碑之后原有行必须逐字节不变（旧实现会整文件重写）")
        self.assertEqual(len(after), len(before) + 1, "墓碑只能新增一行")
        self.assertTrue(ledger.is_tombstone(json.loads(after[-1])))

    def test_verify_passes_after_tombstone(self):
        ledger.tombstone(self.path, 2)
        ledger.tombstone(self.path, 4)
        ok, message, stats = ledger.verify(self.path)
        self.assertTrue(ok, message)
        self.assertEqual(stats["records"], 5, "墓碑不占新号：记录数仍是 5")
        self.assertEqual(stats["tombstones"], 2)
        self.assertEqual(stats["deleted"], [2, 4])
        self.assertIn("2 条墓碑", message)
    def test_seq_after_tombstone_continues_from_records(self):
        """删过报告之后，新记录必须是 6，不能因为多了一行就写成 7。"""
        ledger.tombstone(self.path, 5)
        # prev 要取「末条**记录**」的 hash——墓碑不在链上，不能拿它当锚点
        prev, seq = ledger.chain_state(self.path)
        self.assertEqual(seq, 6)
        ledger.append(self.path, make_entry(6, prev))
        records = ledger.read_records(self.path)
        self.assertEqual([r["seq"] for r in ledger.report_records(records)],
                         [1, 2, 3, 4, 5, 6])

    def test_tombstone_unknown_seq_is_rejected(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.tombstone(self.path, 99)
        self.assertEqual(len(ledger.tombstones(ledger.read_records(self.path))), 0)

    def test_double_delete_is_rejected(self):
        ledger.tombstone(self.path, 2)
        with self.assertRaises(ledger.LedgerError) as ctx:
            ledger.tombstone(self.path, 2)
        self.assertIn("已有墓碑", str(ctx.exception))

    def test_restore_is_an_append_event(self):
        before = self.raw_lines()
        ledger.tombstone(self.path, 2)
        ledger.restore(self.path, 2, actor="web", reason="撤销删除")
        after = self.raw_lines()
        self.assertEqual(after[:len(before)], before,
                         "恢复不能改写报告或删除事件")
        self.assertEqual(len(after), len(before) + 2)
        ok, message, stats = ledger.verify(self.path)
        self.assertTrue(ok, message)
        self.assertEqual(stats["records"], 5)
        self.assertEqual(stats["restored"], [2])
        self.assertEqual(stats["deleted"], [])
        view = ledger.entries_for_api(self.path)
        by_seq = {e["seq"]: e for e in view["entries"]}
        self.assertFalse(by_seq[2]["deleted"])
        self.assertTrue(by_seq[2]["restored"])
        self.assertEqual(by_seq[2]["op"], "restore")

    def test_restore_requires_current_delete_and_allows_delete_again(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.restore(self.path, 2)
        ledger.tombstone(self.path, 2)
        ledger.restore(self.path, 2)
        with self.assertRaises(ledger.LedgerError):
            ledger.restore(self.path, 2)
        ledger.tombstone(self.path, 2)
        _ok, _message, stats = ledger.verify(self.path)
        self.assertEqual(stats["deleted"], [2])
        self.assertEqual(stats["restored"], [])
        self.assertEqual(stats["restores"], 1)

    def test_legacy_in_place_tombstone_still_verifies(self):
        """历史那批是「原位改一行」（行内 op=delete，带 report_hash）。不能被判成坏链。"""
        legacy = self.dir / "legacy-chain.log"
        prev = "0" * 64
        for seq in range(1, 4):
            entry = make_entry(seq, prev)
            if seq == 2:
                entry = dict(entry, op="delete", deleted_at="2026-11-01T09:00:00")
            with open(legacy, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            prev = entry["report_hash"]
        ok, message, stats = ledger.verify(legacy)
        self.assertTrue(ok, message)
        self.assertEqual(stats["legacy_tombstones"], 1)
        self.assertEqual(stats["deleted"], [2])
        self.assertEqual(stats["tombstones"], 0)

    def test_api_view_marks_deleted_without_breaking_chain(self):
        ledger.tombstone(self.path, 3)
        view = ledger.entries_for_api(self.path)
        self.assertTrue(view["chain_ok"], "墓碑不该让链判成断（旧实现在这里会报红）")
        self.assertEqual(len(view["entries"]), 5)
        by_seq = {e["seq"]: e for e in view["entries"]}
        self.assertTrue(by_seq[3]["deleted"])
        self.assertEqual(by_seq[3]["op"], "delete")
        self.assertFalse(by_seq[2]["deleted"])
        self.assertEqual(view["deleted"], [3])

    def test_api_view_flags_legacy_in_place_tombstone(self):
        """生产台账里那 63 条历史原位墓碑，界面必须显示为"已删除"而不是"完好"。"""
        legacy = self.dir / "legacy-view.log"
        prev = "0" * 64
        for seq in range(1, 4):
            entry = make_entry(seq, prev)
            if seq == 2:
                entry = dict(entry, op="delete", deleted_at="2026-11-01T09:00:00",
                             deleted_hash=entry["report_hash"])
            with open(legacy, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            prev = entry["report_hash"]
        view = ledger.entries_for_api(legacy)
        self.assertTrue(view["chain_ok"])
        self.assertEqual(view["deleted"], [2])
        by_seq = {e["seq"]: e for e in view["entries"]}
        self.assertTrue(by_seq[2]["deleted"])
        self.assertTrue(by_seq[2]["legacy_tombstone"])
        self.assertEqual(by_seq[2]["deleted_at"], "2026-11-01T09:00:00")

    def test_chain_break_is_still_detected(self):
        """墓碑宽容了，但真断链不能被顺手宽容掉。"""
        lines = self.raw_lines()
        broken = json.loads(lines[2])
        broken["prev_hash"] = "f" * 64
        lines[2] = json.dumps(broken, ensure_ascii=False)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, message, _stats = ledger.verify(self.path)
        self.assertFalse(ok)
        self.assertIn("链条断裂", message)


class ConcurrentWriteTests(unittest.TestCase):
    """8 个写者 × 25 条 + 4 个删除者 × 5 条，同时跑。

    断言只有三条：链完整、seq 不重号、墓碑指向有效。
    并发本身不需要"每次都复现旧 bug"——只要新实现不再依赖"没人同时写"。
    """

    WRITERS = 8
    PER_WRITER = 25
    DELETERS = 4
    PER_DELETER = 5

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "chain.log"

    def test_concurrent_append_and_tombstone(self):
        errors = []
        written = []
        lock = threading.Lock()

        def writer():
            # 每个写者走一遍真实顺序：拿锁 → 读 prev → 写 → 放锁
            for _ in range(self.PER_WRITER):
                try:
                    with ledger.ledger_lock(self.dir):
                        prev, seq = ledger.chain_state(self.path)
                        entry = make_entry(seq, prev,
                                           review_id=f"R-20261101-{seq:03d}")
                        ledger.append(self.path, entry, locked=True)
                        with lock:
                            written.append(seq)
                except Exception as exc:            # noqa: BLE001 - 测试要收集全部失败
                    with lock:
                        errors.append(f"writer: {type(exc).__name__}: {exc}")

        def deleter():
            # 删除者要和写者抢同一份台账：目标号必须**此刻已存在**，
            # 所以用「读当前记录数 → 挑一个还没删过的号 → 试一次」的小循环，
            # 失败（号还没落地 / 被别人抢先）就换个号重试。
            successes = 0
            attempts = 0
            while successes < self.PER_DELETER and attempts < 200:
                attempts += 1
                try:
                    _prev, next_seq = ledger.chain_state(self.path)
                    live = next_seq - 1
                    if live < 1:
                        continue
                    # 按尝试次数轮换目标，否则会反复撞同一个已删的号
                    target = ((attempts * 7) % live) + 1
                    ledger.tombstone(self.path, target)
                    successes += 1
                except ledger.LedgerError as exc:
                    message = str(exc)
                    if "已有墓碑" in message or "不存在" in message:
                        continue                        # 预期竞态
                    with lock:
                        errors.append(f"deleter: {type(exc).__name__}: {message}")
                    return
                except Exception as exc:                # noqa: BLE001
                    with lock:
                        errors.append(f"deleter: {type(exc).__name__}: {exc}")
                    return
            if successes < self.PER_DELETER:
                with lock:
                    errors.append(
                        f"deleter: 只成功 {successes}/{self.PER_DELETER} 次"
                        f"（{attempts} 次尝试）")

        threads = ([threading.Thread(target=writer) for _ in range(self.WRITERS)]
                   + [threading.Thread(target=deleter) for _ in range(self.DELETERS)])
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, [], "并发写入不该报错：\n" + "\n".join(errors[:5]))

        records = ledger.read_records(self.path)
        reports = ledger.report_records(records)
        marks = ledger.tombstones(records)
        self.assertEqual(len(reports), self.WRITERS * self.PER_WRITER,
                         "每条 append 都必须落盘（吞行就是旧实现的病）")
        seqs = [r["seq"] for r in reports]
        self.assertEqual(sorted(seqs), list(range(1, len(reports) + 1)),
                         "seq 必须 1..N 连续且不重号")
        ok, message, stats = ledger.verify(self.path)
        self.assertTrue(ok, message)
        self.assertGreater(len(marks), 0, "并发下至少应有一次墓碑成功")
        for mark in marks:
            self.assertIn(mark["target_seq"], seqs, "墓碑必须指向真实存在的记录")


class ServerWiringTests(unittest.TestCase):
    """server 侧的删除必须走同一套台账实现（不是自己再写一遍重写逻辑）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "ledger").mkdir()

        import server                                   # noqa: E402
        self.server = server
        self._saved = server.LEDGER
        server.LEDGER = self.base / "ledger"
        self.addCleanup(self._restore)
        self.path = self.base / "ledger" / "chain.log"

    def _restore(self):
        self.server.LEDGER = self._saved

    def _seed(self, count=3):
        prev = "0" * 64
        for seq in range(1, count + 1):
            entry = make_entry(seq, prev, review_id=f"R-20261101-{seq:03d}")
            ledger.append(self.path, entry)
            prev = entry["report_hash"]

    def test_mark_ledger_deleted_appends_tombstone(self):
        self._seed(3)
        before = self.path.read_text(encoding="utf-8").splitlines()
        # 只用到 LEDGER 全局，不用 Handler 实例状态
        hit = self.server.Handler._mark_ledger_deleted(object(), "R-20261101-002")
        self.assertTrue(hit)
        after = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(after[:3], before, "删除不能重写已有行")
        ok, message, stats = ledger.verify(self.path)
        self.assertTrue(ok, message)
        self.assertEqual(stats["deleted"], [2])

    def test_mark_ledger_deleted_unknown_id_returns_false(self):
        self._seed(2)
        self.assertFalse(
            self.server.Handler._mark_ledger_deleted(object(), "R-20261101-999"))
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 2)

    def test_mark_ledger_deleted_is_idempotent_safe(self):
        """重复删除只该返回 False（已经打过墓碑），不该抛异常也不该写第二行。"""
        self._seed(2)
        self.assertTrue(
            self.server.Handler._mark_ledger_deleted(object(), "R-20261101-001"))
        self.assertFalse(
            self.server.Handler._mark_ledger_deleted(object(), "R-20261101-001"))
        self.assertEqual(len(ledger.tombstones(ledger.read_records(self.path))), 1)


if __name__ == "__main__":
    unittest.main()
