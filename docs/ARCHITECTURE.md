# Review Agent 架构 v1

> 状态（2026-10-30，随 `v0.9.2-arch-v1` 更新）：**六层骨架全部落地，旧流程仍是默认路径**。
> `review_agent/` 已提供领域模型、运行时（六节点 + 编排器 + 检查点）、Application Service、
> 工具注册表与适配器；但 `--engine` 缺省仍是 `legacy`，`tools/` 的 42 个模块尚未逐个迁移。
> 逐段迁移的顺序与验收标准见 [REFACTOR_PLAN.md](REFACTOR_PLAN.md)。

## 1. 要解决的是什么

先看事实，不看口号（数字来自 [PHASE0_INVENTORY.md](PHASE0_INVENTORY.md)，可复现）：

| 事实 | 数值 | 说明 |
| --- | --- | --- |
| 全仓 Python | 114 个文件 / 34,638 行 | 其中测试 12,271 行 |
| 主流程 | `review.py` 3,574 行（有效代码） | `run_one` 一个函数 2,292–3,270 行，约 980 行 |
| 状态载体 | `Session`（`review.py:402`）持有 10+ 个可变字段 | quote / 证据 / 争议 / 决策 / 日志全部可被任何函数改写 |
| 能力实现 | `tools/` 平铺 42 个模块，13,274 行 | 靠 `sys.path` 注入互相 import，无注册表、无权限、无统一审计 |
| 结论格式 | 报告 dict 40+ 个顶层键，12 种历史变体 | 无 schema，字段靠约定 |

三个症状：

1. **职责混杂**：解析、规则、语义审、证据锚定、交叉复核、裁定、渲染、哈希链、写盘，全在 `run_one` 里。
2. **状态无契约**：`Session` 是可变字典集合，谁都能改；改完只剩最终值，无法回答「为什么变」。
3. **能力无边界**：工具之间可以任意调用，没有权限、没有入参 schema、没有统一调用记录。

对应地，v1 的三个目标（不是「加功能」，是「把已有的能力装进骨架」）：

* 一次审查的生命周期可完整追踪（阶段、节点、工具、裁定、证据逐层可查）
* 每条结论都能被复算（裁定由策略函数算出，不由模型宣称）
* 每块能力都可单独替换、单独测试（LLM、规则、工具、存储）

## 2. 架构选择

**模块化单体 + 有状态 Agent Runtime + 确定性审查引擎。**

不做微服务，不做多个 Agent 自由对话，不引入消息队列。理由是可验证的约束，不是审美：

* 部署面是 Windows 单机 + `start-console.bat`，没有容器与编排；微服务只会增加启动路径故障点。
* 当前瓶颈是职责混乱（980 行函数、42 个平铺模块），不是吞吐。引入分布式组件解决不了任何现存问题。
* 审查结论必须可复现。多个 Agent 自由对话会产生不可复现的推理路径，与「证据驱动」直接冲突。

## 3. 六层与依赖规则

| 层 | 职责 | 允许依赖 | 当前代码 | 状态 |
| --- | --- | --- | --- | --- |
| 用户交互层 | Web / API / CLI | Application | `web/index.html`、`server.py`、`review.py` 的 CLI（含 `--engine`） | 旧实现 |
| Application | 编排一次审查任务（建任务、选 Profile、启动 Run、取报告） | Runtime、Domain | `review_agent/application/`：`ReviewApplication`（组装/执行/落盘/恢复）、`TaskService`、`ReportService`、影子报告 | **已建（v1）**；未接 server 的 JOBS |
| Agent Runtime | 阶段推进、节点调度、状态、checkpoint、异常 | Capabilities、Domain | `review_agent/runtime/`：协议 + 六节点 + `orchestrator.py` + `serialization.py` | **已建** |
| 审查能力层 | 规则、LLM、检索、证据、工具 | Domain | `review_agent/capabilities/`（工具协议 + `ToolRegistry`）；`tools/`（实现） | 协议与分发已建，`tools/` 未逐个迁移 |
| 领域层 | 六个核心对象、状态机、裁定策略、端口 | **无**（仅标准库） | `review_agent/domain/` | **已建** |
| 基础设施层 | 数据库、文件、模型客户端、审计、日志 | Domain（实现其端口） | `tools/integrity.py`、`tools/llm_client.py`、`ledger/`、`logs/` | 旧实现，未抽象 |

依赖规则的强制口径（不是建议）：

1. `domain` 不得 import `runtime` / `capabilities` / `application` / FastAPI / SQLAlchemy / `tools/`。
2. `runtime` 可以 import `domain`，不得 import `capabilities` 的具体实现（只能依赖其协议）。
3. `capabilities` 可以 import `domain`，不得 import `runtime`（工具不知道自己被哪个节点调用）。
4. `application` 可以 import 三者，反向一律禁止。
5. 基础设施通过 `domain/ports.py` 的 Protocol 注入，领域层不反向依赖。

第 1~3 条由测试守着：`checks/test_architecture_guard.py`（扫 AST，发现越界 import 直接失败）。
第 4 条的**反向依赖**（domain / runtime / capabilities 不得 import application）已并入同一张
`FORBIDDEN` 表，由同一个用例覆盖；正向（application 可 import 三者）无需断言。
第 5 条（基础设施经端口注入）要等 `review_agent/infrastructure/` 落地才有对象可测，
在那之前 `domain/ports.py` 只有 Protocol 定义，不假装已经验证。

> 为什么不是「分六个文件夹」：目录只表达位置，不表达方向。上面五条才是可检查的约束。

## 4. 旧代码的边界与迁移缝

**旧流程不动。** 迁移通过「旁路接管」完成：新层先只读旧数据，再逐步接管写路径。

| 旧资产 | 现状职责 | 迁移目标 | 阶段 |
| --- | --- | --- | --- |
| `run_one`（`review.py:2292`） | 一次审查的全部流程 | Application 用例 + 8 个阶段节点 | 2 |
| `Session`（`review.py:402`） | 可变状态包 | `runtime.RunState`（不可变）+ 端口 | 2 |
| `_h_*` 处理器 + `pack_handlers`（`review.py:1516`） | 维度分发 | `capabilities.ToolRegistry` | 3 |
| `tools/*.py`（42 个） | 能力实现 | `BaseTool` 子类，按批次注册 | 3 |
| 报告 dict（40+ 键） | 对外结论 | 领域聚合 + `extra` 旁路 + `ReportStore` | 3 |
| `integrity` / `ledger`（`tools/integrity.py`） | 哈希链与校验 | 基础设施实现 `ReportStore` / 审计端口 | 4 |

迁移期的硬规矩：**同一份结论不允许新旧两套各写一遍**。阶段 2 的验收方式是新旧双跑、
比对 findings 集合与 verdict，差异逐条解释；不允许「新引擎跑出另一份报告」上线。

**已发生的第一步**（2026-10-30）：报告裁定（`review.py` S6）改为调用
`review_agent.domain.policy.DEFAULT_POLICY`，不再自己写 if/elif。同一输入的改造前后报告
逐键对比（除 `timestamp`、报告哈希、gap 日志绝对路径外）无差异，`decisions.jsonl` 逐字相同。
这条迁移很小，但它证明了「旧流程可以调用领域策略而不改变产物」——阶段 2 的其余接管
都可以照这个方式验证。

## 5. 关键决策记录（ADR）

### ADR-001 领域层零第三方依赖：用 dataclass，不用 Pydantic

* **背景**：方案原文建议 Pydantic 做 Schema。项目 `requirements.txt` 的硬依赖只有 `pyyaml`，
  CI 只装它；README 明确「运行时硬依赖只有 YAML 解析」。
* **决定**：`review_agent/domain` 与 `capabilities` 只用标准库：`dataclasses(frozen=True)` +
  `__post_init__` 显式校验；工具入参用 dataclass 构造校验。
* **理由**：若领域层 import Pydantic，CI（3.12/3.13，只装 pyyaml）会直接失败；
  同时「审查工具能在任何装了 Python 的机器上跑」这条底线会被悄悄改掉。
* **代价**：没有 JSON Schema 自动导出、没有 discriminated union。补偿：应用层（FastAPI 侧）
  需要 schema 时单独用 Pydantic，不往下渗。
* **何时重评**：当 API 层需要对外开放 schema（阶段 5），或领域对象数量翻倍到手写校验不可维护。

### ADR-002 单 Supervisor + 固定节点，不做自由多 Agent

* 节点是**固定拓扑**：Document Analyzer → Rule Analyzer → Semantic Reviewer → Evidence Analyzer
  → Cross Reviewer → Decision Engine，由 Supervisor 按阶段推进。
* 不做「Agent 之间自由讨论、互相评分」：结论不可复现，且无法给每条结论定位到确定的责任节点。
* 详见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md)。

### ADR-003 裁定必须确定性

* 最终 verdict 由 `domain/policy.DecisionPolicy` 纯函数计算：输入 findings + 争议，
  输出 verdict / risk / reasons。LLM 不参与裁定，也不参与严重度定级。
* 理由：验收标准里有一条「能否证明最终裁定符合规则」。模型给出的 verdict 无法证明。
* 现状证据：用该策略复算 202 份历史报告，其中 5 份对不上（旧版本缺陷），已登记为已知偏差，
  见 `checks/test_legacy_adapter.py`。

### ADR-004 证据对象化，锚定由代码校验

* `Evidence` 与 `Finding` 分离：一条发现可以有多条证据（原文 / 外部来源 / 规则 / 知识库）。
* 「quote 必须逐字出现在原文」是硬校验（旧实现 `review.py:2517-2546` 已在做），
  领域层把它表达为 `Verification.ANCHORED` 且必须记录 `verified_by`。
* 理由：证据不是模型的一句话，是可回查的引用。

### ADR-005 Review 与 Run 分离

* 旧实现一次审查 = 一个 `R-id`，重跑换 id，「同一任务第几次审、为什么重审」在数据上消失。
* v1：`Review`（任务身份，稳定）+ `ReviewRun`（执行，递增 `-RUNnn`）。
* 兼容：历史报告按 1:1 映射为 `RUN01`（见 `review_agent/adapters/legacy_report.py`）。

### ADR-006 工具用注册表，不做插件系统

* 一张表 + 一条执行管线（校验 → 权限 → 执行 → 结果校验 → 审计）足够覆盖现存 42 个模块。
* 不做动态发现、不做多层插件抽象：那是「功能扩张期」的产物，与本轮目标相反。

### ADR-007 历史报告只读适配，不重写

* `outputs/` 里的报告是审计证据，不能因为换了模型就批量改写。
* 适配器只读映射，未建模的顶层块（`law_library`、`research`、`action_plan`…）原样进 `extra`。
* 代价：`extra` 是迁移期旁路，不是垃圾桶；新增消费某个键时必须同步改适配器和测试。

### ADR-008 模块化单体，暂不引入分布式组件

* 不引入 Kubernetes / Kafka / Redis 集群 / 微服务。等出现真实瓶颈（并发任务量、单次审查时长、
  多机部署需求）再评估，评估前不预留抽象。

## 6. 目录结构

现在（可运行）：

```
review-agent/
├── review.py                 # 旧主流程（CLI）：S6 裁定已改为调用领域策略
├── server.py                 # 旧控制台后端（stdlib http.server），未改
├── tools/                    # 旧能力实现（42 个模块）
├── rules/ agents/ knowledge/ config/   # 数据与配置
├── outputs/ logs/ ledger/ state/       # 运行产物
├── review_agent/             # 新架构（本文件描述的对象）
│   ├── domain/               # 枚举 / 模型 / 策略 / 状态机 / 端口（零第三方依赖）
│   ├── profiles/             # ReviewProfile 合成（manifest + scenario + format_spec）
│   ├── adapters/             # 历史报告只读映射（迁移期边界层）
│   ├── runtime/              # RunState / Node / NodeResult / advance
│   └── capabilities/         # BaseTool / ToolRegistry / 权限
├── checks/                   # 公开不变量（CI 必跑，含新架构守护测试）
└── docs/                     # 本组文档
```

目标（阶段 2~4 逐步出现）：

```
review_agent/
├── application/     # ReviewService / TaskService / ReportService
├── infrastructure/  # SQLite/PG 仓储、文件存储、LLM 客户端、审计出口
└── engine/          # 六个节点 + Supervisor
```

## 7. 产品形态与当前实现

设计稿（`IMG_20261030_11491694_*.jpg`）给出五个界面的信息架构。本轮**不实现前端**，
但架构必须能支撑它，否则「界面做不出来」会被误判为前端问题。

| 界面 | 需要的数据 | 当前来源 | v1 缺口 |
| --- | --- | --- | --- |
| 审查工作台 | 任务、材料、Profile、目的 | `server.py` 上传 + `review.py` CLI 参数 | Profile 解析器（阶段 2） |
| Agent 执行中心 | 当前阶段、节点、工具调用 | `logs/<id>/task.json` + `tools.jsonl`（事后） | 实时事件流（阶段 4） |
| 证据审查中心 | Finding ↔ 原文 ↔ 证据 | 报告 `issues[].quote` + `target_text` | Evidence 对象与前端（阶段 3） |
| 风险与裁定中心 | 争议、风险等级、人工复核 | 报告 `disputes[]` + `verdict` | Decision 对象 + 复核动作（阶段 2~4） |
| 审计中心 | 运行轨迹、模型版本、历史 | `steps` / `decisions.jsonl` / `ledger/chain.log` | 统一 `AuditEvent` 流（阶段 4） |

设计稿只作信息架构参考：它展示的「Moderate risk」「Source grounded」等标签，
在 v1 里必须落到 `RiskLevel` 与 `Evidence.verification` 字段，不允许只存在于前端文案。

## 8. 不做什么

* 不新增审查维度（先把手上的维度做扎实）。
* 不做模型自由编排（Planner 在 v1 是规则化生成计划，不调用模型）。
* 不做多租户 / 权限体系（单机单用户；审批人只是审计字段）。
* 不做插件市场 / 动态加载（ADR-006）。
* 不在本轮改 `review.py` 的行为（阶段 2 之前，它仍是唯一生产路径）。

## 9. 验收：七个问题

| 问题 | 现状（2026-10-30，`v0.9.2-arch-v1`） | 谁证明 |
| --- | --- | --- |
| 能否追踪一次审查的完整生命周期？ | 旧：`steps` + 三份 jsonl；新：`RunState` 阶段链 + `AuditEvent` + `state/runs/` 落盘 | `checks/test_domain_v1.py::RuntimeStateTests`、`checks/test_orchestrator.py` |
| 能否单独替换 LLM？ | **是**：节点层经 `SemanticExecutor` 注入执行器；模型层经 `ModelClient` 端口注入客户端（`review.make_llm_review_fn(model_client=...)`），真实供应商也走同一条适配缝（`adapters/model.py`） | `checks/test_semantic_node.py`、`checks/test_model_client.py` |
| 能否单独测试每个审查节点？ | **是**：六节点各有独立用例（Decision 16 / Evidence 16 / Cross 17 / Rule 17 / Semantic 21 / Document 15），另有 Planner 8、编排器 21 | `checks/test_*_node.py`、`checks/test_planner_node.py` |
| 能否在中断后恢复执行？ | **状态层是**：`FileRunStore` 落盘 → 读回 → 从断点阶段续跑；「杀进程后自动续跑」仍需入口侧接线 | `checks/test_state_serialization.py`、`checks/test_orchestrator.py::test_resume_does_not_rerun_completed_stages` |
| 能否追溯每条 Finding 的证据？ | **是**：`Evidence` + `verification` + `verified_by`，报告自 schema 1.2 起带 `evidence[]` | `checks/test_legacy_adapter.py`、`checks/test_report_evidence.py` |
| 能否证明最终裁定符合规则？ | **是**：`DecisionPolicy` 纯函数 + `consistency_issues` 复算；全量历史重放仅 5 份已登记偏差 | `checks/test_domain_v1.py::PolicyTests`、`checks/test_decision_node.py` |
| 能否不破坏其他模块地增加 Profile？ | **是**：`ReviewProfile` + `profiles/resolver.py`（四份配置叠加的唯一实现） | `checks/test_profile_resolver.py` |

「部分 / 未做」不是遗漏，是**下一阶段的入口**：每条都在 [REFACTOR_PLAN.md](REFACTOR_PLAN.md)
里有对应交付物与验收命令。
