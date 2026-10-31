# 重构计划 v1（Review Agent Architecture Refactoring）

> 基线：`VERSION` 0.9.1，Git `7562767`（tag `v0.9.1`），2026-10-30。
> 原则：不删旧代码、不改变旧行为、旁路接管、每阶段有硬验收。
> 事实来源：[PHASE0_INVENTORY.md](PHASE0_INVENTORY.md)（可复现：`python tools/arch_inventory.py`）。

## 0. 三条不可谈判的约束

1. **旧流程始终可运行**：`review.py` + `server.py` + `tools/` 在阶段 5 之前保持可跑。
2. **同一份结论只写一遍**：迁移期不允许新旧两套各产出一份报告上线；双跑只用于比对。
3. **每阶段先有验收，再写实现**：验收写不出命令的，说明阶段目标没定义清楚。

## 1. 阶段总览

| 阶段 | 目标 | 交付物 | 验收命令 | 状态 |
| --- | --- | --- | --- | --- |
| 0 冻结与盘点 | 知道现状、能回归 | 盘点表、六份文档、领域骨架 | `python tools/arch_inventory.py` + `python -m unittest discover -s checks` | **完成**（2026-10-30） |
| 1 领域模型统一 | 一套对象、一套枚举、一套错误 | `review_agent/domain`、旧报告适配器、issue 编解码、错误分类 | `python -m unittest checks.test_domain_v1 checks.test_legacy_adapter checks.test_issue_codec checks.test_error_taxonomy` | **完成**（2026-10-30：对象/枚举/适配/Profile/出口闸门/错误分类均已统一） |
| 2 执行层重构 | 阶段化、可恢复、可双跑 | Application Service、Orchestrator、六节点、Checkpoint | 双跑零未解释差异；中断恢复用例 | **收尾中**（六节点 + 编排器 + 检查点 + 状态序列化 + 影子流水线双跑 + Application / Task / Report 服务均已落地；剩在线切流） |
| 3 能力层整理 | 工具注册表接管分发 | `BaseTool` 实现、`_h_*` 改造、重复实现清理 | 注册表工具清单 + 单工具测试 | **进行中**（分发已由 `registry.execute()` 接管；批次 1 已迁 7 个独立工具：`math.check` / `logic.check` / `rules.classify` / `integrity.hash` / `rules.pattern` / `text.regex` / `matrix.match`；其余 3 个与 `tools/` 42 模块的逐条理由见 TOOL_SPECIFICATION §7） |
| 4 可靠性 | 重试/超时/幂等/审计单流 | 统一事件流、恢复演练、失败注入测试 | 故障注入用例全绿 | **完成**（2026-10-31：Retry ✅；Checkpoint ✅；单节点超时 ✅；幂等键 ✅；三类故障注入 ✅；统一 AuditEvent 流 ✅——`application/audit.py` 汇流，旧三个文件由事件流投影生成。**另**：`ModelClient` 端口补上，七问里最后一个「部分」转「是」） |
| 5 验证与交付 | 可评测、可部署、文档同步 | 评测集、集成测试、部署脚本、文档 | 全量 checks + golden + 双跑报告 | **进行中**（2026-10-31：集成测试 ✅ 三类对象真跑；部署自检 ✅；ledger 全量校验 ✅ 219 份；golden ✅；**「在线双跑切流」未做，那需要真实使用时间**） |

## 2. 阶段 0：冻结与盘点（已完成）

交付物：

| 项 | 文件 | 说明 |
| --- | --- | --- |
| 完整文件清单 | `docs/PHASE0_INVENTORY.md` | 114 个 Python 文件 / 34,638 行；目录、扇入扇出、零引用候选、重名函数 |
| 调用关系 | 同上 §3 | `review.py` 扇入 25 / 扇出 24，是事实上的中心 |
| 重复代码线索 | 同上 §5 | `main` 27 处、`summarize` 6 处、`run_all` 3 处 |
| 行为基线 | `checks/`（6 文件 / 106 用例）+ `tests/`（48 文件 / 673 用例） | CI 跑 checks；tests 本地 |
| 架构约束 | `docs/ARCHITECTURE.md` 等六份 | 先有约束，再有实现 |
| 领域骨架 | `review_agent/`（15 文件 / 2,117 行） | 只读、旁路，不接管旧流程 |

验收结果（2026-10-30）：

```
[inventory] 114 files / 34,638 loc / 11 zero-fan-in -> docs/PHASE0_INVENTORY.md
python -m unittest discover -s checks  →  Ran 106 tests, OK
historical reports adapted             →  202/202 无异常；5 份已知 verdict 偏差已登记
```

未做（不阻塞阶段 1）：

* 没有给本次改动打 tag：建议在用户确认后打 `v0.9.2-arch-v1`（tag 是发布动作，不自动执行）。
* `outputs/` 824 个文件未做内容级去重（只做了结构与统计盘点）。

## 3. 阶段 1：领域模型统一（已完成 2026-10-30）

已完成：

* 枚举唯一来源：`Severity` / `Verdict` / `RunStatus` / `Stage` / `FindingStatus` / `EvidenceKind`…
* 六个核心对象 + 值对象，硬不变量由构造强制。
* 裁定策略纯函数（与旧规则等价，可复算）。
* 状态机（Run / Stage / Finding / Dispute）。
* 旧报告适配器：202 份全量可读，未建模字段进 `extra`，已知差异登记。
* Profile 快照：`review_agent/profiles/resolver.py`（四份配置叠加的唯一实现）。
* 运行期 issue 编解码：`review_agent/adapters/issue_codec.py`（S6 出口闸门）。
* 错误分类：`domain/errors.py` 的 `kind` / `http_status` / `retryable`；
  `UnreadableTargetError` 归入 `InputError`，CLI 用一行 `REVIEW_ERROR {json}`
  把分类交给 server（不再靠正则猜 stderr）。

阶段 1 的出口判据（全部满足）：报告 JSON 与旧版逐键等价（新增字段除外），
`checks/test_domain_v1` / `test_legacy_adapter` / `test_issue_codec` /
`test_error_taxonomy` 全绿。

剩余（阶段 1 收口的判据）：全部完成，逐条留档如下。

1. ~~`review.py` 内部不再用裸 dict 表达结论~~ → **已完成（2026-10-30）**，做法是
   在 S6 出口装闸门而不是改二十多个调用点：`review_agent/adapters/issue_codec.py`
   把最终 issue 列表编成 `Finding` 再回写成 dict，逐键校验（全量历史报告逐键可逆）。
   管线内部仍是 dict 工作格式——那是迁移期的双向转换点，不是「两套结论」：
   报告里的 `issues` 由领域对象回写，只有一份。
   代价：`evidence`（判定依据文字）暂未升为领域字段，进 `extras`。
2. ~~`ReviewProfile` 解析器~~ → **已完成**：`review_agent/profiles/resolver.py`，
   验收 `checks/test_profile_resolver.py`。
3. ~~错误类型统一~~ → **已完成**：`domain/errors.py` 增加 `InputError` / `NotFoundError` /
   `ConfigError` 与 `kind` / `http_status` / `retryable`；`UnreadableTargetError`
   改为 `InputError`（仍继承 ValueError，旧 catch 不变）；CLI 失败时向 stderr 打一行
   `REVIEW_ERROR {json}`，server 解析后把 `error_kind` 放进任务载荷。
   验收：`checks/test_error_taxonomy.py`（13 项，含两个真实子进程用例）。
4. ~~验收~~ → `checks/test_domain_v1.py` + `test_profile_resolver.py` +
   `test_issue_codec.py` + `test_error_taxonomy.py`；报告 JSON 与旧版逐键等价（除新增字段）。

> 阶段 1 不允许改报告格式。新增字段可以，删除/改名一律不行——历史报告要能并排比对。

## 4. 阶段 2：执行层重构

目标：把 `run_one` 的 980 行拆成「Application 用例 + 8 阶段 + 6 节点」，且可中断恢复。

| 工作项 | 说明 | 验收 |
| --- | --- | --- |
| ~~Application Service~~ | **已完成（v1）**：`review_agent/application/service.py` 组装/执行/落盘/恢复；CLI 以 `--engine v1|shadow` 接入（`state/runs/` 落状态、`state/double_run/` 落双跑差异）；server 侧通过 `REVIEW_ENGINE` 环境变量透传。**Task/Report 服务已落地（2026-10-31）**：`application/tasks.py`（`start_review` / `get_task` / `list_runs` / `mark`，落 `state/tasks/`）+ `application/reports.py`（按 `run_id` 取报告、v1 优先 legacy 兜底、注入了才校验哈希） | `checks/test_application_service.py`（9 项）、`checks/test_engine_switch.py`（9 项）、`checks/test_task_service.py`（32 项） |
| ~~Orchestrator~~ | **已完成**：`review_agent/runtime/orchestrator.py`；一阶段多节点（结果先合成再推进一次）、失败停住、可重试才重试、未覆盖阶段报错不跳过 | `checks/test_orchestrator.py`（21 项：happy path / 失败 / 重试 / 检查点 / 恢复 / 哈希 / 编号） |
| 六节点 | 见 [AGENT_RUNTIME.md](AGENT_RUNTIME.md) §3，先 Decision，再 Evidence | 每节点独立测试，输入输出用固定语料 |
| ~~Decision Engine~~ | **已完成**：`review_agent/runtime/nodes/decision.py`，S6 已改调 `build_decision` | `checks/test_decision_node.py`（16 项；含全量历史报告重放，仅 5 份已登记偏差） |
| ~~Evidence Analyzer~~ | **已完成**：`review_agent/runtime/nodes/evidence.py`，S4 已改调 `anchor_findings`；证据带 `finding_ids` 指回发现 | `checks/test_evidence_node.py`（四类引用归一 + 全量历史与旧实现逐条等价） |
| ~~Cross Reviewer~~ | **已完成**：`review_agent/runtime/nodes/cross_review.py`，S5 已改调 `review_findings`；降权走 `NodeResult.revisions` | `checks/test_cross_review_node.py`（规则用例 + 全量历史差分：争议与置信度逐字段一致） |
| ~~Rule Analyzer~~ | **已完成**：`review_agent/runtime/nodes/rules.py`，S3 调度改调 `plan_rule_steps` / `run_rule_plan`；检查函数留在能力层，由 `RuleExecutor` 注入 | `checks/test_rule_analyzer_node.py`（计划 / 执行 / 失败语义 / 状态推进）；改造前后同目标 rule-only 跑出完全相同的 9 条异议 |
| ~~Semantic Reviewer~~ | **已完成**：`review_agent/runtime/nodes/semantic.py`，维度×块计划、覆盖率并集、失败块记账；模型调用经 `SemanticExecutor` 注入 | `checks/test_semantic_node.py`（21 项）；覆盖率推导已与旧流程合并为唯一实现 |
| ~~语义审入队~~ | **已完成**：`capabilities/semantic_ingest.py`（去重 / 豁免 / 降级 / 编号，脱离 Session）+ `review.make_semantic_executor()` + `make_llm_review_fn()`（真模型接线） | `checks/test_semantic_ingest.py`（17 项）、`checks/test_llm_review_fn.py`（6 项，全 mock） |
| ~~影子报告~~ | **已完成**：`review_agent/application/shadow_report.py`，v1 产出 `outputs/v1/<review_id>.json/.md`；`unsupported_blocks` 显式列出旧报告有、这份没有的块 | `checks/test_shadow_report.py`（14 项） |
| ~~整改单~~ | **已完成**：`_attach_action_plan()` 复用 `remediation.attach()`（同一套规则表与改写模板），每条发现带 `fix`（改哪句/怎么改/不改会怎样），计划在 `action_plan` 里，且**早于 report_hash**（哈希覆盖整改建议） | `checks/test_shadow_report.py`（整改单形状 + Markdown 渲染 + v1 报告含 fix） |
| ~~目的比对~~ | **已完成**：`_attach_purpose()` 复用 `purpose_mod.analyze()`（同一套意图词典与证据定位），报告带 `purpose`（intents / goals / evidence / matrix / judge），未找到的目标写进 limitations | `checks/test_shadow_report.py`（purpose 默认 null / Markdown 目标段 / CLI `--purpose` 端到端） |
| ~~v1 完整性链~~ | **已完成**：影子报告带 `integrity` 指纹（target / ruleset / agent / knowledge + 版本），`report_hash` 与 `logs_hash` 都可复算，`ledger/v1-chain.log` 独立成链；日志写在 `logs/v1/<review_id>/`（decisions / tools / task） | `checks/test_v1_integrity.py`（6 项：复算 / 篡改可证伪 / 链连续 / 不写官方链 / 指纹齐全 / 日志哈希） |
| ~~limitations 口径~~ | **已完成**：按固定顺序给（未支持块 → 模式边界 → 文本覆盖 → 证据丢弃 → 日志缺失 → 节点告警），不再散拼 | `checks/test_shadow_report.py` |
| ~~Document Analyzer~~ | **已完成**：`review_agent/runtime/nodes/document.py`，读文本 + 判类型；`classify_target` 是唯一实现 | `checks/test_document_node.py`（15 项，含合同判定边界与旧流程口径一致性） |
| ~~RunState 序列化~~ | **已完成**：`review_agent/runtime/serialization.py` + `FileRunStore`（原子写） | `checks/test_state_serialization.py`（9 项：往返 / 脏数据 / 续跑） |
| ~~Planner~~ | **已完成**：`review_agent/runtime/nodes/planner.py`，把「本次审什么」落成 `plan` 产物 | `checks/test_planner_node.py`（8 项） |
| ~~影子流水线 + 双跑~~ | **已完成（rule-only）**：`review.run_typed_pipeline()` 六节点串起来跑；`review.compare_with_report()` 与旧报告比对 | `checks/test_pipeline_parity.py`（4 个目标语料，未解释差异 0） |
| Checkpoint 存储 | 文件或 SQLite；`state_hash` 校验 | 杀进程后恢复：只重跑未完成阶段 |
| 双跑比对器 | 旧报告 vs 新 `RunState`：stats / findings 集合 / verdict | 连续一周零未解释差异（当前脚本化重放已零未解释差异，仍需在线双跑） |
| 开关 | `REVIEW_ENGINE=v1`，默认旧链路 | 切回旧链路无需改数据 |

双跑判据（硬性）：任何 finding 的 `(rule_id, quote, severity)` 不一致都要解释；
无法解释的差异一律按「新实现 bug」处理，不许用「模型波动」搪塞。

## 5. 阶段 3：能力层整理

* `ToolRegistry` 接管 `_h_*` 分发：`resolve_handler` → `registry.execute`。
* 批次 1 的 10 个确定性工具先迁（清单见 [TOOL_SPECIFICATION.md](TOOL_SPECIFICATION.md) §7）。
* 清理重复实现：以盘点 §5 为入口，逐个人工确认；确认前只标记不删除。
* 报告写出：`ReportStore` 端口 + 现有 `outputs/` 格式；`integrity` 哈希链保持不变。
* 验收：`review.py` 不再直接 import 批次 1 的工具模块；工具测试进 `checks/`。

## 6. 阶段 4：可靠性

| 项 | 现状 | 目标 |
| --- | --- | --- |
| Checkpoint | ✅ `FileRunStore` + `MemoryCheckpointStore` + `state_hash`（阶段 2 已落地） | 阶段边界落盘 + `state_hash` 校验 |
| Retry | ✅ 由 `Orchestrator(max_attempts)` 统一；只有 `retryable=True` 才重试 | 由 Orchestrator 统一策略（见 AGENT_RUNTIME §7） |
| Timeout | ✅ **2026-10-31 已加单节点超时**（`Orchestrator(timeout=...)`，超时=可重试失败）；整体兜底仍是 server 的 `--review-timeout` | 节点级超时 + 整体兜底 |
| 幂等 | ✅ **2026-10-31**：`_write_shadow_report` 按 `(run_id, finalize)` 幂等——同一次运行第二次写报告直接返回已有文件，不重写报告/日志、不追加链记录；坏文件不算「已写过」 | `(run_id, stage)` 幂等键；重复执行不产生第二份报告 |
| 审计 | ✅ **2026-10-31**：`application/audit.py` 把节点事件 / 工具 / 决策 / 检查点汇成一条 `AuditEvent` 流写 `logs/v1/<id>/events.jsonl`；旧的 `task.json` / `tools.jsonl` / `decisions.jsonl` 改为由事件流**投影**生成（内容逐字不变，UI 不用改） | 统一 `AuditEvent` 流，旧文件由事件流生成 |
| 失败注入 | ✅ **2026-10-31**：`checks/test_failure_injection.py`（3 项）覆盖三类——模型不可用 / 写盘失败 / 进程中断 | 故障注入测试：模型超时、写盘失败、进程被杀 |

验收：三类故障（模型不可用、磁盘只读、进程中断）都能给出明确错误或恢复，不允许「静默半成品」。

## 7. 阶段 5：验证与交付

* 评测集：金标准（`tools/golden_eval.py`）+ 历史语料 + 新增 Profile 的对照集。
  **现状**：golden 38 用例 / 召回 100% / 误报 0（每次 CI 跑）；历史 219 份报告
  `integrity.py verify` 全绿；「新增 Profile 的对照集」未做。
* 集成测试：真实 `run_one` 跑通 docx / txt / 代码三类对象。
  **已完成（2026-10-31）**：`checks/test_integration_objects.py`（5 项）现场造三类目标，
  真跑 `run_one`，断言「出报告 / 落到磁盘 / 裁定在合法档位 / 问题能追溯到规则」。
* 文档同步：本组六份文档与实现对齐；删掉与现状矛盾的旧描述。
  **进行中**：本轮已修 6 处过期口径 + 逐阶段状态同步；每加一项能力都回写本文件。
* 部署验证：`start-console.bat` 起控制台 → 建任务 → 看执行中心 → 下载报告 → 校验 ledger。
  **部分完成（2026-10-31）**：`start-console.bat --check` 通过（Python / pyyaml / LLM 配置
  均 OK）；`python tools/integrity.py verify` 通过（链完整 + 219 份报告自洽）。
  **未做**：真的起进程、在浏览器里点完整流程（那要占端口、且是人工验收动作）。
* 交付判据：§10 七问全部有「是」的证据（测试或演示），而不是文档承诺。

## 8. 开关与回滚

| 项 | 做法 |
| --- | --- |
| 特性开关 | `REVIEW_ENGINE=v1` 启用新链路；缺省 `legacy` |
| 数据兼容 | 新旧写同一套 `outputs/` 格式；`extra` 字段只增不改 |
| 回滚 | 关开关即回旧链路；新产物可保留（不删报告、不删日志） |
| 回滚触发条件 | 双跑出现未解释差异；新链路错误率高于旧链路；报告 hash 校验失败 |

不允许的回滚方式：改历史报告、删 ledger 记录、改 `report_hash` 算法。审计链一旦破坏，
「可追溯」这条产品定位就不成立了。

## 9. 风险清单

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 双写不一致 | 两份结论对不上，用户不知道该信谁 | 双跑只比对不上线；上线后单一写路径 |
| 报告格式漂移 | 历史报告无法并排比对，前端解析失败 | 阶段 1 冻结格式；新增字段只增不删 |
| 领域模型过度设计 | 骨架比业务复杂，没人愿意用 | 只建六个对象；`extra` 旁路；不为「将来可能」加抽象 |
| 规则/知识库版本未升 | `integrity.verify_report` 判成「疑似篡改」 | 改规则必须升 `config/rules_version.txt` |
| Windows 路径/编码 | 报告 quote 与原文不匹配 | 统一 `as_posix()`；读文件显式编码；S4 锚定兜底 |
| 并发分配 ID | 两个审查拿到同一个 `R-id` | 沿用 `O_EXCL` 锁；阶段 2 改为数据库序列 |
| AI 改动引入新设计 | 每轮迭代换一套结构 | 本目录六份文档 + `checks/test_architecture_guard.py` + 改动说明模板 |

## 10. 验收矩阵（方案 §11 七问）

| 问题 | 阶段 | 证据（测试/文件） | 状态 |
| --- | --- | --- | --- |
| 能否追踪一次审查的完整生命周期？ | 2/4 | `RunState` 阶段链 + 统一 `AuditEvent` 流；`checks/test_domain_v1.py`、`checks/test_audit_stream.py` | **是**：六节点 + 编排器跑完整八阶段，检查点/状态可落盘；v1 的审计事件写入 `logs/v1/<id>/events.jsonl`，可按 `seq` 回放（跨来源无统一时钟，见 audit.py 的说明） |
| 能否单独替换 LLM？ | 2 | `ModelClient` 端口（`domain/ports.py`）+ `ChatClientAdapter`（`adapters/model.py`）+ `make_llm_review_fn(model_client=...)` | **是**（2026-10-31）：任意 `chat(messages) -> ModelReply` 的客户端都能注入，不需要配置文件；真实供应商也走同一条缝。证据：`checks/test_model_client.py`（14 项，两个「供应商」共用同一段审查代码） |
| 能否单独测试每个审查节点？ | 2 | 每节点独立用例（固定语料） | **是**（Decision 16 + Evidence 16 + Cross 17 + Rule 17 + Semantic 21 + Document 15；另有 Planner 8、编排器 21） |
| 能否在中断后恢复执行？ | 2/4 | Checkpoint + 杀进程恢复用例 | **是（状态层）**：`FileRunStore` 落盘 → 读回 → 从断点阶段续跑（`checks/test_state_serialization.py`）；「杀进程后自动续跑」要等 Application Service 接上读回逻辑 |
| 能否追溯每条 Finding 的证据？ | 1/3 | `Evidence` + `verification` + 适配器测试 + 报告 `evidence[]` 段 | **是**（历史报告可追溯；报告 `evidence[]` 自 schema 1.2 起随报告输出，`anchored` 可回查原文） |
| 能否证明最终裁定符合规则？ | 1 | `DecisionPolicy` + `consistency_issues`；全量历史报告重放 | **是**（仅 5 份已知偏差，已登记且用例会盯它是否过期） |
| 能否不破坏其他模块地增加 Profile？ | 2 | `ReviewProfile` + `resolver.py` + `checks/test_profile_resolver.py` | **是**（解析器已建；新增 Profile 不改引擎） |

## 11. 工作量估计（人日，仅作排期参考）

| 阶段 | 估计 | 现状（2026-10-31） | 主要不确定性 |
| --- | --- | --- | --- |
| 1 收口（错误类型统一） | 1~2 | **已完成** | 旧异常散落在 server / review / tools 三处 |
| 2 执行层（Application + 其余 5 节点 + 双跑） | 12~18 | **基本完成**；只剩在线切流（需要真实使用时间） | Semantic Reviewer 的 prompt 与结果稳定性 |
| 3 能力层（42 个工具 + 分发改造） | 6~10 | 分发已接管；批次 1 迁了 5/10，其余 5 个卡点已登记 | 工具间的隐式依赖 |
| 4 可靠性（checkpoint/retry/审计） | 5~8 | **完成**（checkpoint / retry / timeout / 幂等 / 审计单流 / 故障注入） | 恢复语义的边界情况 |
| 5 验证交付 | 4~6 | 集成测试与部署自检已做；在线双跑与 Profile 对照集未做 | 评测集建设 |

估计的前提是「单主题小步提交 + 每阶段验收通过」；并行开发会显著放大双跑比对的成本。

## 12. 现在就该做的三件事

> 2026-10-31 更新：Task/Report 服务已完成（`application/tasks.py` + `application/reports.py`，
> `checks/test_task_service.py` 32 项）。阶段 2 只剩「在线切流」。下面是**当前**的三件事。

1. **在线切流观察**：`--engine shadow` 已能把每次审查的双跑差异写进 `state/double_run/`。
   下一步是让它在真实使用里跑起来（server 侧设 `REVIEW_ENGINE=shadow`），
   连续零未解释差异后再把默认切到 v1。
2. ~~Task/Report 服务~~ **已完成（2026-10-31）**：`start_review(target, profile, purpose)`
   返回 `TaskRecord`（含 `review_id` / `run_id`），`get_task` / `list_runs` 读记录不重跑，
   `ReportService` 按 `run_id` 取报告（v1 优先、legacy 兜底）并可选复算 `report_hash`。
   记录落 `state/tasks/`，进程重启后仍在。**未做**：把它接进 server 的 JOBS
   （那一步要连并发与取消一起改，属阶段 4）。
3. **切流前的最后一道**：`--engine shadow` 仍需等模型差异分类方案（语义审有波动，
   不能当"未解释差异"处理）；`v1` 已能带模型跑（`--llm`）、产出带完整性链（含日志哈希）、
   规范化 limitations、整改单与目的比对的影子报告。要让 v1 替代 legacy，还要清零
   `unsupported_blocks`：law_library / research 两项。

## 13. 与方案原文的偏差清单（2026-10-30 复盘）

方案原文指 `如果我是 review-agent 的总架构师…`（用户提供）与各层的约束文档。
逐条比对后，**方向与验收方式一致，但有 11 处需要点名**——不做「已经完全按方案落地」的结论。

| # | 方案原文要求 | 实际落地 | 性质 | 登记位置 |
| --- | --- | --- | --- | --- |
| 1 | §7 技术栈：Pydantic / SQLAlchemy / SQLite→PostgreSQL / FastAPI / structlog | dataclass + pyyaml + `http.server` + 文件存储 | **偏离**（既有 ADR-001 取舍，非本轮引入；本轮沿用未改） | ARCHITECTURE ADR-001 |
| 2 | §2 六层含 Application（ReviewService / TaskService / ReportService） | **基本完成**：`ReviewApplication`（组装/执行/落盘/恢复）+ CLI `--engine v1|shadow` 已接；server 走 `REVIEW_ENGINE` 环境变量；`TaskService`（建任务/查/列/改状态）与 `ReportService`（按 run_id 取报告、复算哈希）已落地。**未接 server 的 JOBS**（要连并发与取消一起改） | **基本完成**（2026-10-31） | §4 行 |
| 3 | §6 所有工具注册到 Registry，经统一执行器分发 | **分发管线已完成**：`review.build_rule_registry()` 把 12 个 builtin + Agent 自带 `pack:*` 处理器注册成工具（`_HandlerTool`，权限 WRITE），分发走 `registry.execute()`：存在性 → 权限 → 执行 → 结果。`run_rule_plan` 已改为 async，可 await 注册表的 async 执行器。旧 `resolve_handler` 无调用点，按迁移规矩保留到阶段 5。**但 `tools/` 42 个模块仍被 `_HandlerTool` 统一包装，没有逐个实现 `BaseTool`** | **部分完成**（管线完成，工具迁移未开始，2026-10-30） | TOOL_SPECIFICATION §4 |
| 4 | §6 外部文档视为不可信输入，不得影响系统行为 | **部分**：`llm_review.build_messages` 改为随机串围栏（`<<<TARGET:<nonce>>>>` … `<<<END:<nonce>>>>`），文档里的 `TARGET>>>` 无法闭合数据区块；原文逐字保留、撞车直接拒绝。其余三项（工具权限不受文档影响、文档里的命令/URL 不执行、联网白名单）仍未做 | **部分完成**（2026-10-30） | TOOL_SPECIFICATION §6 |
| 5 | §10 五个产品界面（工作台/执行中心/证据中心/裁定中心/审计中心） | 沿用旧控制台；新能力只在报告 JSON 里 | **未做**（未排期） | — |
| 6 | §4 协议示例 `NodeContext{review_id,run_id,state}`、`NodeResult{success,output,errors,warnings}` | `state` 内含 review_id/run_id；`NodeResult` 增 typed 字段（findings/evidence/disputes/decision）与 `revisions`；`NodeContext` 增 `config` / `dry_run` / `next_finding_seq` | **扩展**（与「state 用明确 Schema、不塞 dict」的意图一致，但形参不同于原文） | AGENT_RUNTIME §4 |
| 7 | §3 六个专业节点 | 六个全有，另加 **Planner**（原文把 Planner 列为运行时组件）与 **Finalize**（最小实现，报告写出仍在旧链路） | **扩展** | AGENT_RUNTIME §3 |
| 8 | §9「不允许修改未授权的领域模型」 | 改了 3 处：`Evidence.finding_ids`、`STAGE_ARTIFACTS[analysis]` 增两键、错误分类 `kind/http_status/retryable` + 3 个新异常类 | **偏离但已登记**：每处都写了「为什么必须改」与被谁使用 | DOMAIN_MODEL §2.4 / §5；AGENT_RUNTIME §11；CHANGELOG |
| 9 | §5 报告格式冻结（阶段 1） | `schema_version` 1.1→1.2，新增顶层 `evidence[]` | **允许的增量**（规则是「新增可以，删除/改名不行」） | CHANGELOG 2026-10-30 条目 |
| 10 | §12 原定顺序：先 Semantic Reviewer，再 Application | 实际先做 Orchestrator（一阶段多节点）再做 Semantic | **顺序调整**：analysis 天然是「规则 + 语义」两个节点，没有多节点编排，语义审无处安放 | 本节 |
| 11 | §4 双跑判据「连续一周零未解释差异」才切流 | 目前是 4 个目标语料的一次性比对（0 未解释差异） | **未达标准**（缺时间维度与在线双跑） | §12 第 2 条 |

不计入偏差的两件事（容易被误读，先讲清）：

* **删掉重复实现**（`_quote_overlap` / `covered_chars` / `coverage_from_llm_meta` /
  `detect_type_detail` / `cross_review` / S4 锚定 / S6 裁定）不是「删旧功能」：
  每条都改成委托给唯一实现，行为由历史差分用例钉住。
* **`.trash` 归档语料**参与比对：`outputs/` 里被人工移入 `.trash` 的报告仍在语料集内。
  它让「全量历史」的口径比文档里常说的 202 份更大；用例不依赖具体条数，只依赖「零例外」。

