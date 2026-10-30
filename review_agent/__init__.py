# -*- coding: utf-8 -*-
"""review-agent 的新架构（Architecture v1）。

当前状态（2026-10-30）：**尚未接管旧流程**。
    · 旧流程仍是 review.py（CLI）+ server.py（HTTP）+ tools/（能力实现），行为不变。
    · 本包只提供领域模型、运行时协议、工具协议三块骨架，供阶段 1~2 逐段迁移。
    · 迁移顺序与验收标准见 docs/REFACTOR_PLAN.md。

导入约定：
    from review_agent.domain import Finding, Severity      # 领域层，零第三方依赖
    from review_agent.runtime import NodeResult           # 运行时协议
    from review_agent.capabilities import ToolRegistry    # 工具注册表

为什么新包不放在 tools/ 里：
    tools/ 是「能力实现」的平铺目录（42 个模块，sys.path 注入式 import），
    继续往里加包会让依赖方向更乱。新架构用包内相对导入，依赖方向由目录强制。
"""

__all__ = ["__version__", "ARCHITECTURE_VERSION"]

__version__ = "0.1.0"
ARCHITECTURE_VERSION = "v1"
