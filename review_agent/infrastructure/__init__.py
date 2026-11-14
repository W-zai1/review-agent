# -*- coding: utf-8 -*-
"""基础设施层：只放「实现 `domain/ports.py` 端口」的适配器。

当前模块：

    ledger.py   追加式台账（哈希链 + 墓碑），跨进程写锁
    report_store.py 报告 JSON/Markdown 落盘与完整性链
    retention.py    保留期计划与执行（只算不删 / 显式删）
    task_lock.py    任务续跑跨进程租约

依赖方向：本层只能依赖 `domain`（实现它的协议），**不得** import
`runtime` / `capabilities` / `application`，更不得 import 旧代码（`review` / `server` / `tools`）。
这条规则由 `checks/test_architecture_guard.py` 守着。
"""
