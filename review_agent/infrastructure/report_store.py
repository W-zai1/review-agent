# -*- coding: utf-8 -*-
"""报告落盘：原文快照、幂等判断、台账追加、JSON/Markdown 写入。

这里只做**持久化**，不组装报告内容。报告的结构、字段、整改建议仍由
`review.build_shadow_report` / `review._attach_action_plan` 决定——这样
报告 schema 只有一处来源，落盘层换实现不会改变交付物。

三个动作的顺序是契约的一部分：

1. 先写原文快照（报告里的 `snapshot_ref` 指向它，失败就照实给 None）；
2. 在台账锁内取 `prev_hash` → 算 `report_hash` → 追加链记录；
3. 最后写 JSON / Markdown。

顺序反了会出现「报告已存在、链上没有」或「链上有、报告读不到」的窗口，
恢复/重跑时无法判断到底写没写成功。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from . import ledger as ledger_store


class ReportStore:
    """把一份已组装好的报告写到磁盘，并接到台账哈希链上。

    路径与外部依赖全部由调用方注入：`review.py` 的模块级 `OUTPUTS/STATE/
    LEDGER/ROOT` 会被测试重定向，落盘层不能自己 `import review` 去读全局。
    """

    def __init__(
        self,
        *,
        outputs: Path,
        ledger: Path,
        state: Path,
        root: Path,
        compute_hash: Callable[[dict[str, Any]], str],
        acquire_lock: Callable[[], Path],
        release_lock: Callable[[Path], None],
        now_iso: Callable[[], str],
    ) -> None:
        self.outputs = Path(outputs)
        self.ledger = Path(ledger)
        self.state = Path(state)
        self.root = Path(root)
        self._compute_hash = compute_hash
        self._acquire_lock = acquire_lock
        self._release_lock = release_lock
        self._now_iso = now_iso

    def already_written(self, json_path: Path, run_id: str | None) -> bool:
        """幂等键 `(run_id, stage=finalize)`：同一次运行只写一份报告、只进一次链。

        为什么按 `run_id` 而不是按内容：同一次执行的结论是确定的（节点必须确定性），
        所以「这个 run_id 已经写过报告」就足以判定重复。按内容比对反而危险——
        内容里含 audit / logs_hash 这类每次都会变的东西，永远比不相等。

        读不出来的坏文件不算「已经写过」：宁可重写一份，也不拿坏文件当既成事实。
        """
        json_path = Path(json_path)
        if not run_id or not json_path.exists():
            return False
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return existing.get("run_id") == run_id

    def write_target_snapshot(self, text: str | None) -> dict[str, str] | None:
        """把被审原文按内容哈希存成快照：`state/text/<sha256>.txt`。

        为什么报告里要指一个文件而不是只嵌原文：报告嵌的是**解码后的文本**，
        读者能读到它，但没法证明「这段字就是当初那份文件里的」；快照文件名就是
        文本的 sha256，改一个字文件名就对不上。

        为什么按内容寻址（文件名 = 哈希）而不是按 review_id：
            同一份材料常被审很多次（生产台账里单个目标最多审过 15 次），
            按运行存会把同一段原文复制十几份。

        幂等：文件已存在就不重写（内容相同，重写只会刷 mtime，
        让 retention 的「目录年龄取内部最新文件」永远判它不过期）。

        返回 `{"ref": 相对仓库根的路径, "hash": sha256}`；没有原文时返回 None。
        """
        if not isinstance(text, str) or not text:
            return None
        payload = text.encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        path = self.state / "text" / f"{digest}.txt"
        try:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                # 先写唯一临时名再原子替换：并发两个进程写同一份快照时，
                # 固定临时名会互相覆盖（批次 1 的 FileTaskStore 踩过同一个坑）。
                tmp = path.with_name(f"{digest}.{os.getpid()}.tmp")
                tmp.write_bytes(payload)
                os.replace(tmp, path)
        except OSError as exc:                          # pragma: no cover - 落盘失败不阻断
            print(f"[v1] 原文快照写入失败：{exc}", flush=True)
            return None
        try:
            ref = path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            # 测试把 STATE 重定向到临时目录：照实给绝对路径，不假装它在仓库里
            ref = path.resolve().as_posix()
        return {"ref": ref, "hash": digest}

    def write_report(
        self,
        report: dict[str, Any],
        *,
        official: bool,
        render_markdown: Callable[[dict[str, Any]], str],
    ) -> Path:
        """写 JSON/Markdown 并追加台账；返回 JSON 路径。

        `official=False` 时写 `outputs/v1/` 与 `ledger/v1-chain.log`，
        与正式链严格分开：影子结论不能污染交付台账。
        """
        out_dir = self.outputs if official else self.outputs / "v1"
        out_dir.mkdir(parents=True, exist_ok=True)
        json_path = out_dir / f"{report['review_id']}.json"
        ledger_path = self.ledger / ("chain.log" if official else "v1-chain.log")

        lock = self._acquire_lock()
        try:
            prev, seq_no = ledger_store.chain_state(ledger_path)
            report["integrity"]["prev_report_hash"] = prev
            report["integrity"]["report_hash"] = self._compute_hash(report)
            ledger_store.append(ledger_path, {
                "seq": seq_no,
                "review_id": report["review_id"],
                "engine": report["engine"],
                "target_hash": report["integrity"]["target_hash"],
                "report_hash": report["integrity"]["report_hash"],
                "prev_hash": prev,
                "rules_dir_hash": report["integrity"]["rules_dir_hash"],
                "rules_version": report["integrity"]["rules_version"],
                "ts": self._now_iso(),
            }, locked=True, expect_prev=prev)
        finally:
            self._release_lock(lock)

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        (out_dir / f"{report['review_id']}.md").write_text(
            render_markdown(report), encoding="utf-8")
        return json_path
