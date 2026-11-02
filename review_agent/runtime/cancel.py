# -*- coding: utf-8 -*-
"""取消令牌：让「用户点了取消」变成一个能被编排器看见的事实。

为什么不是 `asyncio.Task.cancel()`：
    节点大多是同步纯计算（读文件、跑正则、算规则），`asyncio` 只能在 `await`
    点上抢占。真实情况是「一个 200 行的规则检查函数要跑 3 秒」——这段时间里
    `Task.cancel()` 什么也做不了，等它结束时状态已经推进到下一个阶段了。
    更糟的是强杀会留下半写的 `state/runs/*.state.json`（写到一半的文件），
    而阶段边界恰恰是**已经设计好的原子点**（`Orchestrator` 在那里落检查点）。

所以取消的语义是**协作式**的，检查点只有两个：

    阶段开始前   `Orchestrator.run` 每轮循环的第一行
    节点开始前   `_run_node` 每次尝试的第一行

两个都命中时返回 `RunOutcome(status=ABORTED)`，**不写报告**（报告只在 FINALIZE 写），
状态与检查点原样留在磁盘上——于是「取消」和「跑到一半进程被杀」在数据上长得一样，
恢复逻辑不用为取消单独开一条分支。

线程安全：`threading.Event` 本身就是线程安全的，HTTP 线程置位、执行线程读取，
不需要额外加锁。跨进程取消不在这里解决：那要求执行侧是**进程内**的
（见 `docs/V1-架构收口与整改方案.md` C2，服务端必须从子进程改进程内调用）。
"""
from __future__ import annotations

import threading

__all__ = ["CancellationToken", "Cancelled"]


class Cancelled(RuntimeError):
    """执行被取消（节点内部可选择抛它提前退出，编排器按取消处理）。"""


class CancellationToken:
    """一次性开关：置位后不可撤销（要重跑就新建 Run，而不是复位旧令牌）。

    为什么不可撤销：令牌一旦撤销，正在跑的节点会以为「刚才没取消」，
    而调用方已经按"已取消"给用户回了响应——两边对同一个 Run 的理解不一致，
    比没取消更糟。
    """

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""

    # ---- 状态 ----
    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason or "用户取消"

    def __bool__(self) -> bool:          # `if token:` 读起来像 "已取消吗"
        return self.cancelled

    # ---- 控制 ----
    def cancel(self, reason: str = "") -> None:
        """置位。重复调用只保留第一次的原因（第一次才是触发者）。"""
        if not self._event.is_set():
            self._reason = str(reason or "").strip()
        self._event.set()

    def raise_if_cancelled(self) -> None:
        """给节点用的显式检查点（长循环里每 N 条查一次即可）。"""
        if self._event.is_set():
            raise Cancelled(self.reason)

    def reset_for_tests(self) -> None:
        """仅供测试：把令牌复位。生产代码不该调用（见上面"不可撤销"的理由）。"""
        self._event.clear()
        self._reason = ""
