# Agent Runtime v1

> 代码：`review_agent/runtime/node.py`（`RunState` / `NodeContext` / `NodeResult` / `Node` / `Workflow` / `advance`）
> 状态机：`review_agent/domain/transitions.py`　裁定：`review_agent/domain/policy.py`
> 本文是**规范**：节点清单、阶段产物、失败语义、恢复语义。调度器（Planner / Executor）在阶段 2 实现。

## 1. 为什么不是「多 Agent 自由讨论」

方案原文的判断是对的：问题不是 Agent 不够多，是职责不清。v1 采用**单 Supervisor + 固定节点**：

* 拓扑固定：六个专业节点，顺序由阶段决定，不互相「商量」。
* 每个结论必须能指到**一个**节点、**一条**规则、**一段**原文。
* 自由讨论产生的结论无法复现，也无法在审计里定位责任节点——与「证据驱动」冲突。

## 2. 拓扑

```
                    ┌──────────────────────┐
                    │   Review Supervisor  │  阶段推进 / 异常 / 恢复
                    └──────────┬───────────┘
        ┌──────────┬───────────┼───────────┬───────────┬──────────┐
        ▼          ▼           ▼           ▼           ▼          ▼
  Document    Rule        Semantic    Evidence    Cross      Decision
  Analyzer    Analyzer    Reviewer    Analyzer    Reviewer   Engine
   摄取/结构   确定性规则   LLM 语义    证据锚定    冲突/重复   确定性裁定
```

Supervisor 本身不产出结论：它只做四件事——挑下一个节点、校验产物、记录状态迁移、决定重试/终止。

## 3. 节点清单

| 节点 | 阶段 | 输入 | 输出 | 用 LLM | 旧实现位置 |
| --- | --- | --- | --- | --- | --- |
| Document Analyzer | ingestion | 文件路径 / 字节 | `target_text`、`target_meta`（类型、编码、结构） | 可选（docx/pdf 解析用库；类型判定可规则化） | `read_target_text` 2268、`detect_type_detail` 252、`docx_extract` |
| Rule Analyzer | analysis | 目标文本、Profile 规则集 | `findings`（规则类） | **不用** | `run_agent_steps` 1821 + `_h_*` 1516 + `rule_exec` / `regex_check` |
| Semantic Reviewer | analysis | 目标文本、任务书、维度 | `findings`（模型类）+ `llm_meta` | **用** | `semantic_review` 1843、`llm_review`、`llm_client` |
| Evidence Analyzer | evidence | findings、原文、知识库 | `evidence`、`evidence_dropped` | 混合（检索可算法，判断可模型） | S4 锚定 2517、`law_cite`、`retrieve`、`law_index` |
| Cross Reviewer | cross_review | findings（全维度） | `disputes`、折叠/降权决策 | 可选（先规则，复杂冲突再用模型） | `cross_review` 1359、`aggregate_repeats` 1401、`drop_covered_comp004` |
| Decision Engine | decision | findings、disputes、coverage、研究兜底 | `decision` | **不用** | S6 裁定 2639–2686 + `remediation.attach` |
| Review Supervisor | 全部 | RunState | 新 RunState / 终止原因 | 不用 | `run_one` 的控制流 2292–3270 |

**实现进度**（2026-10-30）：

* Decision Engine 已落地：`review_agent/runtime/nodes/decision.py`（节点 + 纯函数 `build_decision`）。
  旧流程 S6 已改为调用它算裁定；`Session.issues` 经
  `review_agent/adapters/issue_codec.py` 编成 `Finding` 后再交给节点。
  单测：`checks/test_decision_node.py`（16 项；含全量历史报告重放，差异只允许已登记的 5 份）。
* Evidence Analyzer 已落地：`review_agent/runtime/nodes/evidence.py`
  （节点 + 纯函数 `anchor_findings` / `verify_quote`）。旧流程 S4 已改为按节点结论过滤条目；
  证据对象带 `finding_ids` 指回它支撑的发现，`advance()` 校验不出现悬空指向。
  单测：`checks/test_evidence_node.py`（四类引用归一 + 历史语料与旧实现逐条等价）。
* Cross Reviewer 已落地：`review_agent/runtime/nodes/cross_review.py`
  （节点 + `review_findings`）。旧流程 S5 已改成「编成领域对象 → 节点复核 → 回写」；
  降权通过 `NodeResult.revisions` 表达（见 §4 规则 6）。
  单测：`checks/test_cross_review_node.py`（规则 9 项 + 差分：全量历史报告与旧实现逐字段一致）。
* Rule Analyzer 已落地：`review_agent/runtime/nodes/rules.py`
  （节点 + `plan_rule_steps` / `run_rule_plan`）。旧流程 S3 的调度已改为走同一份计划；
  检查函数本身仍在能力层（`_h_*` / Agent 自带 `checks.py`），通过 `RuleExecutor` 端口注入，
  处理器改造留给阶段 3 的 Tool Registry。产物多了 `analysis_stats`（每步命中数）。
  单测：`checks/test_rule_analyzer_node.py`（计划 / 执行 / 失败语义 / 状态推进）。
* Orchestrator 已落地：`review_agent/runtime/orchestrator.py`（`Orchestrator` +
  `MemoryCheckpointStore` / `FileCheckpointStore` + `state_hash`）。
  同阶段可挂多个节点（analysis = 规则审 + 语义审），结果先 `merge_results()` 合成、
  再 `advance()` **推进一次**；每阶段落一条 `Checkpoint`（阶段 + 序号 + state_hash）。
  失败停在原地、可重试才重试、未编排的阶段报「未覆盖」而不是悄悄跳过。
  单测：`checks/test_orchestrator.py`（21 项）。
  尚未接入 `run_one`：等 Application Service 与剩余节点。
* Semantic Reviewer 已落地：`review_agent/runtime/nodes/semantic.py`
  （节点 + `plan_semantic_steps` / `build_meta` / `coverage_from_meta`）。
  模型调用通过 `SemanticExecutor` 端口注入，节点负责「维度×块」执行顺序、
  成功块区间并集覆盖率、失败块记账。旧流程的覆盖率推导已改为调用同一份实现。
  单测：`checks/test_semantic_node.py`（21 项：计划 / 覆盖 / 失败 / 编号）。
* 语义审**入队**已落地：`review_agent/capabilities/semantic_ingest.py`
  （去重 / 语境豁免 / 严重度下调 / 编号，全部脱离 Session）；
  适配层 `review.make_semantic_executor()` 把「一次模型调用」接到入库规则上，
  模型调用经 `review_fn` 注入，离线可测。`run_typed_pipeline(..., semantic_review_fn=…)`
  即可把语义审挂进 analysis 阶段。
  单测：`checks/test_semantic_ingest.py`（17 项）。
* 语义审**接真模型**（2026-10-30）：`review.make_llm_review_fn()` 把
  `llm_client.load_config` / `LLMClient` / `llm_review.review_dimension` 接起来
  （按维度取规则、任务书含用户目的、事实库进 prompt、quote 对全文校验）。
  `--engine v1 --llm` 已可跑通（真机验证：deepseek-chat，2 条发现）；
  配置不可用时 `use_llm=True` 直接报 `ConfigError`，`use_llm=None` 降级但打印原因。
  单测：`checks/test_llm_review_fn.py`（6 项，全程 mock，不发请求）。
* Document Analyzer 已落地：`review_agent/runtime/nodes/document.py`
  （节点 + `classify_target`）。「标题与开头加权、正文里提到合同不算合同」的判定规则
  只有一份实现，旧流程的 `detect_type_detail` 已改为委托。
  单测：`checks/test_document_node.py`（15 项：扩展名 / JSON / 合同边界 / 读失败）。
* RunState 序列化已落地：`review_agent/runtime/serialization.py`
  （`state_to_dict` / `state_from_dict` / `FileRunStore`）。检查点 + 状态落盘后，
  进程重启可以读回状态、从断点阶段续跑。
  单测：`checks/test_state_serialization.py`（9 项：往返一致 / 脏数据 / 原子写 / 续跑）。
* 六节点全部就位；另有两个「运行时补位」节点：**Planner**（planning 阶段，
  把「这次承诺审什么」落成 `plan` 产物）与 Finalize（最小实现，把状态收敛成报告骨架，
  真正的报告写出仍在旧链路）。编排器与检查点可用。
* **影子流水线**（2026-10-30）：`review.run_typed_pipeline()` 用六节点 + 编排器
  跑一次 rule-only 审查，`review.compare_with_report()` 与旧报告双跑比对。
  四个目标语料上「未解释差异 = 0」。语义审入队仍是 Session 语义，尚未接入。
* **引擎开关**（2026-10-30）：CLI `--engine legacy|v1|shadow`（也认 `REVIEW_ENGINE`）。
  `v1` 跑六节点流水线、落状态（`state/runs/`）并写影子报告（`outputs/v1/`），
  **不写旧链路的正式报告**；`--llm` 已接真模型（语义审经 `SemanticExecutor` 注入）。
  `shadow` 出旧报告 + 双跑差异（`state/double_run/<review_id>.json`），**强制 `--no-llm`**——
  双跑比的是确定性结论，模型波动混进来会让「未解释差异」无从判定。
* **任务与报告服务**（2026-10-31）：`review_agent/application/tasks.py` 的 `TaskService`
  把「跑一次」变成落盘记录（`state/tasks/<review_id>.json`，先写 `running` 再执行），
  `list_runs()` 按更新时间倒序；`application/reports.py` 的 `ReportService` 按 `run_id`
  取报告（v1 优先、legacy 兜底），哈希校验是注入的（没注入时 `verify()` 返回 `None`，
  不返回 `True`）。单测：`checks/test_task_service.py`（32 项，含真 Agent 的端到端）。

**LLM 使用边界**（沿用方案原文，并给出代码落点）：

| 节点 | 是否 LLM | 原因 |
| --- | --- | --- |
| Document Analyzer | 可选 | 解析是确定性工作；类型判定猜错要记置信度 |
| Rule Analyzer | 不需要 | 规则必须可复现，同输入同输出 |
| Semantic Reviewer | 需要 | 语义理解是模型唯一不可替代的部分 |
| Evidence Analyzer | 混合 | 定位/比对用算法，相关性判断可用模型 |
| Cross Reviewer | 可选 | 冲突检测先用规则（同片段、定级差），复杂冲突再升级 |
| Decision Engine | 不需要 | 裁定必须能被复算（见 ARCHITECTURE ADR-003） |

## 4. 契约

```python
# review_agent/runtime/node.py
@dataclass(frozen=True, slots=True)
class RunState:
    review_id: str
    run_id: str
    stage: Stage
    profile: ReviewProfile
    completed_stages: tuple[Stage, ...] = ()
    findings: tuple[Finding, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    disputes: tuple[Dispute, ...] = ()
    decision: Decision | None = None
    artifacts: Mapping[str, Any] = ...      # target_text / plan / report …
    warnings: tuple[str, ...] = ()
    attempt: int = 1
```

规则（全部由代码强制，不是约定）：

1. **不可变**：状态只能通过 `advance()` 产生新对象；节点拿不到别的阶段的写权限。
2. **单阶段**：一个节点只跑一个阶段，`Node.stage` 固定；`advance()` 校验阶段匹配。
3. **产物白名单**：`NodeResult.output` 的键必须在 `STAGE_ARTIFACTS[stage]` 里，
   写错键名立刻失败（`InvariantViolation`），而不是半年后没人知道 `report` 在哪。
4. **失败必须说明**：`NodeResult(success=False)` 且 `errors` 为空 → 构造即失败。
5. **归属校验**：`findings` / `evidence` / `disputes` / `decision` 的 `run_id` 必须与状态一致。
6. **修订显式化**：节点不能改上游对象。要改已有结论，只能通过 `NodeResult.revisions`
   交出「同一 id 的新版本」，由 `advance()` 按下标替换。约束：
   只能修订本阶段之前已存在的发现；同一阶段既新增又修订同一 id 直接被拒。
   例子：交叉复核给冲突双方各降 0.05 —— 旧实现就地改 dict，改完只剩最终值，
   「为什么从 0.75 变成 0.70」在报告里查不出来。
7. **一阶段一推进**：同一阶段可以有多个节点（见 §3 的 analysis），但
   `completed_stages` 只能前进一次。多个节点的结果先由 `merge_results()` 合成
   （产物键必须唯一、发现编号必须唯一），再交给 `advance()`。
   每个节点各推进一次会让阶段变成「第一次的序号」，恢复语义随之失效。
8. **编号由编排器发放**：节点新建发现必须从 `ctx.next_finding_seq` 起号
   （`ids.format_finding_id(ctx.next_finding_seq + i)`）。编排器按「已有发现最大号 + 1」
   发放，并在同阶段内逐节点接续，所以规则审与语义审不会撞号。
   节点不许自己猜序号，也不许重排已有编号；`merge_results()` 会对重复编号直接报错。

`completed_stages` 必须是 `STAGE_ORDER` 的连续前缀，`stage` 由它推导——这条不变量让
「跳过了某阶段」在状态构造时就暴露。

## 5. 八阶段工作流

| 阶段 | 产物（`STAGE_ARTIFACTS`） | 失败处理 | 旧 S 对应 |
| --- | --- | --- | --- |
| intake | `review_id`、`target_hash` | 不可读文件 → 终止并回显原因，不产出空报告 | S1 前半 |
| ingestion | `target_text`、`target_meta` | 解析失败 → 可重试（换解析器）；仍失败则终止 | S1 |
| planning | `plan` | 规划失败 → 终止（不允许「边跑边想」） | S2 |
| analysis | `findings`、`analysis_stats`、`semantic_stats` | 单维度失败 → 记录失败维度并继续；全失败 → 降级 rule-only | S3 |
| evidence | `evidence`、`evidence_dropped` | 锚定工具失败 → 该批 finding 标 `UNANCHORED` 并进 limitations | S4 |

`evidence_dropped` 不只是统计：它列出的发现**不再算数**——交叉复核与裁定都要
用 `runtime.surviving_findings(state)` 取「排除已丢弃后」的集合。
状态是只增的（发现不可变），所以「移出报告」表现为后续阶段按清单排除，
而不是从 `state.findings` 里删掉。
| cross_review | `disputes` | 复核失败 → 终止（带着未复核的结论出报告 = 假绿灯） | S5 |
| decision | `decision` | 策略无法复算 → 终止；人工复核未完成 → `awaiting_decision` | S6 |
| finalize | `report`（含 `evidence[]`）、`audit_export` | 写入失败 → 重试；仍失败则保留状态，报告下载返回明确错误 | S6 写盘 |

报告的 `evidence[]` 段是 finalize 的产物之一：每条发现对应一条证据记录
（`evidence_id` / `issue_id` / `quote` / `verification`），
`anchored` 表示逐字命中原文、`placeholder` 表示占位式引用（缺失型问题）。
不可回溯的条目在 evidence 阶段已被移出报告，所以这一段**不会**出现 `unanchored`。

旧实现里 **S5.5（目的比对）与 S5.6（库外事实核验）** 是 analysis 的补算子阶段：
它们不改变阶段顺序，只往 `findings` / `artifacts` 追加内容，因此不单列阶段。
把它们做成「阶段」会让人误以为可以跳过——事实上目的比对失败必须写进 limitations。

## 6. 状态与 Checkpoint

* 每个阶段成功后写一个 `Checkpoint(run_id, stage, sequence, state_hash, created_at, summary)`。
  已实现：`Orchestrator` 每推进一个阶段写一条；`FileCheckpointStore` 按行 JSONL 落盘，
  进程被杀也不丢已完成阶段（坏行跳过，不让「还能不能恢复」本身变成异常）。
* `state_hash` 是状态快照的规范化哈希（只摘会让结论变化的字段：阶段、发现身份与定级、
  证据/争议/裁定身份、产物摘要），用于判断「恢复出的状态与断点是否一致」。
* 恢复语义：**只重跑未完成的阶段**。已完成阶段的产物来自 checkpoint，不重新调用模型——
  否则「恢复」会悄悄产生第二份结论。状态本身用 `FileRunStore` 落盘
  （`state_to_dict` / `state_from_dict`，原子替换写），进程重启后读回 `RunState`
  再从它的阶段继续；`state_hash` 用来判断「读回的状态」与「断点记录」是否一致。
* 幂等键：`(run_id, stage, attempt)`。同一键重复执行必须得到同一份产物；
  写操作（报告、ledger、审计）以 `(run_id, stage)` 去重。
* 不做「阶段内断点」：阶段内的中间结果不落盘。阶段粒度是可靠性与复杂度的平衡点。

## 7. 重试 / 超时 / 降级

| 阶段 | 可重试 | 超时（建议） | 降级 |
| --- | --- | --- | --- |
| ingestion | 是（解析器可换） | 60s | 无（读不出就是读不出） |
| planning | 否 | 5s | 无 |
| analysis | 是（按维度/块） | 单块 120s | LLM 全失败 → rule-only，报告注明降级 |
| evidence | 是 | 60s | 锚定失败 → 该条移出报告并计入 `evidence_dropped` |
| cross_review | 是（1 次） | 30s | 失败即终止 |
| decision | 否 | 5s | 无 |
| finalize | 是（写盘） | 30s | 无（失败保留状态供重试） |

超时值不是拍脑袋：阶段 2 用历史 `logs/*/cost.json` 的 `total_latency_ms` 分布校准，
校准结果写进本表并留 ADR。当前不写死，是因为还没有节点级耗时数据。

**已落地（2026-10-31）**：`Orchestrator(timeout=...)` 是**单节点**上限，不是整次审查的上限——
整次超时只能告诉你「跑太久了」，单节点超时能告诉你「卡在哪个节点的第几次尝试」。
超时被记为 `retryable=True` 的失败（模型慢、IO 抖动可重试），而节点自己报的确定性错误
仍不可重试。默认 `None`（不限制），由入口显式打开，迁移期不改变既有行为。
验收：`checks/test_orchestrator.py::NodeTimeoutTests`（6 项）。

**已知边界**：超时只能打断 **await 得住** 的节点。节点内部若做同步阻塞调用
（例如把 `read_target_text` 直接写在 `run()` 里读大文件），`asyncio.wait_for`
无法抢占——那类慢属于「进程内阻塞」，只能靠进程级超时兜底。
上表建议值仍需按真实耗时校准后再写进默认配置。

## 8. 并发模型

* 阶段之间**严格串行**：`evidence` 不结束，`cross_review` 不许开始。
* `analysis` 阶段内部并行：维度 × 文本块，`asyncio.gather` + 信号量限流（默认 4）。
* 合并顺序**确定**：按「维度声明顺序 → 块起始位置」排序，不按完成时间。
  否则同一份输入两次跑出不同的 findings 顺序，报告 hash 永远不稳定。
* 单进程 asyncio，不引入进程池：CPU 密集部分（正则、docx 解析）在阶段 3 评估。

## 9. 观测与审计

一条 `AuditEvent` 回答「谁在什么时候做了什么、花了多久、结果如何」：

| 旧记录 | 新 `AuditKind` | 关键字段 |
| --- | --- | --- |
| `Session.step()` → `task.json` steps | `STEP` | `name`（S1 接收解析）、`status`、`latency_ms`、`detail` |
| `Session.tool()` → `tools.jsonl` | `TOOL` | 工具名、入参摘要、结果摘要、`retry` |
| `Session.decision()` → `decisions.jsonl` | `DECISION` | 决策、依据规则、备选方案、为何不选 |
| LLM 调用（`llm_meta`） | `MODEL` | 模型名、tokens、耗时、失败原因 |
| 阶段迁移 | `STATE` | from → to、`attempt`、`checkpoint` |
| 人工动作（批准/驳回） | `HUMAN` | actor、动作、理由 |

约束：审计事件**先落盘再返回**（进程崩溃不丢已发生事件），序号单调递增。
旧实现的三份文件（`task.json` / `tools.jsonl` / `decisions.jsonl`）保留为兼容产物，
阶段 4 之后由统一事件流生成。

## 10. 迁移路径（阶段 2 的执行细则）

1. **旁路适配**（已完成一半）：历史报告 → `AdaptedReview`，可复算 verdict。
2. **影子双跑**：旧 `run_one` 照常产出报告；同时把它的阶段结果喂给新 `RunState`，
   比对 `stats`、`findings` 集合、`verdict`。差异必须逐条解释（口径差 vs 缺陷）。
3. **节点接管**：按依赖顺序逐个替换——先 Decision（最纯、最易验证），再 Evidence、
   Cross Review、Rule Analyzer、Semantic Reviewer、Document Analyzer。
   每接管一个节点，旧函数保留但不再被调用（不删除，直到阶段 5）。
   *已完成 Decision*：`review.py` S6 的 verdict 计算已改调
   `review_agent.runtime.nodes.decision.build_decision`，领域策略仍是唯一实现；
   issue 出口经 `issue_codec.export_issues` 回写（编码→解码→逐键校验）。
   *已完成 Evidence*：S4 的引用校验改调 `evidence.anchor_findings`（规则唯一实现是
   `verify_quote`），报告条目按节点结论过滤；编不出领域对象时退回同一函数的逐条入口并留痕。
   *已完成 Cross Review*：S5 改调 `cross_review.review_findings`，争议回写走
   `dispute_to_report_dict`（键序与旧报告一致），降权走 `revisions`。
   *已完成 Rule Analyzer*：S3 的调度改由 `rules.plan_rule_steps` 生成、`run_rule_plan`
   执行；旧流程传 `on_error="raise"`（与改造前一致：检查炸了立刻暴露），节点传 `record`。
   *已完成 Semantic Reviewer*：`semantic.plan_semantic_steps` 决定「维度×块」顺序，
   `coverage_from_meta` 是覆盖率的唯一实现（旧流程的 `coverage_from_llm_meta` 已改为委托）；
   模型调用经 `SemanticExecutor` 端口注入。
   *已完成 Document Analyzer*：`document.classify_target` 是类型判定的唯一实现，
   旧流程的 `detect_type_detail` 已改为委托；读文件经 `reader` 端口注入。
   六节点全部就位。下一步是 **Application Service**：把 `run_one` 的入参组装、
   节点执行器（reader / rule executor / semantic executor）与 `FileRunStore` 接起来。
4. **开关**：`REVIEW_ENGINE=v1` 环境变量切换新链路；默认仍是旧链路，直到双跑连续
   一周零未解释差异。
5. **回滚**：把开关切回旧链路即可，不需要回滚数据——两份产物都写同一套 `outputs/` 格式。

判据（硬性）：双跑期间任何一条 finding 的「规则号 + 原文片段 + 严重度」不一致，
都必须有书面解释，否则不许推进下一节点。

## 11. 未定问题（阶段 2 开始前必须回答）

| 问题 | 现状 | 倾向 |
| --- | --- | --- |
| Planner 是否用 LLM？ | 未实现 | 不用：按 Profile 与目标类型确定性生成计划（可复现优先） |
| 人工复核如何进入状态机？ | `awaiting_decision` 已定义，动作未实现 | 阶段 4 加 `HUMAN` 事件 + `Decision.approved_by` |
| 阶段内部分失败如何计入覆盖率？ | 旧实现按块记 `failed_chunks` | 沿用，并进 `Coverage.failed_chunks` |
| 多 Run 的重审触发条件？ | 只有 1:1 历史映射 | 材料变更（`target_hash` 变化）或规则集升级；由 Application 决定 |
| research 结果如何进状态？ | Decision 节点暂从 `ctx.config` 读 `external_unverified` / `coverage_complete` | 阶段 2 扩 `STAGE_ARTIFACTS[evidence]`/`[cross_review]`，把研究兜底升级为登记产物；在那之前不改契约，避免「产物键随手加」 |
| 节点如何分配发现编号？ | **已解决**：`NodeContext.next_finding_seq` + 编排器按「已有最大号 + 1」发放、同阶段逐节点接续 | 节点一律用它起号；`merge_results()` 对重复编号直接报错 |
| `Finding` 与 issue dict 的关系？ | 迁移期双形态：管线内用 dict（S3~S5 边跑边改），S6 出口由 `issue_codec` 单向回写并逐键校验 | 阶段 5 之后 dict 只作为报告序列化格式，管线内一律用 `Finding`；`evidence`（判定依据文字）是否升为领域字段在阶段 3 定 |
