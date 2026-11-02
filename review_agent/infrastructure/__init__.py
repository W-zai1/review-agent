# -*- coding: utf-8 -*-
"""基础设施层：只放「实现 `domain/ports.py` 端口」的适配器。

现状只有一个模块：

    ledger.py   追加式台账（哈希链 + 墓碑），跨进程写锁

计划中的另外两个（见 `docs/V1-架构收口与整改方案.md` C3/C5，尚未实现）：

    retention.py    保留期计划与执行（只算不删 / 显式删）
    llm_gateway.py  模型供应商适配入口

刻意不预建：空模块会让人以为能力已经有了。**本层的模块数上限是 3**——
超过就说明在拿目录整齐替代依赖治理（与 ARCHITECTURE ADR-008 同一条理由）。

依赖方向：本层只能依赖 `domain`（实现它的协议），**不得** import
`runtime` / `capabilities` / `application`，更不得 import 旧代码（`review` / `server` / `tools`）。
这条规则由 `checks/test_architecture_guard.py` 守着。
"""
