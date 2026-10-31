# 变更记录（CHANGELOG）

本文件是**逐条登记**的改动账本：每一次新增 / 修改 / 删除都记一条，
不看 `git diff` 也能知道「什么时候、动了哪个文件、改了什么、为什么、怎么验的」。

- **倒序**：最新在最上面。
- **粒度**：一次改动一条，不按版本合并。
- **与 README 的分工**：README 讲「为什么这样设计」（可读的长文，按主题成章）；
  本文件讲「谁在什么时候动了哪一处」（可查的流水）。
- **与 `.workbuddy/memory/` 的分工**：memory 是给下一次开工的备忘（坑、口径、教训）；
  本文件是给人看的账本。

---

## 登记约定

每条记录固定四行，字段不许省：

```md
### YYYY-MM-DD · 修改 · 一句话标题

- **文件**：`path/a.py`（+ `tests/x.py`、`README.md` §n）
- **改了什么**：删了/加了/换了什么，写到能照着复现的程度
- **为什么**：触发它的是哪个现象、哪个用户反馈、哪个缺陷
- **验证**：跑了什么、看到什么数（测试项数 / 实测数值 / 截图核对）
```

类型只用四种：

| 类型 | 含义 |
|---|---|
| **新增** | 新文件、新接口、新功能、新规则 |
| **修改** | 改了已有行为（含重构、调参、文案） |
| **删除** | 删文件、删接口、删功能（**删了要写清为什么不留着共存**） |
| **修复** | 修缺陷（要写清根因，不只是"修好了"） |

三条硬要求：

1. **删改必须写"为什么"**——半年后看"删了 X"最想问的就是这句。
2. **验证要带数**——"测试通过"不算，"405 项 OK / 偏移 0px"才算。
3. **改了必须登记，登记了必须是真的**——账本失真比没有账本更糟。

---

# 2026-10-31

### 2026-10-31 · 新增 · 阶段 5 起步：三类对象集成测试 + 部署自检

- **文件**：`checks/test_integration_objects.py`（新增 5 项）、`docs/REFACTOR_PLAN.md` §1/§7/§10/§11
- **改了什么**：
  1. 集成用例现场造三类目标（txt / `.py` 代码 / 用 `zipfile` 手写的最小 docx），用**真实** `run_one` 跑，断言「出报告、报告落盘、裁定在合法档位、问题能追溯到规则」。
  2. txt 用例盯的是确定性信号：分项 360+210+480=1050 与「加总 1080」的矛盾必须命中 `LOGIC-001`，且证据里能读到 1050——这是「结论不依赖模型」的可执行证据。
  3. 部署自检：`start-console.bat --check` 通过（Python / pyyaml / LLM 配置）；`python tools/integrity.py verify` 通过（链完整 + **219 份**报告自洽）。
- **为什么**：单元测试证明「每个零件是对的」，集成测试才证明「拼起来能出结论」。阶段 5 的验收要求「全量 checks + golden + 双跑报告」，前两项现在都有可复现证据。
- **验证**：`python -m unittest discover -s checks` → **476 项 OK**（+5）；`tools/integrity.py verify` exit 0 / 219 份 OK / 0 失败。
- **未做（不装完成）**：真的起控制台进程、在浏览器里走完「建任务 → 看执行中心 → 下载报告」（要占端口，且是人工验收动作）；「在线双跑切流」需要真实使用时间，不是写代码能提前完成的。

### 2026-10-31 · 修改 · CI 修复：公开 clone 无私有资产时优雅跳过（26 用例 + golden + tests 步骤）

- **文件**：`checks/test_engine_switch.py`、`checks/test_v1_integrity.py`、`checks/test_pipeline_parity.py`、`checks/test_llm_review_fn.py`、`checks/test_report_evidence.py`、`checks/test_rule_dispatch.py`、`checks/test_shadow_report.py`、`checks/test_holdout.py`（8 个文件共 26 个用例加守卫）、`tools/golden_eval.py`、`.github/workflows/ci.yml`
- **改了什么**：
  1. 26 个依赖私有资产的端到端用例加 `skipUnless` 守卫：`targets/` 样本或 `agents/` Agent 定义缺失时优雅跳过（措辞与 `test_failure_injection.py` 既有 `HAS_AGENTS` 约定一致），本地带资产时照常运行。
  2. `tools/golden_eval.py`：`agents/` 缺失时输出「跳过金标准评测」并以 0 退出，不再因 `未知 Agent：doc-review` 崩溃。
  3. `ci.yml` Full regression 步骤：判断条件由 `Test-Path tests` 改为「存在 `test*.py` 才跑」——`tests/` 目录会因金标准数据随仓库走而在场，但公开 checkout 里没有测试实现，`unittest discover -s tests` 无测试时在 Python 3.12+ 以退出码 5 失败。
- **为什么**：GitHub Actions CI 两个 job（3.12 / 3.13）全部失败（各 21s、2 条 annotation）——`checks/` 里 26 个用例依赖被 `.gitignore` 排除的私有资产（`targets/` 对抗样本 127 个文件、`agents/` Agent 定义、`state/` 链状态），干净 clone 报「未知 Agent：doc-review；可选：(无)」「找不到待审文件 targets/contract_001.md」；golden 评测同因崩溃；tests 步骤误触发 exit 5。`ci.yml` 注释本意是「checks/ 是可公开的最小不变量集，必须绿」，但无数据环境从未实现优雅跳过，等于守门从未在 CI 上真正生效。
- **验证**：本地（有资产）`python -m unittest discover -s checks` → **471 项 OK**（26 用例照常运行，无误跳过）；干净 clone 模拟 CI 全链路 → **checks 471 项 OK（skipped=37）**、golden 步骤 exit 0、tests 步骤明确输出跳过、changelog 守门在干净树通过。
- **未做**：`targets/`、`agents/` 仍按「不公开」规则只留本地，不随仓库走；跳过不等于在这些环境失去保护——本地跑全量回归的强度不变。

### 2026-10-31 · 新增 · 阶段 4：统一审计事件流（`events.jsonl` + 旧格式投影）

- **文件**：`review_agent/application/audit.py`（新增）、`checks/test_audit_stream.py`（新增 14 项）、`review.py`（`_write_v1_logs` 改走事件流）、`review_agent/application/__init__.py`（导出 `build_audit_stream` / `load_audit_stream` / `audit_to_legacy` / `write_audit_stream`）
- **改了什么**：
  1. 新增 `AuditEvent` 有序事件流：节点事件 / 工具调用 / 决策 / 检查点四个来源汇成一条 `events.jsonl`，`seq` 从 1 连续编号。
  2. `_write_v1_logs` 改为先 `build_audit_stream` 写 `events.jsonl`，再由 `audit_to_legacy` 投影生成旧的 `task.json` / `tools.jsonl` / `decisions.jsonl`——格式逐字不变，UI 与历史脚本照旧读，消费者不用改。
  3. 事实来源只剩一份，跨文件顺序问题（如「第 7 步之前调了哪个工具」）从此有唯一答案。
- **为什么**：旧实现把「发生了什么」记在三处字段各异的文件里，审计中心只能按文件分别展示，回答不了跨来源的先后关系；`checkpoints` 还只留在内存，进程重启即丢。切流不能顺带改旧文件格式——那是发布动作，所以用投影保格式。
- **验证**：`python -m unittest discover -s checks` → **471 项 OK**（+14）；新用例钉住汇流顺序连续、投影与旧形状逐字一致、jsonl 往返与坏行跳过、`_write_v1_logs` 同时产出事件流与旧三件（含目录哈希）。
- **已知边界**：跨来源没有统一时钟，`seq` 是「来源优先级 + 来源内原始顺序」，不是墙钟顺序；要真实先后看 `ts`（没有时间的记 `None`，不用「现在」冒充）。

### 2026-10-31 · 新增 · 阶段 4：报告写入幂等（`run_id` + finalize）

- **文件**：`review.py`（`_report_already_written` + `_write_shadow_report` 提前返回）、`checks/test_idempotency.py`（新增 4 项）、`docs/REFACTOR_PLAN.md` §1/§6
- **改了什么**：
  1. `_write_shadow_report` 在写报告/写日志/追加链之前先查 `outputs/v1/<review_id>.json` 是否已是**同一个 `run_id`**；是就直接返回该路径，不重写报告、不重写 `logs/v1/<id>/`、不往 `ledger/v1-chain.log` 追加第二条记录。
  2. 幂等键选 `(run_id, finalize)` 而不是内容哈希：报告里含 `audit.latency_ms` 与 `integrity.logs_hash` 这类每次都会变的东西，按内容比对永远不相等，等于没做幂等。
  3. 读不出来的坏文件不算「已写过」——宁可重写一份，也不拿坏文件当既成事实。
- **为什么**：中断恢复会重跑最后一个阶段。没有幂等的话，`outputs/v1/` 会被重写（读者手里的那份和一小时后下载的不是同一份），审计链也会多出一条同结论记录，链就不再能回答「到底跑了几次」。
- **验证**：`python -m unittest discover -s checks` → **457 项 OK**（+4）；用例覆盖「同 run 两次只写一份且链上 1 条」「换一次 run 就该有新记录且链可校验」「坏文件会被重写」「缺 `run_id` 的旧文件不算已写过」。
- **未做**：任务层（`TaskService.start_review`）仍然一次调用一个 review_id，同目标重复提交会得到两个 R-id——那属于「请求级幂等」，与报告写入幂等不是同一层，没有混在一起做。

### 2026-10-31 · 新增 · 阶段 4：三类故障注入（模型不可用 / 写盘失败 / 进程中断）

- **文件**：`checks/test_failure_injection.py`（新增 3 项）、`docs/REFACTOR_PLAN.md` §1/§6
- **改了什么**：按阶段 4 的验收原话「三类故障都要给出明确错误或恢复，不允许静默半成品」各打一发：
  1. **模型不可用**：注入一个必抛的语义审执行器 → 运行必须失败、`state.decision is None`、`DECISION`/`FINALIZE` 不在 `completed_stages`，错误串里能读到「模型不可用」。
  2. **写盘失败**：把 `outputs/` 占成同名文件制造 `NotADirectoryError` → `_write_shadow_report` 抛错，且事后 `R-*.json` **一个都没有**（半成品比没有更危险：它看起来像结论）。
  3. **进程中断**：任务记录落成 `running` 时抛 `KeyboardInterrupt` → 新构造的服务实例仍能 `list_runs()` 看到它、状态是 `failed` 且带原因，并可用 `mark(ABORTED)` 收敛。
- **为什么**：阶段 4 的六项里，「失败注入」是唯一能证明前五项真的生效的手段——不注入故障，「不会留半成品」只是文档里的承诺。
- **验证**：`python -m unittest discover -s checks` → **453 项 OK**（+3）；故障从**注入点**进（执行器 / 目录 / 重新构造服务），没有 mock 掉被测对象本身。
- **未做**：真正的「杀进程」演练（`subprocess` + `taskkill`）没做，当前用「记录已落 running 后抛中断」等价模拟；要在 CI 上稳定跑还需要先解决进程清理的跨平台差异。

### 2026-10-31 · 新增 · 阶段 3：`rules.pattern` 工具 + `pattern_rules.scan_hits` 纯核心（批次 1 达 5/10）

- **文件**：`tools/pattern_rules.py`（新增 `scan_hits`，`scan` 改为委托）、`review_agent/capabilities/builtin_tools.py`（`PatternScanTool`）、`review_agent/capabilities/__init__.py`、`review.py`（注入 `pattern_rules.scan_hits`）、`checks/test_builtin_tools.py`（+8 项 → 36 项）、`docs/TOOL_SPECIFICATION.md` §7、`docs/REFACTOR_PLAN.md` §1
- **改了什么**：
  1. `pattern_rules.scan_hits(text, rules, ...)`：把匹配语义（pattern / require / guard / unless / 注释行去噪 / 文档字符串去噪 / 维度筛选）抽成**纯核心**，返回 `(参与匹配的规则数, [命中])`。
  2. 旧 `pattern_rules.scan(s, ...)` 改为委托 `scan_hits()`，只负责把命中写进 Session。**判定条件仍然只有一份**——两处一旦分叉，「流水线说有命中、工具说没有」就没法解释。
  3. `rules.pattern`（权限 ANALYZE）工具：入参 `{text, rules, dimension?, skip_comments?, target_suffix?}`，出参 `{findings, count, rules_examined}`，标 `untrusted=True`。
  4. 工具边界校验：没有带 `pattern` 的规则 → 失败；带 pattern 但缺 `rule_id` / `dimension` → 失败（否则 `scan_hits` 会在写 issue 时 `KeyError`）。
- **为什么**：`scan()` 的第一个参数是 Session，工具不该造一个假 Session 去跑真实逻辑——那等于让「独立工具」继续依赖旧流程的内部形状。
- **验证**：`checks` → **450 项 OK**（+8）；其中 `test_matches_the_legacy_scan_exactly` 是**差分用例**：同一份文本、同一批规则，工具结果与 `pattern_rules.scan()` 写进假 Session 的 issue 逐字段一致（rule_id / quote / location / evidence）。`tests/` 642 项 OK。
- **实测发现**：当前 `rules/` 的 33 条规则**没有一条**自带 `pattern`，所以这个工具在真实规则集上会明确报「没有带 pattern 的规则」。用例 `test_real_ruleset_is_handled_honestly` 按这个现状写，规则集将来加了声明式规则它就自动变成正向断言。

### 2026-10-31 · 新增 · 阶段 3：`rules.classify` / `integrity.hash` 两个独立工具（批次 1 达 4/10）

- **文件**：`review_agent/capabilities/builtin_tools.py`、`review.py`（`build_capability_registry` 注入 `rule_exec` / `integrity`）、`checks/test_builtin_tools.py`（+11 项 → 28 项）、`docs/TOOL_SPECIFICATION.md` §7、`docs/REFACTOR_PLAN.md` §1
- **改了什么**：
  1. `rules.classify`（权限 **READ**）：入参 `{rules:[规则定义]}`，出参 `{summary, counts, prompt_only}`。回答「这条规则是代码算的 / 声明式跑的 / 只出现在提示词里」——`prompt_only` 的结论跨次可能漂移，报告里必须能分辨。
  2. `integrity.hash`（权限 **READ**）：入参 `{text}`，出参 `{sha256, size}`（UTF-8 字节）。用于证据指纹、引用去重、跨报告判断「是不是同一段原文」。
  3. 规则里缺 `rule_id` 的条目在工具边界就被拒绝（`summarize` 会 `KeyError`，不能让它变成运行时崩溃）；`rules` 为空返回 `ok=False`（没有可分类的规则 ≠ 全部合格）。
  4. 两个工具权限都按**最小**给 `READ`：只查表 / 只算哈希，不读材料，不该要求 `ANALYZE`。权限收紧比放宽难。
- **为什么**：批次 1 的验收是「注册表里有它 + `review.py` 通过注册表调用 + `checks/` 有测试」。这两个模块本来就纯（`rule_exec` 三个函数零 IO，`integrity.sha256_bytes` 零 IO），属于「卡点最少、收益最直接」的两个——先把纯的迁完，再动需要改旧代码的那批。
- **验证**：`python -m unittest checks.test_builtin_tools` → 28 项 OK；规则分类用 `review.load_rules()` 的**真实规则集**跑，断言「每条规则落进且只落进一个执行方式」；哈希用例对照 `hashlib` 独立复算（含空串与中文）。
- **未做**：批次 1 还剩 6 个，卡点已逐条写进 TOOL_SPECIFICATION §7（`text.regex` 缺位置映射、`rules.pattern` 要先抽纯核心、`law.cite` 要先做索引注入…），不是「忘了」。

### 2026-10-31 · 新增 · 阶段 4 起步：单节点超时（`Orchestrator(timeout=...)`）

- **文件**：`review_agent/runtime/orchestrator.py`、`review_agent/application/service.py`、`checks/test_orchestrator.py`（+6 项）、`docs/AGENT_RUNTIME.md` §7、`docs/REFACTOR_PLAN.md` §1/§6
- **改了什么**：
  1. `Orchestrator` 增加 `timeout: float | None`：每个节点的一次尝试用 `asyncio.wait_for` 包住，超时记为 `NodeResult.fail(retryable=True)`——**模型慢 / IO 抖动可重试**。
  2. 默认 `None`（不限制），由入口显式打开：迁移期不改变既有行为（既有 21 项编排器用例仍按原样通过）。
  3. `ReviewApplication(node_timeout=...)` 透传到编排器。
  4. `timeout <= 0` 在构造期直接 `ValueError`，不留到运行时变成「每个节点都立刻超时」。
- **为什么**：阶段 4 的六项目前只有 Retry/Checkpoint 有骨架。整次审查超时（server 的 `--review-timeout`）只能回答「跑太久了」，节点级超时才能回答「卡在哪个节点的第几次尝试」——恢复与排障靠的都是后者。
- **为什么不顺手把默认值打开**：AGENT_RUNTIME §7 的建议值（ingestion 60s / analysis 单块 120s…）还没用真实 `logs/*/cost.json` 校准。先有旋钮、再有数据、最后定默认，顺序反了就会变成「凭感觉设一个数字，然后把正常慢请求判成失败」。
- **验证**：`python -m unittest discover -s checks` → **431 项 OK**（新增 6）；超时用例覆盖「超时即失败并指出节点名」「超时会重试到 max_attempts」「默认不限制」「快节点不受影响」「开了超时不会让确定性失败变成可重试」「非法值构造期报错」。
- **已知边界**：超时只能打断 `await` 得住的节点。节点内部若做同步阻塞调用（同步读大文件），`asyncio.wait_for` 抢占不了——那类慢只能靠进程级超时兜底，已写进 AGENT_RUNTIME §7。

### 2026-10-31 · 新增 · 阶段 3 起步：批次 1 工具落地为独立 `BaseTool`（`math.check` / `logic.check`）

- **文件**：`review_agent/capabilities/builtin_tools.py`（新增）、`review_agent/capabilities/__init__.py`、`review_agent/capabilities/tools.py`（管线顺序的 docstring 更正）、`review.py`（`build_capability_registry`）、`checks/test_builtin_tools.py`（新增 17 项）、`docs/TOOL_SPECIFICATION.md` §7、`docs/REFACTOR_PLAN.md` §1
- **改了什么**：
  1. `MathCheckTool`（`math.check`）与 `LogicCheckTool`（`logic.check`）实现为 `BaseTool` 子类：`arguments_type = TextCheckArgs{text, rule_ids}`，权限 `ANALYZE`，输出 `{findings, count}` 并标 `untrusted=True`（命中的 quote 逐字来自待审文档）。
  2. **算法本体不复制**：`capabilities` 层不许 import `tools/`（ARCHITECTURE §3 第 3 条，架构守卫会扫），所以由 `review.build_capability_registry()` 注入 `arith_check.run_all` / `logic_checks.run_all`。同一份实现，两种调用方式。
  3. `rule_ids` 的三态语义在工具层钉死：`None` = 跑全部、`()` = 一条都不跑、列表 = 只跑这些。把 `[]` 归一成 `None` 会让「本次只启用 0 条规则」变成「跑全量」。
  4. 空文本返回 `ok=False`：「审了个空文件」不许显示成 `pass`。
  5.  更正 `ToolRegistry` 的管线 docstring：实际顺序是「存在性 → 权限 → schema → 执行」，旧注释写的是「schema → 权限」，与代码相反。
- **为什么**：阶段 3 的验收是「注册表里有它、`review.py` 通过注册表调用、`checks/` 有测试」。此前 12 个 builtin 处理器只是被 `_HandlerTool` 统一包了一层——分发走了注册表，但工具本身仍**依赖 Session**，脱离规则流水线就用不了，也不是「不知道谁在调它」的工具。
- **验证**：`python -m unittest discover -s checks` → **425 项 OK**（新增 17）；`checks.test_builtin_tools` 用真算法跑真文本（分项 360+210+480=1050 对「加总 1080」；30 岁 / 从业 25 年），不 mock。
- **未做（不装完成）**：批次 1 还有 8 个没迁，逐个理由写进 TOOL_SPECIFICATION §7（`text.lint` 有文件 IO、`rules.pattern` 首参是 Session、`docx.format` / `law.cite` / `matrix.match` 依赖批次 2 的索引与解析）。

### 2026-10-31 · 新增 · 阶段 2 收尾：TaskService / ReportService

- **文件**：`review_agent/application/tasks.py`（新增）、`review_agent/application/reports.py`（新增）、`review_agent/application/__init__.py`、`review.py`（`make_task_runner` / `make_task_service` / `make_report_service` / `_allocate_task_review_id` / `allocate_review_id` 增加 `state/tasks/` 扫描）、`checks/test_task_service.py`（新增 32 项）、`docs/REFACTOR_PLAN.md`、`docs/ARCHITECTURE.md`、`docs/AGENT_RUNTIME.md`
- **改了什么**：
  1. `TaskService`：`start_review(target, profile, purpose) -> TaskRecord`（含 `review_id` / `run_id`）、`get_task`、`list_runs`、`mark`。记录落 `state/tasks/<review_id>.json`，**执行前先写 `running`**，失败也留痕（`status=failed` + 错误串）后原样抛出。执行由注入的 `runner` 完成，本层不 import `review.py` / `tools/`。
  2. `ReportService`：`locate` / `get_report` / `get_report_with_source` / `verify` / `list_reports`。来源顺序 `v1 → legacy`，并把 `source` 一起返回；哈希校验函数是注入的，未注入时 `verify()` 返回 `None`（**没验证 ≠ 通过**）。
  3. `review.make_task_service()` / `make_report_service()` 负责装配；`TaskService` 的 `profile_id` 直接作为 Agent 选择器（v1 的 `ReviewProfile` 由 agent 派生）。
  4. `allocate_review_id` 增加扫描 `state/tasks/`：任务先落盘、执行前失败时，下一个任务不会拿到同一个 R-id 覆盖记录。
- **为什么**：阶段 2 的「入口」缺口。此前「跑一次」只存在于 CLI 的一次函数调用里，server 的 `JOBS` 是内存字典，进程重启后「那次跑完没有」无从判断；「报告在哪」由每个入口各拼一遍路径（CLI `outputs/`、v1 `outputs/v1/`、server 再一份）。
- **修复**（写实现时暴露的真实缺陷）：`ReviewIdAllocator` 端口约定入参是 `datetime.date`，而旧 `allocate_review_id` 吃 `"YYYYMMDD"` 字符串。第一版直接透传，产出 `R-2026-10-31-001` 这类非法 ID（`format_run_id` 当场拒绝）。适配收在 `_allocate_task_review_id` 一处，不把日期格式化扩散出去。
- **验证**：
  - `python -m unittest discover -s checks` → **408 项 OK**（新增 32）
  - `python -m unittest checks.test_task_service -v` → 32 项 OK，其中 2 项是**真 Agent / 真规则**的端到端（`start_review → get_report → verify=True`、两个任务拿到不同 R-id）
  - 用例跑完 `state/tasks/` 仍为空（目录重定向到临时目录），不污染仓库
- **未做**：接进 server 的 `JOBS`（要连并发、取消、超时一起改，属阶段 4）；幂等去重（同目标重复提交仍会得到两个 review_id）。

# 2026-10-30

### 2026-10-30 · 修改 · 修正 6 处过期文档口径（ARCHITECTURE / AGENT_RUNTIME / REFACTOR_PLAN / CLI 帮助）

- **文件**：`docs/ARCHITECTURE.md`（状态行、§3 六层表、§4 约束测试口径、§9 七问）、`docs/AGENT_RUNTIME.md`（§3 引擎开关）、`docs/REFACTOR_PLAN.md`（§1 阶段表、§11 工作量表、§13 偏差 #3）、`review.py`（`--help` 的 `--engine` 说明）
- **改了什么**：
  1. `ARCHITECTURE` §3 把 Application「未建」、Runtime「调度未建」、Capabilities「迁移未开始」改为实况（均已落地，仅 `tools/` 未逐个迁移）。
  2. `ARCHITECTURE` §9 七问从「未完成（阶段 2）」改为「是 / 部分」并逐条给出验收用例名。
  3. `ARCHITECTURE` §4 说明第 4 条反向依赖已由同一张 `FORBIDDEN` 表覆盖，第 5 条等基础设施层落地再测。
  4. `AGENT_RUNTIME` §3 修掉「`v1`/`shadow` 强制 `--no-llm`」：实况是 `v1` 可带 `--llm`，只有 `shadow` 要求 `--no-llm`。
  5. `REFACTOR_PLAN` §1 阶段 2 改「Application Service 与切流未做」→ 已落地、剩 Task/Report 与在线切流；阶段 3 由「未开始」改「进行中」并写清管线已完成、工具未迁移。
  6. `REFACTOR_PLAN` §11 工作量表加「现状」列；§13 偏差 #3 由「完成」降为「部分完成」。
  7. `review.py` 的 `--engine` 帮助文案同步（`v1` 写影子报告并可带 `--llm`；`shadow` 需 `--no-llm`）。
- **为什么**：文档与代码冲突时以代码为准（`docs/README.md` §维护规则），这 6 处已经长期并存，会让下一个接手的人（或 AI）照着过期状态继续做。典型后果：以为 Application 未建而重复实现一遍，或以为 `v1` 不支持 LLM 而绕开它。
- **验证**：`python -m unittest discover -s checks` → 376 项 OK（改后重跑）；`python tools/changelog_guard.py` → exit 0。

### 2026-10-30 · 修改 · 版本号升至 0.9.2，归档 Architecture/Runtime/Engine v1

- **文件**：`VERSION`、`README.md`、`.gitignore`、`docs/README.md`、`CHANGELOG.md`
- **改了什么**：
  1. `VERSION` 0.9.1 → 0.9.2，README 标题与「当前实现」同步（`checks/test_invariants.py` 要求标题含版本号，漏改即红）。
  2. `.gitignore` 新增 `docs/系统诊断报告*.md`：该文含 P0/P1 缺陷清单与漏洞细节，按本仓库「测试/漏洞报告不公开」的既定规则只留本地。
  3. `docs/README.md` 注明该诊断报告不在公开仓库，属预期而非链接失效。
- **为什么**：阶段 0「冻结与盘点」要求固定当前版本和 Git Tag。此前 0.9.0/0.9.1 两个 tag 与 3 个提交都只在本地，远端 `origin/main` 停在 `0f1cbaa`（10-27）。不升版本直接打 tag，会出现「tag 叫 0.9.2、VERSION 写 0.9.1」的自相矛盾，违反 `tools/version.py` 的「版本号唯一事实源」约定。诊断报告本身也把「公开仓库仍跟踪内部报告」列为 P0-3，这次一并执行。
- **验证**：`python -m unittest discover -s checks` → 376 项 OK；`python -m unittest discover -s tests` → 642 项 OK（221s）；`python tools/golden_eval.py` → 召回 100% / 误报 0；`python tools/changelog_guard.py` → exit 0。
- **影响面**：报告 `model_version` 由 `v0.9.1-demo` 变为 `v0.9.2-demo`；历史报告的 `report_hash` 自包含 `model_version`，**复算仍通过**，不需要重算。

### 2026-10-30 · 新增 · 影子报告接入目的比对（`purpose`）

- **文件**：`review.py`（`_attach_purpose`、`_run_v1` / `_run_shadow` 传 `purpose_info`）、`review_agent/application/shadow_report.py`（`purpose` 字段、Markdown 目标段、`unsupported_blocks` 减到 2 项）、`checks/test_shadow_report.py`
- **改了什么**：
  1. 影子报告新增 `purpose`：复用 `purpose_mod.analyze()`（同一套意图词典、同一套证据定位规则），带 `intents` / `goals`（含 `question` / `status` / `evidence_ids`）/ `evidence` / `matrix` / `judge`。
  2. `judge=None`：v1 暂不做模型语义裁决（要额外一次调用），但确定性部分完整——目标定位、覆盖、未找到说明。
  3. 未找到的目标写进 limitations（**未找到 ≠ 不存在**），Markdown 增加「审查目的比对」段。
  4. `unsupported_blocks` 从 3 项减到 **2 项**（law_library / research）。
- **为什么**：`--purpose` 是产品的核心入口之一（"按你的目的，看到了什么"）。旧报告把目的比对放在 S5.5、依赖 Session；v1 一直缺这块，用户填了目的也看不到对照结果。
- **验证**：
  - `python -m unittest discover -s checks` → **376 项 OK**（新增 2）
  - 端到端：`--purpose "这是给客户的合同，找出对我方不利的条款"` → 报告 `purpose.goals` 两条（要件齐全 found、违约责任对等 not_found），4 条原文证据
  - `python -m unittest discover -s tests` → **642 项 OK / 262s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0

### 2026-10-30 · 新增 · 影子报告接入整改单（`action_plan`）

- **文件**：`review.py`（`_attach_action_plan`、写入前挂载）、`review_agent/application/shadow_report.py`（`action_plan` 字段、Markdown 整改段、`unsupported_blocks` 减到 3 项）、`checks/test_shadow_report.py`
- **改了什么**：
  1. 影子报告新增 `action_plan`：复用 `remediation.attach()`（同一套规则表与改写模板，避免两套口径），每条发现挂 `fix`（`action` / `where` / `from` / `to` / `how` / `consequence` / `has_rewrite`），计划只列 blocker/major 并给出 `concrete` / `manual` / `headline`。
  2. **挂在 `report_hash` 之前**：完整性哈希必须覆盖整改建议本身，否则"改了改写建议再重算结论"就无从发现。
  3. Markdown 增加「整改建议」段：怎么改 + 不改会怎样。
  4. `unsupported_blocks` 从 4 项减到 **3 项**（purpose / law_library / research）。
- **为什么**：只给"建议"的报告把改写工作留给用户。整改单回答的是「改哪一句 / 改成什么 / 不改会怎样」——这是审查工具从"指出问题"到"可执行"的分界。
- **验证**：
  - `python -m unittest discover -s checks` → **374 项 OK**（新增 3：整改单形状 / Markdown 渲染 / v1 报告含 fix）
  - 端到端：`contract_001.md` 的 v1 报告 `action_plan.total=1, concrete=1`，`headline="1 条可直接改，0 条需人工改写"`，issue 带完整 `fix`
  - `python -m unittest discover -s tests` → **642 项 OK / 249s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0

### 2026-10-30 · 修复 · Windows 并发审查偶发 `PermissionError: .chain.lock`

- **文件**：`review.py`（`_acquire_ledger_lock` 增加 PermissionError 分支、抽出 `_wait_for_ledger_lock`）
- **改了什么**：拿台账写锁时，除了 `FileExistsError` 也捕获 `PermissionError`——Windows 上「锁文件已存在且被另一进程打开」时，`os.open(..., O_EXCL)` 抛的是 `PermissionError`（实测 6 路并发必现）。原实现直接把它抛给调用方，整次审查失败。两处细节：① 抽出 `_wait_for_ledger_lock()`（清超龄锁 / 等 10ms / 超时报可操作错误），两条分支共用；② **不要**先判断「锁文件还在不在」再决定是否重试——持有者可能在两次系统调用之间就删了锁，这个检查会误判成「目录不可写」而直接抛错（第一版就这么写的，8 次压测里复现 1 次）。
- **为什么**：这是 `tests/test_bruteforce_regressions.py::test_chain_survives_six_concurrent_reviews` 在全量回归里偶发失败的原因（6 路并发 + 系统负载）。锁争用是**正常现象**，不该表现为异常；链完整性由台账校验兜底，锁只需要保证「同一时刻只有一个写者」。
- **验证**：`LedgerConcurrencyTests` 连跑 **10 次 0 失败**（修复前约 1/8 失败）；`python -m unittest discover -s checks` → **372 项 OK**；全量回归见下条。

### 2026-10-30 · 新增 · v1 日志与限制口径：`logs_hash` 补齐 + limitations 规范化

- **文件**：`review.py`（`_write_v1_logs`、`_write_shadow_report` 接收 `session`、`_run_v1`/`_run_shadow` 接线）、`review_agent/application/shadow_report.py`（`review_mode`、limitations 分类、`integrity` 块内置）、`checks/test_v1_integrity.py`、`checks/test_shadow_report.py`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. **v1 日志**：写 `logs/v1/<review_id>/{decisions.jsonl, tools.jsonl, task.json}`，并对目录取哈希进 `integrity.logs_hash`。之前报告只能证明"结论没被改过"，不能证明"过程也没被改过"。
  2. **limitations 规范化**：按固定顺序给——未支持块 → 模式边界（纯规则审）→ 文本覆盖（语义审覆盖率 / 失败块）→ 证据丢弃条数 → 日志缺失（仅当没写）→ 节点告警。旧实现是散在 `run_one` 里按场景拼，读者很难判断"边界到底在哪"。合并完成后 `unsupported_blocks` 从 5 项减到 4 项（`coverage.limitations` 已退出清单）。
  3. 影子报告新增 `review_mode`（rule-only / rule+llm），`integrity` 块改由 builder 统一生成（`_write_shadow_report` 只负责补 `prev_report_hash` 与 `report_hash`）。
- **为什么**：完整性链如果只覆盖结论、不覆盖过程，等于"可以改过程再重算结论哈希"——审计价值减半。limitations 同理：写得散，读者就只看到结论、看不到"没查什么"。
- **验证**：
  - `python -m unittest discover -s checks` → **372 项 OK**（新增 1；完整性用例扩到 6 项）
  - 新用例：`logs_hash` 非空且等于目录哈希、**改 `task.json` 后目录哈希变化**（过程可校验）、三件日志文件都存在
  - `python -m unittest discover -s tests` → **642 项 OK / 233s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0

### 2026-10-30 · 新增 · v1 影子报告接入完整性链（`report_hash` + `ledger/v1-chain.log`）

- **文件**：`review.py`（`_integrity_inputs`、`_write_shadow_report` 印章与链、`run_typed_pipeline` 增 `integrity_out`、`_run_v1` / `_run_shadow` 接线）、`review_agent/application/shadow_report.py`（移除 `integrity.chain` 未支持项，补 `logs_hash` 说明）、`review_agent/application/__init__.py`、`checks/test_v1_integrity.py`（新增）、`checks/test_shadow_report.py`
- **改了什么**：
  1. 影子报告带完整指纹：`target_hash` / `ruleset_hash` / `rules_dir_hash` / `rules_version` / `agent_dir_hash` / `agent_version` / `knowledge_hash` / `knowledge_version`，与旧报告同一批 `integrity.*` 函数（同一口径）。
  2. `report_hash` 用同一套 `compute_report_hash` 计算，可由报告文件本身复算；改任何一个字段都会对不上。
  3. **独立成链** `ledger/v1-chain.log`：序号连续、`prev_hash` 相接、`verify_ledger` 通过；不写官方 `ledger/chain.log`（v1 报告在 `outputs/v1/`，混进官方链会让 `integrity verify` 找不到对应报告）。
  4. `unsupported_blocks` 从 6 项减为 5 项（哈希链已接入）；`logs_hash` 为空单独写进 limitations——v1 还没写决策/工具日志。
- **为什么**：报告是交付物，"结论可被篡改而无人察觉"比"缺 purpose 块"严重得多。这是切流清单里优先级最高的一项。
- **验证**：
  - `python -m unittest discover -s checks` → **371 项 OK**（新增 6：完整性 5 + 报告块 1）
  - 用例覆盖：`report_hash` 可复算、**篡改 verdict 后哈希失配**（可证伪）、两次 v1 后链 seq=1/2 且 prev 接得上、官方 `chain.log` 未被创建、指纹字段齐全且 `logs_hash` 为空
  - `python -m unittest discover -s tests` → **642 项 OK / 233s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
  - 手工冒烟：`--engine v1 --no-llm` → `outputs/v1/R-20261030-024.json`；`integrity.verify_ledger('ledger/v1-chain.log')` → 校验通过，1 条记录

### 2026-10-30 · 新增 · 影子报告：v1 的交付物（`outputs/v1/`）

- **文件**：`review_agent/application/shadow_report.py`（新增）、`review_agent/application/__init__.py`、`review.py`（`_FinalizeNode` 改用 `build_shadow_report`、`_write_shadow_report`、`_run_v1`/`_run_shadow` 落盘）、`checks/test_shadow_report.py`（新增）、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. 新增影子报告 `schema_version=1.0-v1-shadow`：`engine` / `target` / `verdict` / `decision` / `stats` / `by_origin` / `issues` / `evidence` / `disputes` / `coverage` / `limitations` / `unsupported_blocks` / `audit`（节点事件 + 检查点 + 阶段）。
  2. **`unsupported_blocks` 显式列出旧报告有、v1 还没有的 6 项**：purpose、law_library、research、action_plan、integrity.chain、limitations 合并口径。这张清单就是切流验收单——清零之前 v1 不能替代 legacy。
  3. 落 `outputs/v1/<review_id>.json/.md`，**不占正式报告命名空间**；`--engine v1` 与 `--engine shadow` 都会写（双跑时差异能对着「新链路自己怎么说」看）。
  4. Markdown 渲染刻意「先讲边界、再列条目」：读者先知道这份报告没做什么，再看发现了什么。
- **为什么**：v1 到上一轮为止有完整结论却没有交付物。直接照抄旧报告格式等于伪造没算过的块（目的 / 法条 / 库外核验 / 整改单 / 哈希链）——"没算就不写、但要明说"是唯一诚实的做法。
- **验证**：
  - `python -m unittest discover -s checks` → **365 项 OK**（新增 10）
  - 影子报告用例覆盖：形状与身份、`unsupported_blocks` 与常量一致、limitations 含未支持项与节点告警、**被丢弃的发现不混进 issues**、证据与发现对得上、审计带事件/检查点、target_meta 透传、Markdown 含裁定/边界/条目、空结果如实说明
  - CLI 用例：`--engine v1` 写出 `outputs/v1/` 的 json+md，且 `outputs/` 下**没有**正式报告
  - `python -m unittest discover -s tests` → **642 项 OK / 241s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
  - 手工冒烟：`--engine v1 --no-llm` → `outputs/v1/R-20261030-022.json`（6 项未支持块、7 条节点事件、7 个检查点）

### 2026-10-30 · 新增 · 语义审接真模型：`--engine v1 --llm` 跑通

- **文件**：`review.py`（`make_llm_review_fn`、`run_typed_pipeline` 增 `use_llm` / `llm_meta_out` / `purpose_info`、`_run_v1` 降级提示、`allocate_review_id` 纳入 `state/runs`）、`checks/test_llm_review_fn.py`（新增）、`checks/test_engine_switch.py`（更新 + 新增 1 项）、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. **接线**：`make_llm_review_fn()` 把 `load_config` / `LLMClient` / `llm_review.review_dimension` 接起来，口径与旧 `semantic_review` 一致——按维度取生效规则、任务书并入用户目的、事实库进 prompt、quote 对**全文**校验。
  2. **三态语义**：`use_llm=False` 纯规则；`None` 能配就配、配不上降级并**打印原因**；`True` 配不上直接抛 `ConfigError`。显式要求过模型却静默降级，是这次特意堵掉的口子。
  3. **CLI**：`--engine v1 --llm` 可跑；`--engine shadow` 仍只允许 `--no-llm`（模型有波动，双跑比对无法判定差异性质）。
  4. **修一个真实 bug**：`allocate_review_id` 原来只看 `outputs/` + ledger，v1 不写报告 → 第二次 v1 会拿到同一个 R-id 并覆盖上一份状态。现在把 `state/runs/*.state.json` 也算进已用 ID，并有用例钉住「两次 v1 产生两份状态」。
- **为什么**：语义审的执行器、入库规则、节点都就位了，只差"一次真实调用"这根线。接上之后 `v1` 才是完整流水线（规则 + 模型），也才有资格谈切流。
- **验证**：
  - `python -m unittest discover -s checks` → **355 项 OK**（新增 8：接线 6 + 引擎 2）
  - 真机一次：`--engine v1 --llm` → `model=deepseek-chat（ui）`，流水线 `completed`，2 条发现（1 规则 + 1 模型）
  - mock 用例覆盖：配置不可用返回原因、配置加载异常不吞、可用时按维度传规则且 `full_text` 传全文、`use_llm=True` 不可用抛 `ConfigError`、`None` 降级但回填 meta、可用时 `semantic_stats` 进入 analysis 产物
  - `python -m unittest discover -s tests` → **642 项 OK / 240s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
- **注意**：真机运行会消耗模型 token；`checks/test_engine_switch.py` 已把 LLM 配置显式 mock 成不可用，避免测试误发请求。

### 2026-10-30 · 新增 · 语义审入队：候选入库规则脱离 Session

- **文件**：`review_agent/text.py`（新增：文本工具上移）、`review_agent/runtime/nodes/text.py`（改为转发）、`review_agent/capabilities/semantic_ingest.py`（新增）、`review_agent/capabilities/__init__.py`、`review.py`（`sentence_no` 委托、`make_semantic_executor`、`run_typed_pipeline` 增 `semantic_review_fn`）、`checks/test_semantic_ingest.py`（新增）、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. **文本工具上移**：`normalize_quote` / `quote_overlap` / `same_clause` / `sentence_no` / `sentence_key` 移到包根 `review_agent/text.py`。原因很实际：能力层（入库去重）也要用，但架构约束禁止 capabilities → runtime，放在任何一层都会逼着另一层反向依赖。
  2. **入库规则**：`ingest_candidates()` 把模型候选变成 `Finding`，四条判断逐条与旧实现等价——去重（同规则 + 重合 ≥0.8 或首句指纹相同）、语境豁免（确定性判据优先，进 `excluded` 不静默丢）、严重度下调（blocker/major + 置信度 <0.7 降一级并记 `severity_note`）、编号（从调用方给的序号起）。
  3. **适配层**：`review.make_semantic_executor()` 把「一次模型调用」接到入库规则上，模型调用经 `review_fn` 注入——离线可测，不需要网络与 Key；`run_typed_pipeline(..., semantic_review_fn=…)` 即可把语义审挂进 analysis 阶段。
- **为什么**：语义审一直卡在两处 Session 耦合（`make_issue`、`context_exempt`）。把入库判断抽成纯函数后，「模型给什么、最后进报告什么」第一次可以离线钉住；这也让 analysis 阶段的第二个节点真正可插。
- **验证**：
  - `python -m unittest discover -s checks` → **347 项 OK**（新增 17：去重 4 / 豁免 2 / 降级 4 / 形状 4 / 适配层 3）
  - 端到端（离线）：`contract_001.md` + 假 `review_fn` → 流水线产出 1 条规则发现 + 1 条模型发现（`ISS-0002`，origin=model），全流程 `completed`
  - `python -m unittest discover -s tests` → **642 项 OK / 370s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
- **未做**：真实 `llm_review.review_dimension` 的客户端接线（分块 / 缓存 / 配置）与 `--engine v1 --llm`。`shadow` 仍需等模型差异分类方案，不能直接比。

### 2026-10-30 · 新增 · 引擎开关（`--engine legacy|v1|shadow`）：CLI 接入 Application

- **文件**：`review.py`（`_take_engine` / `_run_v1` / `_run_shadow`、usage 文案、main 接入）、`checks/test_engine_switch.py`（新增）、`docs/REFACTOR_PLAN.md`、`docs/AGENT_RUNTIME.md`
- **改了什么**：
  1. `--engine` 命令行开关 + `REVIEW_ENGINE` 环境变量（server 拉起子进程时自动继承），缺省 `legacy`；**非法值直接报错退出**，不静默回退——用户以为跑了新引擎、实际还是旧链路是最坏的结果。
  2. `--engine v1`：只跑六节点流水线，状态落到 `state/runs/`，**不写正式报告**（还没接语义审与报告写出，写一份同名报告等于给用户看「看起来一样、实际少一半」的东西）。
  3. `--engine shadow`：旧链路照出正式报告，同时跑一遍新流水线，差异写到 `state/double_run/<review_id>.json`（含 `ok` / `unexplained` / `missing` / 折叠条目）。
  4. `v1` / `shadow` 强制要求 `--no-llm`：语义审未接入，开着 LLM 会产出「看起来审了模型」的影子结论，宁可拒绝。
- **为什么**：切流前需要「能在真实使用里持续跑双跑」的能力。之前双跑只活在测试里（`compare_with_report`），参数、落点、失败语义都没有出口；现在它是 CLI 上的一等公民，且不会污染正式报告。
- **验证**：
  - `python -m unittest discover -s checks` → **330 项 OK**（新增 9：开关解析 6 + 三种模式 3）
  - 进程内跑三种模式：`v1` 无报告、有状态文件；`shadow` 有报告 + 差异文件且 `diff.ok=True`；不带 `--no-llm` 的 `v1` 退出码 2 且不产出报告
  - `python -m unittest discover -s tests` → **642 项 OK / 282s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
  - 手工冒烟：`--engine shadow` 在 `contract_001.md` 上输出「双跑一致：verdict=warn，提及折叠 0 条」，差异文件 `state/double_run/R-20261030-017.json`

### 2026-10-30 · 修改 · 偏差清单 #3：规则分发改由 Tool Registry 接管

- **文件**：`review_agent/runtime/nodes/rules.py`（`run_rule_plan` 改 async）、`review.py`（`_HandlerTool` / `build_rule_registry` / `rule_executor_from` / `_tool_name`；`run_agent_steps` 与 `run_typed_pipeline` 改走注册表）、`checks/test_rule_dispatch.py`（新增）、`checks/test_rule_analyzer_node.py`（改用例异步化 + 新增 awaitable 用例）、`docs/TOOL_SPECIFICATION.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. **接口契约**：`run_rule_plan` 从同步改为 async，执行器允许返回 awaitable——否则注册表（async 的 `BaseTool.execute`）永远接不进来，只能另开一条同步通道。
  2. **注册与分发**：`_HandlerTool` 把 12 个 `builtin:*` 处理器与 Agent 自带 `pack:*` 包成工具（权限 WRITE），`rule_executor_from` 把一步计划变成一次 `registry.execute()`：存在性 → 权限 → 执行 → 结果。处理器键的冒号在工具名里换成点（`builtin:fact` → `builtin.fact`）。
  3. **权限语义**：调用方传 `actor="pipeline"` 并显式授予 WRITE——审查流程自己在写发现，不是「模型能自己开写权限」。
  4. **错误映射**：注册表抛 `ToolError`（未注册 / 缺权限）→ `agent_spec.AgentError`，保持旧语义（配置问题走退出码 2，而不是运行时崩溃）。
  5. 旧 `resolve_handler` 已无调用点，按 AGENT_RUNTIME §10.3 的迁移规矩**保留到阶段 5**，并在 docstring 里写明「已被注册表取代、不删的原因」。
- **为什么**：旧分发直接 `resolve_handler(key)(...)`，绕过了工具层的存在性校验、权限判定与执行管线；md §6 要求「所有工具注册到 Registry，经统一执行器分发」。上一轮判断的卡点是对的：不先改 async/sync 契约，只能做半截接管。
- **验证**：
  - `python -m unittest discover -s checks` → **321 项 OK**（新增 9：分发 8 + async 执行器 1）
  - 分发用例覆盖：工具名合法且权限 WRITE、维度上下文透传、incodable issue 不进 findings 但写决策日志、未知处理器 → AgentError、缺 WRITE 拒绝执行、注册表包含全部 builtin
  - `python -m unittest discover -s tests` → **642 项 OK / 266s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
  - 双路径冒烟：`adversarial_meeting_001.txt` 旧链路 → fail / 9 条；新链路 → completed / 10 条（多出的一条仍是被旧流程折叠的 FACT-004）；双跑比对 4 目标未解释差异 0

### 2026-10-30 · 新增 · 偏差清单 #2/#4：Application Service + 不可信输入围栏

- **文件**：`review_agent/application/__init__.py` + `service.py`（新增）、`review.py`（`run_typed_pipeline` 改调 Application、新增 `store` 参数）、`tools/llm_review.py`（随机围栏、`PROMPT_VERSION` 升版）、`checks/test_application_service.py`（新增）、`checks/test_untrusted_fence.py`（新增）、`docs/TOOL_SPECIFICATION.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. **Application 层**（偏差清单 #2）：`ReviewApplication` 承担组装（六节点 + 可选语义节点）、执行（Orchestrator）、落盘（`FileRunStore`）、恢复（`resume(run_id)` 从断点阶段继续）；`run_typed_pipeline()` 改为调用它并支持 `store=` 落盘。此前这段逻辑散在 `review.py` 里，CLI/server/测试各接一遍就会各写一份恢复语义。
  2. **不可信输入围栏**（偏差清单 #4）：`llm_review.build_messages` 用**带随机串**的围栏包住原文（`<<<TARGET:<nonce>>>>` … `<<<END:<nonce>>>>`）。旧实现是固定 `<<<TARGET … TARGET>>>`，文档里写一行 `TARGET>>>` 就能提前闭合数据区块、把后续文字顶进提示词语境。新实现：原文**逐字保留**（quote 仍能回查原文）、围栏串与原文撞车直接拒绝、`PROMPT_VERSION` 升到 `2026-10-30a` 让旧缓存失效。
- **为什么**：这两项是 §13 偏差清单里「原文明确要求但没做」的条目。Application 是「入口统一」的前提（否则每加一个入口复制一遍保存/恢复逻辑）；随机围栏是「材料作者不得通过审查系统反向影响结论」的最低实现——不做的话，一句 `TARGET>>>` 就能越过数据边界。
- **验证**：
  - `python -m unittest discover -s checks` → **312 项 OK**（新增 18：Application 9 + 围栏 9）
  - Application 用例覆盖：节点顺序、语义节点可插拔、初始状态在 intake 之后、跑完落盘且可读回、**失败后按 run_id 恢复并跑完**、已完成的 run 再次 resume 不重跑
  - 围栏用例覆盖：prompt 里只有一对围栏、原文逐字在围栏内、文档里写 `TARGET>>>`/`<<<TARGET` 不增加围栏、每次 nonce 不同、显式 nonce 可用、nonce 撞车拒绝、系统提示词已说明随机围栏、既有提示词约束未丢
  - `python -m unittest discover -s tests` → **642 项 OK / 302s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
  - 端到端：`run_typed_pipeline(..., store=tmp)` 落盘 `R-20261030-950-RUN01.state.json`，状态可读回
- **本轮未做**：偏差清单 #3（Tool Registry 接管分发）。卡点是 `BaseTool.execute` 为 async、规则执行器为 sync；真正接管要先改 `RuleExecutor` 契约为可 await，否则只能做「查表 + 权限」半截改造。已写进 §12 第 2 条。

### 2026-10-30 · 修改 · 复盘：与方案原文的偏差清单 + 修正过期验收数字

- **文件**：`docs/REFACTOR_PLAN.md`（新增 §13 偏差清单、修正 §4/§10 的用例数与状态）、`docs/AGENT_RUNTIME.md`（19→21 项、207 份→全量历史）、`docs/DOMAIN_MODEL.md`（207/1015→全量历史语料）
- **改了什么**：
  1. 新增 §13「与方案原文的偏差清单」：11 条逐项列出（技术栈未按原文、Application 层未建、Tool Registry 未接管分发、不可信输入未做、五界面未做、节点协议形参不同、多出 Planner/Finalize、领域模型改了 3 处、报告 schema 1.2、实施顺序调整、双跑未达「连续一周」）。
  2. 修正过期数字：六节点用例数按 `unittest` 实际计数改为 16/16/17/17/21/15（另有 Planner 8、编排器 21）；「报告 `evidence` 段待落地」改为已落地（schema 1.2）；「能否单独替换 LLM」从「未开始」改为「部分（节点层可注入执行器）」。
  3. 明确两件容易被误读的事：删重复实现不是删功能（都是委托 + 差分用例）；`.trash` 归档报告也在比对语料内，所以「全量历史」比文档常说的 202 份更大。
- **为什么**：验收矩阵里的数字是给人判断进度用的，写死就会过期；过期数字比没有数字更糟。复盘同时把 3 项「原文要求但没做」的事显式挂出来，避免下一轮再把它们当成已完成。
- **验证**：逐模块 `python -m unittest checks.<module>` 计数（decision 16 / evidence 16 / cross 17 / rule 17 / semantic 21 / document 15 / planner 8 / orchestrator 21 / serialization 9 / parity 2）；`python -m unittest discover -s checks` → **294 项 OK**。

### 2026-10-30 · 新增 · 阶段 2 第八块：Planner 节点 + 影子流水线（六节点双跑）

- **文件**：`review_agent/runtime/nodes/planner.py`（新增）、`review_agent/runtime/node.py`（`surviving_findings`）、`review_agent/runtime/nodes/cross_review.py` / `decision.py`（改用 `surviving_findings`）、`review_agent/runtime/__init__.py`、`review.py`（`run_typed_pipeline` / `compare_with_report` / `_FinalizeNode`）、`checks/test_planner_node.py`（新增）、`checks/test_pipeline_parity.py`（新增）、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. **Planner 节点**：planning 阶段把「这次承诺审什么」落成 `plan` 产物（`dimensions` / `handlers` / `checks` / `local_checks`）。没有维度直接失败；没有任何本地检查时给警告（纯规则模式下等于没审）。
  2. **`surviving_findings()`**：evidence 阶段判为不可回溯的发现（`evidence_dropped`）不再参与交叉复核、裁定与报告骨架。状态只增（发现不可变），所以「移出报告」表现为后续阶段按清单排除，而不是从 `state.findings` 删掉。
  3. **影子流水线** `run_typed_pipeline()`：Document → Planner → Rule → Evidence → Cross → Decision → Finalize 七节点串起来跑一次 rule-only 审查，返回 `(RunState, RunOutcome, Session)`；读失败抛 `UnreadableTargetError`，与 `run_one` 同语义。语义审不在其中——硬接会改变行为。
  4. **双跑比对** `compare_with_report()`：新链路保留下来的每条发现必须对上旧报告的 `issues` 或 `suppressed`；旧报告的每条 issue 必须在新链路里存在；verdict 必须一致。
- **为什么**：节点齐了、编排器也能跑，但缺一份「装配起来跑一次、并证明与旧链路同结论」的证据。双跑是 REFACTOR_PLAN 阶段 2 的硬判据，也是切流前的唯一刹车。顺带补上 Planner——旧实现没有「计划」这个东西，审什么维度全靠现算，事后无法核对「当时承诺审什么」。
- **验证**：
  - `python -m unittest discover -s checks` → **294 项 OK**（新增 10 项：Planner 8 + 双跑 2，双跑覆盖 4 个目标语料）
  - 双跑结果：`answer_001.txt` / `contract_001.md` / `adversarial_meeting_001.txt` / `code_001.py` 四个目标，未解释差异 **0**；唯一差异是 FACT-004 被旧流程折叠进 `suppressed`（比对器把它算作「已解释」）
  - `python -m unittest discover -s tests` → **642 项 OK / 285s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
- **边界**：影子流水线只跑 rule-only；语义审（去重、语境豁免、`make_issue`）仍是 Session 语义，接进新链路是阶段 3 的第一件事。报告写出（哈希链、ledger、Markdown）仍由 `run_one` 负责，`_FinalizeNode` 只产报告骨架。

### 2026-10-30 · 新增 · 阶段 2 第七块：Document Analyzer 节点 + RunState 序列化

- **文件**：`review_agent/runtime/nodes/document.py`（新增）、`review_agent/runtime/serialization.py`（新增）、`review_agent/runtime/nodes/__init__.py`、`review_agent/runtime/__init__.py`、`review.py`（`detect_type_detail` 改为委托、删掉重复的 `CONTRACT_MARKERS` 与判定实现）、`checks/test_document_node.py`（新增）、`checks/test_state_serialization.py`（新增）、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. `DocumentAnalyzerNode`：读文本 + 判类型 + 记元数据（`target_text` / `target_meta`），读文件经 `reader` 端口注入。类型判定 `classify_target()` 成为唯一实现——旧流程的 `detect_type_detail` 改为委托，`CONTRACT_MARKERS` 与那段 40 行判定删除（不再两份）。
  2. `state_to_dict()` / `state_from_dict()` / `FileRunStore`：RunState 全量往返（六个对象 + 产物 + 警告 + attempt）。约定写死：枚举存 `.value`、时间存 ISO、tuple↔list、`artifacts` 必须可 JSON 化（混进 `Path` 直接报错，不许写出读不回来的状态）；`FileRunStore.save()` 用临时文件 + `replace` 原子写。
  3. `checks/test_state_serialization.py` 覆盖「存盘 → 进程重启 → 读回 → 从断点阶段继续」，前置阶段节点零重跑。
- **为什么**：`Document Analyzer` 是六节点的最后一块，且类型判定直接决定「用哪套判定标准」，猜错一次就换掉整份报告的口径——这种规则必须只有一份实现、有边界用例。状态序列化则是「中断后恢复执行」从「能演示」到「能交付」的分水岭：检查点只记阶段与哈希，进程一重启状态就没了。
- **验证**：
  - `python -m unittest discover -s checks` → **284 项 OK**（新增 24 项：文档节点 15 + 序列化 9）
  - `python -m unittest discover -s tests` → **642 项 OK / 256s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
  - 端到端冒烟：`targets/contract_001.md` 走旧链路出报告，裁定与异议数不变（warn / 1 条）
- **仍未接入 `run_one`**：六节点齐了，但旧主流程还在跑 S1~S6。接入点是 Application Service（下一步）。

### 2026-10-30 · 新增 · 阶段 2 第六块：Semantic Reviewer 节点 + Run 级编号分配器

- **文件**：`review_agent/runtime/nodes/semantic.py`（新增）、`review_agent/runtime/node.py`（`NodeContext.next_finding_seq`）、`review_agent/runtime/orchestrator.py`（编号发放 `next_finding_seq()`）、`review_agent/runtime/nodes/__init__.py`、`review_agent/domain/transitions.py`（analysis 白名单增 `semantic_stats`）、`review.py`（`covered_chars` / `coverage_from_llm_meta` 改为委托）、`checks/test_semantic_node.py`（新增）、`checks/test_orchestrator.py`、`checks/test_rule_analyzer_node.py`、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. 新增 `SemanticReviewerNode`：维度×块执行计划（`plan_semantic_steps`，顺序固定）、成功块区间并集覆盖率（`build_meta` + `coverage_from_meta`）、失败块记账；模型调用经 `SemanticExecutor` 端口注入（参数是 step + 起始编号），prompt 与去重/Dedupe 仍留在能力层。
  2. **Run 级编号分配器**：`NodeContext.next_finding_seq`；编排器按「已有最大号 + 1」发放并在同阶段逐节点接续。规则审与语义审同处 analysis，以前各从 ISS-0001 起号会被 `merge_results()` 直接拦下——现在不会了。
  3. 覆盖率推导去重：`review.coverage_from_llm_meta` / `covered_chars` 改为委托节点实现（`review_agent.runtime.nodes.semantic`），节点与旧流程用同一份算法，不会出现「节点说审了 80%、报告说 100%」。
  4. `STAGE_ARTIFACTS[analysis]` 白名单增加 `semantic_stats`；`advance(required=)` 已按「本阶段节点 produces 并集」判定必需产物，所以只挂规则审的编排不会被要求提供语义审产物（既有用例已改为显式传 `required`）。
- **为什么**：语义审是六节点里唯一必须用 LLM 的，直接接管整个函数风险最高。但它里面真正容易出错、又完全可测的三件事——跑哪些维度×块、覆盖了多少字、哪些块失败了——可以现在就接管。旧实现把失败块 `continue` 掉后仍报「全覆盖」，202 份历史报告里 `complete=False` 一次都没出现过，就是这三件事没被单独测试的代价。
- **验证**：
  - `python -m unittest discover -s checks` → **260 项 OK**（新增 23 项：语义节点 21 + 编号 2）
  - `python -m unittest discover -s tests` → **642 项 OK / 226s**
  - 覆盖率回归：同一目标 `targets/adversarial_meeting_001.txt` 改造前后 `text_coverage_pct` / `chars_reviewed` / `complete` / `chunks_*` 全部一致（100 / 1780 / True / 0 / 0），9 条异议与裁定不变
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
- **未接入 `run_one`**：语义审的入队（去重、语境豁免、`make_issue`）仍是旧实现里的 Session 语义，等阶段 3 的能力层整理与 Application Service 一起搬。节点负责的那三件事已经可以被未来链路复用。

### 2026-10-30 · 新增 · 阶段 2 第五块：Orchestrator（一阶段多节点 + 检查点）

- **文件**：`review_agent/runtime/orchestrator.py`（新增）、`review_agent/runtime/node.py`（`Workflow` 多节点、`merge_results`、`stage_requirements`、`advance(required=)`）、`review_agent/runtime/__init__.py`、`checks/test_orchestrator.py`（新增）、`checks/test_domain_v1.py`（契约更新）、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. `Workflow` 允许同一阶段挂多个节点（必须连续出现、名字唯一、阶段只能前进）。`stages` 改为「按出现顺序去重」——原来的实现会把同阶段节点的阶段名重复列出。
  2. `merge_results()`：把同阶段多个节点的结果合成一个 `NodeResult` 再推进。三条硬约束：产物键唯一（两个节点都写 `analysis_stats` 直接失败，不许「谁覆盖谁看声明顺序」）、发现编号唯一、一个阶段最多一个裁定。
  3. `advance(..., required=)`：必需产物从「`STAGE_ARTIFACTS` 全集」改为「本阶段节点 `produces` 的并集」。否则只挂 Rule Analyzer 的 analysis 阶段会被要求提供语义审才有的产物。`STAGE_ARTIFACTS` 仍是**允许写入**的白名单，不因部分编排放宽。
  4. `Orchestrator`：按阶段推进、同阶段多节点顺序执行、失败停在原地、只有 `retryable` 才重试、未编排的阶段返回 `incomplete` 而不是悄悄跳过；每推进一个阶段写一条 `Checkpoint`（`MemoryCheckpointStore` / `FileCheckpointStore`），`state_hash` 只摘「会让结论变化」的字段。
  5. 契约测试同步更新：`test_workflow_rejects_duplicate_stage_and_wrong_order` → `test_workflow_allows_many_nodes_per_stage_but_rejects_wrong_order`（同一阶段多节点从「非法」变为「合法且有顺序」）。
- **为什么**：节点写完了却没有装配线，等于零件摊在地上；而且 analysis 天然是两件事——规则审（确定性）与语义审（模型），合并成一个节点后「哪一半没跑成」就说不清。编排器把「谁先跑、失败停不停、从哪恢复、检查点写了什么」变成可测的代码，而不是散在一个 900 行函数里。
- **验证**：
  - `python -m unittest discover -s checks` → **237 项 OK**（新增 19 项）
  - 覆盖：完整八阶段流水线、同阶段两节点按序执行且只推进一次、失败停在阶段入口（检查点数 = 已完成阶段数）、可重试失败重试 2 次、不可重试不重试、未覆盖阶段报 incomplete、从中断的 `RunState` 继续跑不重跑已完成阶段、`FileCheckpointStore` 往返与坏行跳过、`state_hash` 可复现且随发现变化
  - `python -m unittest discover -s tests` → **642 项 OK / 223s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
- **未接入 `run_one`**：编排器目前只被测试使用，旧主流程仍是 S1~S6。接入的前置是 Application Service 与剩余两个节点（Semantic Reviewer / Document Analyzer），以及 **Run 级发现编号分配器**——规则审与语义审同处 analysis，两个节点各从 ISS-0001 起号会被 `merge_results()` 直接拦住（已登记为 AGENT_RUNTIME §11 开放问题）。

### 2026-10-30 · 新增 · 报告新增 evidence[] 段（schema_version 1.2）

- **文件**：`review_agent/runtime/nodes/evidence.py`（`evidence_to_report`）、`review_agent/runtime/nodes/__init__.py`、`review.py`（S4 保留节点产出、报告顶层 `evidence`、`schema_version` 1.1→1.2）、`checks/test_report_evidence.py`（新增）、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. 报告顶层新增 `evidence[]`，每条 `{"evidence_id", "issue_id", "kind", "quote", "location", "verification", "verified_by"}`。`verification` 只会是 `anchored`（逐字命中原文）或 `placeholder`（占位式引用，缺失型问题）——不可回溯的条目在 S4 就被移出报告，所以这一段不会出现 `unanchored`。
  2. `schema_version` 1.1 → 1.2（按报告契约：新增顶层字段必须改版本号）。既有字段一个没动，`issues[]` 结构不变。
  3. 证据来自 S4 节点产出（`_anchor.evidence`），被丢弃的条目不在其中，所以不会出现指向已删结论的悬空证据。
- **为什么**：`issues[].quote` 只回答「我引了哪一句」，不回答「这句核过没有」。产品定位是「证据驱动」，报告里必须有可核对的 `verification`——这正是「能否追溯每条 Finding 的证据」从领域对象走到用户眼前的一步。
- **验证**：
  - `python -m unittest discover -s checks` → **218 项 OK**（新增 5 项：证据段形状 + 一次真实审查的端到端核对）
  - 端到端：`targets/adversarial_meeting_001.txt` 跑出 9 条异议 ↔ 9 条证据，全部 `anchored` 且 `quote` 能在 `target_text` 里回查到；`issue_id` 与 `issues[].issue_id` 一一对应
  - `python -m unittest discover -s tests` → **642 项 OK / 223s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0

### 2026-10-30 · 新增 · 阶段 1 收口：错误分类（kind / http_status）+ CLI→server 结构化错误行

- **文件**：`review_agent/domain/errors.py`、`review_agent/domain/__init__.py`、`review.py`（`UnreadableTargetError` 改继承、`_emit_error` / `_emit_domain_error`、main 四个失败出口）、`server.py`（`_parse_review_error` / `_strip_error_marker`、任务载荷增 `error_kind`）、`checks/test_error_taxonomy.py`（新增）、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. `DomainError` 增加三个类属性：`kind`（input / config / system）、`http_status`（400 / 404 / 500）、`retryable`；新增 `InputError`（继承 ValueError，兼容旧 catch）、`NotFoundError`、`ConfigError`。
  2. `UnreadableTargetError` 从裸 `ValueError` 改为 `InputError`：仍能被 `except ValueError` 捕获，但上层能看出「这是用户输入问题」。
  3. CLI 失败时向 stderr 多打一行 `REVIEW_ERROR {"kind":…,"http_status":…,"message":…}`；人类可读的提示与退出码**一个没改**。
  4. server 解析这一行：任务载荷与同步返回里多 `error_kind` / `error_status`，展示给用户的错误文本会把这行机器可读 JSON 剔除。
- **为什么**：以前 server 只能把子进程 stderr 原样贴给前端，用户分不清「我把路径写错了」还是「你们服务坏了」；日志里也只剩一段文本，没法按错误类型统计。分类放在领域层，CLI 与 server 共用同一套口径。
- **验证**：
  - `python -m unittest discover -s checks` → **213 项 OK**（新增 13 项，含两个真实子进程用例：文件不存在 → kind=input/404、docx 损坏 → kind=input）
  - `python -m unittest discover -s tests` → **642 项 OK**（错误文本与退出码未变，既有断言全过）
  - `python tools/golden_eval.py` → 38 用例 / 召回 100% / 误报 0

### 2026-10-30 · 新增 · 阶段 2 第四块：Rule Analyzer 节点接管 S3 调度

- **文件**：`review_agent/runtime/nodes/rules.py`（新增）、`review_agent/runtime/nodes/__init__.py`、`review_agent/domain/transitions.py`（`STAGE_ARTIFACTS[analysis]` 增 `analysis_stats`）、`review.py`（`run_agent_steps` 改走计划）、`checks/test_rule_analyzer_node.py`（新增）、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. 调度（跑哪些检查、按什么顺序）从 `run_agent_steps` 搬进 `plan_rule_steps()`：先按 manifest 维度声明顺序跑维度绑定处理器，再跑附加检查；附加检查沿用「最后一个维度」的上下文——旧实现就是这么传的，改成「无维度」会改变只认 `ctx["dimension"]` 的处理器行为。
  2. 执行压成 `RuleStepOutcome`：每步记录处理器、维度、命中条数、失败原因。`run_rule_plan()` 提供两种失败语义：`raise`（旧主流程口径，检查炸了立刻暴露）与 `record`（节点口径，单维度失败继续、全失败判失败）。
  3. 检查函数仍在能力层（`_h_*` / Agent 自带 `checks.py`），节点通过 `RuleExecutor` 端口调用——处理器改纯函数留给阶段 3 的 Tool Registry，不在这一步推倒重来。
  4. `analysis` 阶段产物新增 `analysis_stats`：哪台处理器跑了、命中多少条。旧实现只把「执行的检查」写进一条日志，漏跑时报告依然看着正常。
- **为什么**：六节点里 Rule Analyzer 的"规则"最少、但"调度"最容易出错——漏跑一类检查不会报错，只会让报告少报问题（假绿灯）。把调度变成可单独测试的纯函数 + 每步留痕的产物，才能回答「这次到底审了什么」。
- **验证**：
  - `python -m unittest discover -s checks` → **200 项 OK**（新增 17 项）
  - 差分：同一目标（`targets/adversarial_meeting_001.txt`）改造前后 rule-only 跑出**完全相同的 9 条异议**（规则号 / 严重度 / 引用 / 置信度逐字段一致），stats 也一致
  - `python -m unittest discover -s tests` → **642 项 OK / 220s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
- **未纳入本次**：语义审（Semantic Reviewer）仍是旧实现单独一段，analysis 阶段目前只有规则这一半进节点；一个阶段挂多个节点的编排能力还没做（`Workflow` 目前一个阶段一个节点），是 Orchestrator 的活。

### 2026-10-30 · 新增 · 阶段 2 第三块：Cross Reviewer 节点接管 S5（含显式 revisions）

- **文件**：`review_agent/runtime/nodes/cross_review.py`（新增）、`review_agent/runtime/nodes/text.py`（新增：`quote_overlap` 唯一实现）、`review_agent/runtime/node.py`（`NodeResult.revisions` + `advance()` 的应用与校验）、`review_agent/runtime/nodes/__init__.py`、`review.py`（S5 改调节点；`_quote_overlap` / `_norm_quote` 改为委托）、`checks/test_cross_review_node.py`（新增）、`docs/AGENT_RUNTIME.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. 交叉复核规则搬进 `review_findings()`：只查两对配置视角（fact×safety、compliance×style）、quote 相似度 ≥ 0.6、至少一方到 major、冲突双方各降 0.05（下限 0.5、保留两位小数）。同一对发现反复命中时从**已降过的值**继续降——与旧实现就地改 dict 的口径逐字一致。
  2. **运行期新增 `NodeResult.revisions`**：发现不可变，所以「降权」不再就地改上游对象，而是由节点交出同 id 的新版本、`advance()` 按下标替换。校验三条：只能修订本阶段之前存在的发现；同一阶段既新增又修订同一 id 直接拒；`run_id` 必须一致。
  3. 同片段判定（去标点包含 / 二元组 Jaccard）抽成 `runtime/nodes/text.py` 的唯一实现，`review.py` 的 `_quote_overlap` / `_norm_quote` 改为委托——否则「折叠判为同句、复核判为不同句」只是时间问题。
  4. 争议回写报告走 `dispute_to_report_dict()`，键序固定为 between / point / resolution_hint / issue_ids，报告字节与旧版一致。
  5. 编不出领域对象时退回旧 `cross_review(s)` 并写决策日志（阶段 5 前旧函数不删）；旧实现自己会写决策条目，不重复记。
- **为什么**：S5 是「不让互相矛盾的结论蒙混过关」那道门，但旧实现把规则、降权、日志揉在一起，且用**就地改 dict** 的方式降权，改完只剩最终值——报告里看不出「这条为什么从 0.75 变成 0.70」。接管时必须先给运行时一个表达「修订」的正式通道，否则只能继续偷改或干脆不降权（后者会让 `to_confirm` 名单变化，等于悄悄改了产品行为）。
- **验证**：
  - `python -m unittest discover -s checks` → **183 项 OK**（新增 17 项）
  - 差分（关键）：全量历史语料（outputs/ 含 .trash 归档，当前 271 份 / 1162 条 issue / 3 处冲突）——节点产出的争议与降权后的置信度与旧实现**逐字段一致**
  - `python -m unittest discover -s tests` → **642 项 OK / 223s**
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
- **未纳入本次**：`aggregate_repeats`（长文同类聚合）、`drop_covered_comp004` / `collapse_rule_overlap`（同片段归口折叠）仍留在 S6。它们依赖 suppressed/folded 记账，运行时还缺对应的产物通道，单列为下一步。

### 2026-10-30 · 新增 · 阶段 2 第二块：Evidence Analyzer 节点接管 S4

- **文件**：`review_agent/runtime/nodes/evidence.py`（新增）、`review_agent/runtime/nodes/__init__.py`、`review_agent/domain/models.py`（`Evidence.finding_ids`）、`review_agent/runtime/node.py`（悬空证据校验）、`review_agent/adapters/legacy_report.py`、`review.py`（S4 改调节点、`run_id` 上移）、`checks/test_evidence_node.py`（新增）、`docs/AGENT_RUNTIME.md`、`docs/DOMAIN_MODEL.md`、`docs/REFACTOR_PLAN.md`
- **改了什么**：
  1. `verify_quote(quote, text)` 把「引用校验」四类归一：空 → `NOT_CHECKED`、逐字命中 → `ANCHORED`、占位式 → `PLACEHOLDER`、其余 → `UNANCHORED`（应丢弃）。`anchor_findings()` 用它产出 `Evidence` 与被丢弃/缺失的发现；`EvidenceAnalyzerNode` 校验阶段与 `artifacts["target_text"]`，把丢弃清单写进 `evidence_dropped` 产物。
  2. `Evidence` 增加 `finding_ids`：证据在分析之后才产出，晚到的证据无法回填不可变的 `Finding.evidence_ids`；同一句原文常同时支撑多条发现，一对多关系天然属于证据这一侧。
  3. `runtime.advance()` 校验证据指向的发现必须在本 Run 里真实存在（悬空引用比没有引用更难查）。
  4. `review.py` S4 的清单过滤改按节点结论执行；编不出领域对象时退回 `verify_quote` 的逐条入口并写决策日志；`run_id` 从 S6 上移到 Session 创建之后，S4/S6 共用同一个归属。
- **为什么**：S4 是报告可信度的安全线（不可回溯的引用不能进报告），但规则和统计混在 `run_one` 里，没法单独测、也没法在节点接管后复用。挪进节点后同一条规则有了逐条入口（旧 dict）与批量入口（领域对象）两个调用方式，口径只有一份；顺带补上了「证据指不到发现」这个此前无人校验的悬空引用。
- **验证**：
  - `python -m unittest discover -s checks` → **166 项 OK**（新增 16 项，上一轮 150）
  - 历史等价：全量历史（271 份 / 1162 条 quote）按新规则算出的丢弃集合与旧 S4 实现逐条一致
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0
  - `python -m unittest discover -s tests` → **642 项 OK / 220s**

### 2026-10-30 · 新增 · 阶段 1 收口（issue 编解码闸门）+ 阶段 2 第一块（Decision Engine 节点）

- **文件**：
  - 新增：`review_agent/adapters/issue_codec.py`、`review_agent/runtime/nodes/decision.py`、`review_agent/runtime/nodes/__init__.py`、`checks/test_issue_codec.py`、`checks/test_decision_node.py`
  - 修改：`review.py`（S6 裁定改调节点、issue/suppressed 出口回写、`Session.decision()` 支持附加字段）、`docs/REFACTOR_PLAN.md`、`docs/AGENT_RUNTIME.md`
  - 同时登记同日上一轮未入账的 Architecture v1 骨架：`review_agent/domain/*`、`review_agent/profiles/resolver.py`、`review_agent/capabilities/tools.py`、`review_agent/adapters/legacy_report.py`、`review_agent/runtime/node.py`、`docs/` 六份约束文档 + `docs/PHASE0_INVENTORY.md`、`checks/test_domain_v1.py`、`checks/test_legacy_adapter.py`、`checks/test_profile_resolver.py`、`checks/test_tool_registry.py`、`checks/test_architecture_guard.py`、`checks/fixtures/legacy_report_min.json`、`tools/arch_inventory.py`
- **改了什么**：
  1. `issue_codec`：`encode_issue()` 把 issue dict 编成 `Finding`；领域装不下的键（`fix`/`consequence`/`aggregated_count`/`folded_into`…）进 `extras`；标量类型原样保住（`1` 不变 `1.0`、`null` 不变 `false`、缺键不补键）。`export_issues()` 在 S6 出口把最终 `issues` / `suppressed` 换成由领域对象回写的 dict，逐键校验，不一致抛 `CodecMismatch`。
  2. `DecisionEngineNode` + `build_decision()`：Decision 阶段唯一裁定入口。节点校验阶段匹配、缺上游产物、`decision.run_id` 归属，并在返回前用 `consistency_issues()` 复算自检——算不回来的裁定不许落状态。
  3. `review.py` S6：verdict 由 `build_decision` 算出；`DEFAULT_POLICY.decide_from_counts` 只在编解码失败时作兜底并留痕。`Session.decision()` 增加 `**extra`，决策日志新增 `risk_level` / `requires_human_review` / `policy` / `reasons` 四个字段（既有字段一字未改）。编解码失败写进 `limitations`，不静默。
  4. 账本格式修正（顺带）：`2026-10-29 · 新增/修复` 的复合类型不符合词表——`tests/test_api_contract.py::ChangelogContractTests` 只认「新增/修改/删除/修复」四种，这一条在 HEAD 上就已经让全量回归红着。改为「新增」，标题与正文未动。
- **为什么**：阶段 1 要求「结论不再用裸 dict 表达」，但 issue 在 S3~S5 是边跑边改的（追加、折叠、整改补字段），直接改二十多个调用点等于让旧流程停机一个迭代。改成在出口装闸门：用**可逆性**证明领域模型能表达旧报告的每条结论，且报告字节不变（哈希链不漂）。同时把裁定从 900 行的 `run_one` 里挪进节点——为 Evidence / Cross Review 用同样方式接管留出坐位。
- **验证**：
  - `python -m unittest discover -s checks` → **150 项 OK**（新增 31 项，基线 119）
  - 历史语料可逆性：全量 outputs/（含 .trash 归档，当前 271 份）的 issue 与 suppressed `encode→decode` 逐键相等，0 例外
  - 裁定重放：全量历史报告重放 `build_decision`，与报告 verdict 差异仅 5 份（`R-20261028-045/046/047/055/071`，即旧版本「库外事实未核完仍给 pass」的已登记缺陷）
  - `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0（与基线一致）
  - `python -m unittest discover -s tests` → **642 项 OK / 219s**（修账本类型前，全量回归红 1 项；该项在 HEAD 上已红，见「改了什么」第 4 条）

# 2026-10-29

### 2026-10-29 · 新增 · 控制台长任务、冻结式 holdout、金标准防假绿灯、配置静默失效（v0.9.1）

- **文件**：`server.py`、`web/app.js`、`tools/holdout_eval.py`（新增）、`checks/test_console_reliability.py`（新增）、`checks/test_holdout.py`（新增）、`holdout/README.md` + `holdout/cases/.gitkeep` + `holdout/labels/.gitkeep`（新增）、`tools/golden_eval.py`、`tools/llm_client.py`、`review.py`、`checks/test_invariants.py`、`.gitignore`、`VERSION`、`README.md`、`docs/API接口对照表.md`；`docs/盲测协议.md` 增补（该文件被 gitignore，仅本地）
- **改了什么**：
  1. **控制台长任务**：审查超时从写死 300s 改为 `REVIEW_TIMEOUT`（默认 1800s，`--review-timeout` > `REVIEW_AGENT_REVIEW_TIMEOUT` > 默认，clamp 30–86400）；同步/异步两条链路都带实际 `timeout` 字段与超时文案。服务启动时扫 `logs/.jobs/*.jsonl`：能按「目标 + 时间窗」对上报告就恢复 `done`，对不上就明确 `error`（永不伪造 running）；清理 7 天前进度文件。前端 `pollJob` 加 `clearTimeout`、try/catch、连续 3 次失败才提示（1s/2s 退避），后台标签页暂停、回前台恢复。
  2. **holdout**：新增 `tools/holdout_eval.py`。`holdout/cases/*.txt` + `holdout/labels/*.labels.json` 与 `tests/golden/` 物理隔离；`--save-manifest` 生成 sha256 清单，缺清单/多文件/少文件/改一个字节都退出 2，拒绝出分；recall/precision 给 Wilson 95% 置信区间；空 holdout 退出 2 并提示先收 30–50 份真实文档。`holdout/README.md` 写清“先盲标、后看模型、冻结后不调参”。
  3. **金标准防假绿灯**：`golden_eval.regressed()` 不再只比 `labels_hit` 绝对数；召回率下降、误报上升、基线用例被删/改名都判回归（新增 3 项测试）。
  4. **配置/锁/契约**：`llm_client.load_config` 真正传 `chunking/chunk_chars/chunk_overlap`（此前 yaml/UI 改了也不生效），并补环境变量；`review.py` 的 review_id 锁加 `atexit` 兜底清理（异常退出不再留 `.R-*.lock`）；报告加 `schema_version: "1.1"`。
- **为什么**：300s 硬超时让长文 + LLM 在控制台必然失败（202 份报告只有 33 份 LLM、1 份分块）；`JOBS` 纯内存导致服务重启后前端永久停在“审查中…”；`pollJob` 无 try/catch，fetch 一断就永不复原；金标准只看绝对命中数，新增用例会掩盖召回下降；`chunking` 等配置项从 yaml 读进来却没进 `LLMConfig`，用户调参无效且无告警；`id_lock.unlink()` 不在 `finally`，异常路径会漏锁。
- **验证**：全量 `python -m unittest discover -s tests -v` → **642 项 OK / 233.2s**；契约 `tests.test_api_contract` → **158 项 OK / 156.0s**；公开 `python -m unittest discover -s checks -v` → **48 项 OK**；`node --check web/app.js` 通过；`python tools/holdout_eval.py` 在空目录 → 退出 2 并打印“没有真实 holdout 文档”；金标准 **38 用例 / 26 标注 / 召回 100% / 误报 0**。超时单测覆盖默认/env/clamp/CLI 优先级；job 恢复覆盖有报告→done、无报告→error、超时标记、其他目标不误配。

# 2026-10-29

### 2026-10-29 · 修复 · 法条分档/覆盖率真话/控制台安全边界，补可复现基线与 CI（v0.9.0）

- **文件**：`tools/law_cite.py`、`review.py`、`server.py`、`tools/llm_client.py`、`web/app.js`、`tools/version.py`（新增）、`VERSION`（新增）、`requirements.txt`（新增）、`.github/workflows/ci.yml`（新增）、`checks/test_invariants.py`（新增）、`tests/golden/cases/p21_law_probation_tier.txt` / `p22_law_probation_tier2.txt` / `p23_law_salary_chinese.txt` 及对应 labels（新增）、`tests/golden/baseline.json`、`README.md`、`docs/API接口对照表.md`；本地回归另改 `tests/test_law_library.py`、`tests/test_api_contract.py`，新增 `tests/test_coverage_truth.py`（`tests/` 不入库，公开仓库以 `checks/` 为准）
- **改了什么**：
  1. **法条**：`LAW-001` 按《劳动合同法》第十九条分档——3 个月以上不满 1 年→≤1 个月；1 年以上不满 3 年→≤2 个月；3 年以上/无固定期限→≤6 个月；不满 3 个月/以完成一定工作任务为期限→不得约定。支持「自 YYYY 年 M 月 D 日起至 … 止」日期区间；支持「百分之六十」这类中文数字。期限未知且 ≤6 个月时只记 `skipped`、不再给「已核验」；>6 个月仍按绝对上限报。
  2. **覆盖率**：`plan_chunks` 抽成 `plan_chunk_spans`（文本输出逐字节不变）；`semantic_review` 记录每个 `(维度, 块)` 的成败与字符区间；`chars_reviewed` 改为成功区间并集，`complete` 绑定 `failed_chunks`；报告/limitations/Markdown 写清「哪一段没进模型」。顺手把 S4 从「只统计」改成真丢弃 `quote` 不可回溯的 issue（空 quote 与占位式缺失项保留）。
  3. **控制台安全**：`/api/llm/test` 切 host 且无新 key 时拒绝，本地端点允许但清空继承的 key，`base_url` 带 userinfo 直接拒绝；`_guard_api` 的 Origin 校验改为 host + 真实监听端口，带 Origin 的写请求必须带 `X-Review-Console: 1`；`masked_key()` 只留前缀；`_send_file` 用 `resolve().relative_to()` + `is_file()` 判边界，`/static/` 目录不再 500。
  4. **可复现基线**：新增 `VERSION` + `tools/version.py`，报告 `model_version`、控制台 `server_version`、README 标题统一读它；新增 `requirements.txt`；新增 `.github/workflows/ci.yml`；新增 `checks/test_invariants.py`（公开、无敏感样本，覆盖法条分档/覆盖率/安全/版本）。CI 明确：`tests/` 不入库时不假装跑过，改为跑 `checks/` + `golden_eval`。
- **为什么**：审计实测「本合同期限为一年，试用期为四个月」被判 `pass`、`key_facts` 写「未超法定上限 6 个月」，把违法条款认证成合规；202 份历史报告 `complete=False` 一次没出现，LLM 失败块被 `continue` 后仍打印「全覆盖」；`/api/llm/test` 可把已保存的 Key 发到任意 host，且 Origin 校验丢端口让本机任意页面成为同源；README 标题 v0.2.7、报告 v0.2.7-demo、控制台 0.1、正文 v0.8.2 四处版本互相矛盾；`tests/` 被 `.gitignore` 排除导致 CI 实际 0 项。
- **验证**：全量 `python -m unittest discover -s tests -v` → **642 项 OK / 231.7s**；契约 `tests.test_api_contract` → **158 项 OK / 170.8s**（原 148 + 新增 10 项安全回归）；法条 `tests.test_law_library` → 22 项 OK；覆盖率 `tests.test_coverage_truth` → 11 项 OK；公开 `python -m unittest discover -s checks -v` → 14 项 OK；金标准 `python tools/golden_eval.py` → 38 用例 / 26 标注 / 召回 100% / 误报 0，基线已重存。端到端复跑同一句：`verdict=warn`、`key_facts=[]`、`LAW-001` 证据写「一年以上不满三年，试用期不得超过 2 个月，本文写四个月」，不再出现「已核验 6 个月」。

# 2026-10-28

### 2026-10-28 · 新增 · 整改单（改哪一句/改成什么/不改会怎样）+ 库外事实检索核验 + 已学结论复用

- **文件**：`tools/remediation.py`、`tools/research.py`（新增）；`review.py`（`run_one` 增加 `research_enabled`，S5.6 核验、`action_plan`/`issue.fix`/`research` 入报告与 Markdown；LAW-001 与 FACT-001 同句归口）、`server.py`（前端 `research:true` 透传 `--research`）、`web/app.js` / `web/index.html` / `web/style.css`（整改单表格、issue 改法与后果、联网核验开关）、`config/research.yaml`、`knowledge/sources/`（2 条示例来源）、`demo/`（合同/事实两份合成样例 + 3 分钟脚本）、`tests/test_remediation.py`、`tests/test_research.py`
- **改了什么**：每条 issue 现在带 `fix`：`where`（位置）/`from`/`to`（可替换文本）/`consequence`（不改的具体后果）/`action`（replace/insert/annotate/delete/manual）。法条上限、事实数值、用词、隐私脱敏、缺失条款模板由确定性规则生成；没有依据的标 `manual`，不猜。事实审新增：抽「实体+指标+数值」→ 本地来源库 → 已学结论 → 缓存 → 公开网页（360/搜狗，失败降级）→ 支持/反驳/未找到；**两个独立来源或官方来源才进整改单**，单一来源只进「待人工确认」；未找到不生成 issue，只在核验区说明查过什么。`state/learned_facts.jsonl` 沉淀已查实结论，下次同实体同指标直接复用，不再联网。`--research` 显式开启（CLI 默认关闭；控制台复选框默认勾选）。
- **为什么**：原报告只到「可疑」：「补充 COMP-002 要求的条款」既不说补什么、也不说不改会怎样；事实维度只有 9 条断言，库外主张一律「无出处」，用户拿不到「是否属实」的判断。搜索引擎对中文垂类数据经常只返回官网导航，所以本地来源库优先、网页仅兜底；未找到不生成 issue，避免把「没查到」写成噪音。
- **验证**：`tests.test_remediation` 7 项 + `tests.test_research` 10 项 OK；`tests.test_review` / `test_law_library` / `test_fact_learning` 136 项 OK；`tests.test_api_contract` 148 项 OK；`tests.test_purpose` / `test_match_matrix` 86 项 OK；全量 `unittest discover` **615 项 OK**。实测 `targets/selftest/st21_fact_xiaomi_research.txt`：规则层 0 条 → 检索反驳「5 亿」为「1.69 亿」，报告给出整句改写与来源；第二次运行命中本地来源（`local_hits=1`）不再联网；没有本地来源时会命中 `state/learned_facts.jsonl`（`local_hits=0 / learned_hits=1`），同样不联网。`st04_fact_law_trial.txt`：输出整句替换「试用期最长不得超过六个月」，不再只改数字留下「可以约定一年」的自相矛盾。`st05_fact_no_source.txt`：FACT-004「无出处」被检索结果吸收（suppressed），报告改判 `warn` 并写明「1 条库外事实未找到可比来源」，不再用 pass 掩盖未核验。`demo/` 两份合成样例：合同 5 条全部有可替换文本（试用期/工资/竞业/缺终止/缺争议解决）；事实样例给出「5 亿→1.69 亿」整句改写、未找到的 35% 进 warn、豆包 10 亿→1 亿。

### 2026-10-28 · 新增 · 回测老库：`tools/fact_audit.py` 用摘除法算已入库断言的真实贡献

- **文件**：`tools/fact_audit.py`（新增，约 250 行）、`README.md` §41、`docs/知识库自学习.md` §8/§10、`.workbuddy/memory/MEMORY.md`（压缩重写至 3000 字内）
- **改了什么**：新增**只读**工具，对 `knowledge/facts.json` 的每条断言做 `库 vs 库 − 本条` 两轮预演，
  复用 `fact_promote` 的 `_shadow_pass` / `_corpus_cases` / `_exercised_by` / `_shadow_llm`（不另写一套审查）。
  五档结论：`kept`（找回真错误且无误报）/ `harmful`（让正确陈述变误报）/ `shadowed`（单独贡献 0 但同句有同伴在报）/
  `weak_signal`（语料提到实体却零贡献）/ `unmeasured`（语料没提该实体）。参数 `--rule-only` / `--llm` /
  `--only <实体>` / `--json [路径]`；退出码 1 只在出现 `harmful` 时（未测到与无信号是语料的债，不阻断但不隐藏）。
- **为什么**：§40 把门禁修对了，但库里 9 条断言是**门禁之前**写进去的，没人用新尺子量过——
  "这些断言可验吗"这个问题只能拿同一把尺子回测一次才答得出来。**不能直接拿现库断言当候选跑
  `fact_promote shadow`**：`shadow` 的算法是 `库+候选 vs 库`，两边都是现库，增量恒 0，什么都测不出来。
  必须摘除法，且 `before` 必须是"没有本条"——方向反了算出来的数没有任何人能读对。
- **验证**：语料 35 份，按实体预过滤压到 **13 份**（数据安全法 0 份、豆包 4 份、华为 3 份、劳动合同法 2 份、
  其余各 1 份）。`--rule-only` **3.7s**、`rule+llm` **1m51s**，两轮结论完全一致：
  **6 kept / 2 shadowed / 1 unmeasured / 0 harmful**。9 条断言**无一条**在把对的判错
  （摘掉后误报数不变，说明它们在干净语料上不开火）。

### 2026-10-28 · 修复 · 预演的正贡献按「（语料, 标注）」定位：同名标注不再互相顶掉

- **文件**：`tools/fact_promote.py`（`_shadow_pass` 的 `on` 改 key，新增 `_label_key`，`shadow` 的 `on_target_labels` 跟随）、`tests/test_fact_gate.py`（新增 `LabelKeyAcrossCasesTests` 2 项）、`README.md` §40.4/§41.3
- **改了什么**：`_shadow_pass` 返回的 `on` 从 `{标注 id}` 改成 `{(语料名, 标注 id)}`；
  `shadow()` 落盘的 `on_target_labels` 用 `_label_key()` 拍成 `语料:标注`（测试替身给的纯 id 原样放过，
  不能把 `"L01"` 切成 `L:0`）。
- **为什么**：回测老库时「豆包·月活」显示**正贡献 0**，可它明明找回了 `p11_fact_doubao_mau` 的 MAU 错误。
  根因是**标注 id 只在单份语料内唯一**：`p11` 与 `p12` 的标注都叫 `L1`，`on.update(matched)` 把两份合并成一个，
  `before` 轮里 `p12` 的 `L1` 已经在了，`after` 轮加进 `p11` 的 `L1` 看不出变化。
  后果不是报错，而是 `gate` 看到 `on_target_hits == 0` → 判 `weak_signal` → `unverified` →
  **一条好断言必须人工放行才能进库**。这种缺陷比"放过坏断言"更难发现：表现是"系统太保守"，
  人只会顺手点放行，没人回头查为什么。
- **验证**：`tests/test_fact_gate.py::LabelKeyAcrossCasesTests` 2 项（两份语料的同名标注必须算两次 /
  `_label_key` 不切纯 id）全绿；两模块 **61 项 OK / 1.7s**，全量 **598 项 OK / 299.5s**
  （原 595 项 + 本轮新增 3 项）。回测中豆包两条由 `shadowed` 转 `kept`，
  `kept` 从 4 条升到 6 条。

### 2026-10-28 · 修复 · 模型自提的 FACT-001 不再混进事实库的账（预演假误报 + 缺口漏记）

- **文件**：`tools/fact_promote.py`（`_shadow_pass` 加 `tool` 过滤）、`tools/fact_gap.py`（`collect` 的跳过条件）、`tests/test_fact_learning.py`（新增 1 项）、`README.md` §40.4 第 3 条改写 + §41.4
- **改了什么**：
  1. `_shadow_pass` 只统计 `tool` **不以 `llm` 开头**的 `FACT-001`（即事实库判的）；
  2. `fact_gap.collect` 的跳过条件从 `rid == "FACT-001"` 改成 `rid == "FACT-001" and "llm" not in tool`；
  3. README §40.4 第 3 条原写「LLM 非确定性不影响这道门的判据，因为 FACT-001 是确定性判定」——**这句是错的，已改写**。
- **为什么**：干净对照样本 `c08_doubao_api_ok`（豆包大模型/API 属另一口径，应 0 issue）在 26 轮预演里
  偶发一条 `rule_id=FACT-001`、`tool=llm_api` 的 issue，而单独连跑 3 次都复现不出来（每次都是 FACT-002 + COMP-004）。
  根因在 `review.validate`：它只把"quote 里**没有**事实库实体"的 FACT-001 降级成 FACT-002，
  quote 里只要有实体（"豆包""华为"）就**原样保留 rule_id**。两层后果：
  ① 预演把它当成事实库判的 → LLM 的偶发性直接变成"候选把对的判错"的证据（凭空多一条 `off_target`，误杀好候选）；
  ② 缺口清单按 `rule_id` 一律跳过 → 模型裸判的 FACT-001 **不进账**，
  而"没依据就开口"正是最该记的缺口。判据不能只信 `rule_id`（它不是排他的），还得看 `tool`。
- **验证**：改后回测中「豆包·推出」的 `存量误报 1 → 0`，`rule-only` 与 `rule+llm` 两轮结论重新对齐；
  新增 `test_model_written_fact_001_is_still_a_gap`（`FACT-001 + tool=llm_api` → `model_judgment`）通过，
  原有 `test_library_assertion_is_not_a_gap`（`FACT-001 + tool=retrieve + regex` → 非缺口）仍绿。

### 2026-10-28 · 修复 · 门禁心脏三处错：off_target 没减基线、shadow 关着 LLM、安全线只防 URL 不防数值（`tools/fact_promote.py`）

- **文件**：`tools/fact_promote.py`（重写 `shadow`，新增 `_corpus_cases` / `_exercised_by` / `_shadow_pass` / `_shadow_llm` / `_num_set` / `_expected_numbers` / `_value_from_target`，重写 `gate` 与 `verdict`；`_source_ok` 返回三值区分「硬违规」；`promote`/`cmd_gate`/`cmd_shadow`/`cmd_list` 跟随）、`tools/fact_gap.py`（草稿补 `source_quote` 空字段）、`tests/test_fact_gate.py`（新增 27 项）、`tests/test_fact_learning.py`（`_cand` 补 `entity_confirmed`、`test_off_target_must_be_zero` 固定 rule-only 并断言 mode）、`docs/知识库自学习.md` §5/§8 门禁表、`README.md` §40
- **改了什么**：
  1. `off_target` 从**绝对值**改成**增量**：`after_off − before_off`，逐条用 `(语料名, normalize_issue)` 指纹对齐。存量误报单独记 `off_target_baseline` + `off_target_baseline_cases`，不归咎候选但写进 warnings。
  2. `shadow` 从写死 `use_llm=False` 改成**跟随生产配置**（`_shadow_llm(None)` 读 `llm_client.load_config()`，本地端点免 key），`metrics.mode` 记 `rule+llm` / `rule-only`；`--rule-only` 只作排查降级，降级必须留标记。
  3. 新增 `value_from_target`：期望值与被审文档 `evidence_quote` 里的数字重合时，必须有独立的 `source_quote` 且其中含该数值，且 `source_quote` 不能是被审文档原句（比对去掉空白与标点）。`5,000 万台` 与 `0.5 亿` 归一为同一数。
  4. `gate` 结论拆四档 `verdict`：`pass` / `incomplete`（草稿没填完）/ `rejected`（实质否决）/ `unverified`（测不出来）。`entity_confirmed` 为假 → incomplete；`source_url` 指向 `targets/` 或 `file:` → `source_from_target`（否决，不是"字段没填"）。
  5. 语料预过滤：只跑正文含 `entity` 的用例（FACT-001 触发前提就是 `entity in text`），跳过的份数记 `corpus_skipped`。35 份语料实测压到 3～4 份。
  6. `weak_signal` / `unmeasured` 从"只警告"改成**拦截**（`unverified`），必须 `--allow-unmeasured` 人工放行，放行写进 `metrics.override` 与 warnings。
  7. shadow 抛异常从"警告"改成 `shadow_failed` 否决（fail-closed）：预演跑不起来就没有负贡献证据，不能算通过。
  8. 新增 `confirm-entity <cid> --entity <写法> --by <人>` 命令：草稿里 `entity` 是正则剥出来的（「华为」会被剥成「华」），改对实体必须留痕（`from`/`to`/`by`/`at`），不能靠手工改 JSON。
- **为什么**：复核结论是「外壳的工程纪律都对，但唯一真正守门的环节算法是错的」——① `off_target = sum(after)` 没减基线，语料里只要有一条存量误报，任何候选都过不了，合法断言全被误杀；② 纯规则模式下 FACT-002/003/004、LOGIC-004 根本不存在，在那里算"误报零容忍"测的不是生产行为；③ `_source_ok` 只查 URL 字符串里有没有 `targets/`，草稿里的 `evidence_quote` 就是被审文档原句，把「华为出货 5 亿」抄进 expected 再贴个真 URL 照样放行——红线防住了"拿文档当链接"，没防住"拿文档当数字"。另外 `entity_confirmed:false` 只写在草稿里从没人检查，人对着正则猜错的实体填数，后续冲突检查全按错实体走。
- **验证**：新增 `tests/test_fact_gate.py` **27 项 OK**（增量算账 / 指纹对齐 / 模式跟随生产 / 崩溃 fail-closed / 数值三条拒绝路径 / 四档结论 / replace 守门 / 实体确认留痕），`test_fact_learning.py` 31 项仍全绿，两模块合计 **58 项 OK / 1.7s**，全量 **595 项 OK / 245.2s**。真实 LLM 双跑对照（35 份语料、压到 3 份）：错误断言「毛利率 90%」→ `off_target` 增量 **3**（c01/p01/p02 三份干净语料全被判错）、`off_target_baseline=0`、28s；合理断言「毛利率 26%（±2 倍容差）」→ `off_target` 增量 **0**、9s，仅 `weak_signal` 拦（语料没有该属性的阳性标注）。旧实现下前者与后者都会被算出同一个绝对值，无法区分。

### 2026-10-28 · 修改 · replace 加乐观锁防清库、批量导入补冲突检查（`tools/fact_promote.py` + `tools/fact_import.py`）

- **文件**：`tools/fact_promote.py`（`replace_all` 加 `expect_current` / `allow_shrink` / 空列表拒绝；新增 `_bulk_conflicts`，`bulk_append` 走静态检查并记 `metrics.shadow="skipped-bulk"`）、`tools/fact_import.py`（`--replace` 传当前条数、新增 `--allow-shrink`、`ValueError` 转成人话退出码 2）、`tests/test_fact_gate.py`（`ReplaceAllGuardTests` 5 项）
- **改了什么**：`replace_all` 现在必须给 `expect_current` 且与当前断言数一致（乐观锁），条数减少必须显式 `allow_shrink`，空列表一律拒绝。`bulk_append` 在写库前对每一行做 `no_conflict` 检查（同实体同属性两个值直接拒绝，并指出是第几行），审计行标记 `metrics.shadow="skipped-bulk"` 说明批量路径没有跑预演。`fact_import` 捕获该 `ValueError` 并返回退出码 2，不再抛 traceback。
- **为什么**：「只增不改不删」写在设计文档里，但 `replace_all` 能一次删光整库，且没有任何东西区分"迁移"和"传错文件"；批量导入更彻底——它绕过门禁直接 `_apply`，能把两个互相矛盾的断言同时塞进库，之后 `fact_review` 拿两个不同的期望值判同一句话，结论取决于遍历顺序。批量路径跑不了 shadow（N 条 × 2 轮全语料），但**冲突检查不能省**。
- **验证**：`ReplaceAllGuardTests` 5 项（缺 `expect_current` / 条数不符 / 空列表 / 未授权收缩 / watchlist 必须保留）全绿；实际导入 1 行 `宁德时代` → 版本 1.1.0→1.1.1、`total=10`；重复导入同一行 → `added=0, replaced=1`、版本不变（幂等）；缺 `source_url`/`review_due` → 退出码 2 并逐行点名。

### 2026-10-28 · 新增 · 知识库缺口清单：让审查器自己报「该补哪条事实」（`tools/fact_gap.py`）

- **文件**：`tools/fact_gap.py`（新增，约 470 行）、`review.py`（`GAP_LOG` 全局 + `_gap_log_path()` + S6 前记账 + `--no-gap-log`）、`tests/test_fact_learning.py`（`GapCollectTests` / `GapLogStoreTests` / `GapIsolationTests` / `ReviewGapIntegrationTests` 共 22 项）、`README.md` §39、`docs/知识库自学习.md` §8
- **改了什么**：新增四类缺口识别——`model_judgment`（模型在 fact 维度下判断且非 FACT-001）、`law_missing`（LAW-001 因缺条文未执行）、`unsourced_claim`（FACT-002/003）、`watchlist_unasserted`（点名 watchlist 实体），追加到 `state/gap_log.jsonl`。每行写死 `usable_as_evidence: false`；重复出现追加 `op=repeat`、补齐后追加 `op=resolve`，不覆盖也不删行。报告新增顶层 `fact_gaps{recorded,new,repeat,log,kinds,top,usable_as_evidence}`，MD 报告新增「知识库缺口」小节，limitations 增一句边界说明。`--no-gap-log` 关闭落盘（报告记 `disabled:true`）。
- **为什么**：用户判断「法典和证据库几乎是空的，流程再严密也只是把错误记录得滴水不漏」。此前审到库外实体只有两种结局：静默跳过、或模型按参数记忆裸判（`tool=llm_api`），两者都不留任何工单，于是 facts.json 永远停在手灌的 9 条。规则侧早就有 `rule_miner` 提案，事实侧没有对应物。
- **验证**：`tests/test_fact_learning.py` 31 项全绿；一段「据行业报告显示，微信月活 14 亿」文本实跑记录 3 条（1 `unsourced_claim` + 2 `watchlist_unasserted`），limitations 出现「缺少库内依据」；FACT-001（库内断言）不记缺口、非 fact 维度不记；同一句写两遍只留 1 条；改 `review_id` 后 `gap_id` 不变；缺口日志随 `review.OUTPUTS` 重定向落到临时目录（真实 `state/` 无写入）；审查前后 `knowledge/facts.json` sha256 不变。

### 2026-10-28 · 新增 · 候选断言门禁与唯一写入口（`tools/fact_promote.py`）

- **文件**：`tools/fact_promote.py`（新增，约 700 行）、`review.py`（无写入）、`docs/知识库自学习.md` §5/§10、`docs/API接口对照表.md`（`fact_gaps` 字段行）、`README.md` §39.3
- **改了什么**：新增 `status/list/show/gate/shadow/promote/rollback/log` 七个命令。门禁六道：`schema`（entity/attr/期望值/note/review_due）、`source_verifiable`（http(s) 或「《法名》第X条」，且**拒绝指向 targets/**）、`type_correct`（`max` 必须 `unit_hint`、`number` 的 `tolerance` 必须 >1、`date` 必须四位年份）、`no_conflict`（同实体同属性同年度不许两个值）、`off_target`（必须为 0）、`on_target_hits`（可为 0，但记 `weak_signal`）。`promote` 必须 `--confirm-manual`（`--auto` 退出码 2）；写库顺序为快照 → 升 `knowledge_version` → 追加断言 → `state/fact_log.jsonl` 记账 → 缺口 resolve；`rollback` 按 `candidate_id` 精确删除并再升版本。`_apply` 做幂等去重（同实体同属性同类型同年份同期望值不重复写）。
- **为什么**：没有唯一入口，"人工补据"就会退化成手工编辑 `facts.json`——不留审计、不升版本、历史报告被判成「知识库被篡改」（红），而且没人核对来源。`off_target` 这一道尤其关键：存一条「把对的判错」的事实，比漏一条危害更大。
- **验证**：门禁四类拒绝实测——毛利率写 90% → `off_target=3`（命中 c01/p01/p02 三份干净或已标注用例）并拒绝；`source_url=targets/uploads/...` → 拒绝并给出「被审对象不能当证据」；`type=max` 缺 `unit_hint` → 拒绝；豆包月活写 10 亿 → 拒绝并指出与现有断言 1 亿冲突。晋升闭环：版本 1.1.0→1.1.1、断言带 `source_url/retrieved_at/review_due/promoted_by`、快照留存、缺口转 resolve；回滚后断言数回到 9、版本再升一级。

### 2026-10-28 · 修改 · 批量导入改走治理路径，修掉「重写整库」与相对路径崩溃（`tools/fact_import.py`）

- **文件**：`tools/fact_import.py`、`tools/fact_promote.py`（新增 `bulk_append` / `replace_all`）、`tests/test_fact_learning.py`（2 项导入用例）
- **改了什么**：`--by` 改为必填；每一行必须带 `source_url` + `review_due`，缺行直接拒绝（退出码 2）；写入统一走 `fact_promote.bulk_append`（快照 + 升版本 + 记账），`--replace` 走 `replace_all`（同样快照并保留 `watchlist`）。修两处缺陷：① 旧实现重建 `payload` 时**丢掉 `watchlist`**，跑一次批量导入就把 4 条「刻意不写断言」的说明冲掉；② 回显用 `FACTS_PATH.relative_to(ROOT)`，指向仓库外路径时直接抛 `ValueError`，新增 `_rel()` 兜底。
- **为什么**：批量导入最容易变成「往事实库灌来源不明数字」的快捷方式——它曾经是唯一能写 `facts.json` 却不过任何门禁的路径，与「唯一入口」的设计直接冲突。丢 `watchlist` 与相对路径崩溃是写测试时暴露出来的真实缺陷（临时知识库目录一放就炸）。
- **验证**：同一份 CSV 连跑两次，断言数 9→10→10（幂等），版本只升一次（1.1.0→1.1.1），`watchlist` 仍为 4 条；缺 `source_url`/`review_due` 的行被拒（退出码 2，点名到行）。

### 2026-10-28 · 修复 · changelog_guard 对新目录只看到目录名，整部法典被放行

- **文件**：`tools/changelog_guard.py`（`git_changed` 的 `status` 加 `-uall`）、`tests/test_changelog_guard.py`（+2 项，其中一项 mock `subprocess.run` 锁住 `-uall`）
- **改了什么**：`git status --porcelain` 改为 `git status --porcelain -uall`。默认 `-unormal` 对**未跟踪的新目录**只输出一行 `?? knowledge/laws/`；目录名没有扩展名，`must_register()` 判成「不必登记」，于是新加的 `knowledge/laws/{index.json,labor-contract.md,synonyms.json}` 三个文件在守门时被折叠成一行"不登记"。同类风险还有 `tools/law_*.py` 这类新目录下的代码。
- **为什么**：本轮新增法条库后跑 `--explain`，输出里赫然是 `[不登记] knowledge/laws/`——那三个文件是**判定依据**（法条原文），改了它们等于改了裁判尺度，账本却可以不知道。守门工具漏掉新增目录，比没有守门更糟：它给了一个"已检查"的假信号。
- **验证**：修前 `--explain` 为 `改动=19`、`knowledge/laws/` 一行「不登记」；修后 `改动=25`，`knowledge/laws/index.json`、`labor-contract.md`、`synonyms.json` 逐个列出并标「需要登记」，`漏登记=0`（因本轮已在账本登记）。`tests/test_changelog_guard.py` **17 项 OK**（新增 `test_new_directory_is_expanded_to_files` 断言命令行含 `-uall` 且 `decide()` 会因此拦下，新增 `test_law_library_files_must_register`）。

### 2026-10-28 · 新增 · 法条库：把「模型常识」换成「条文原文」（LAW-001）

- **文件**：`knowledge/laws/`（新增：`index.json`、`labor-contract.md`、`synonyms.json`）、`tools/law_index.py`（新增）、`tools/law_ingest.py`（新增）、`tools/law_cite.py`（新增）、`review.py`（新增 `_h_law` / `_dedupe_cites` / `Session.law_cites/law_skipped` / `CONTRACT_RATIO_RE`；`FOLD_INTO` 加 `LAW-001`；报告新增 `law_library`；limitations 新增覆盖自报）、`rules/compliance.yaml`（新增 `LAW-001`）、`tools/rule_exec.py`（登记执行器）、`agents/doc-review/manifest.json`（1.3.0 → 1.4.0，checks 加 `builtin:law_cite`）、`config/knowledge_version.txt`（1.0.3 → 1.1.0）、`config/rules_version.txt`（a4 → a5）、`tests/test_law_library.py`（新增 16 项）、`tests/golden/cases/{p20_law_probation_illegal,c16_law_compliant}.txt`（新增）+ `labels/p20_*.labels.json` + `baseline.json` 重存、`README.md` §38、`docs/API接口对照表.md`
- **改了什么**：
  1. **数据层**：每部法一个 md（条头「第X条　」，章/节用 `##`/`###`）+ 一份 `index.json` 元数据（`status`/`effective_date`/`source_url`/`retrieved_at`/`article_count`）。首批收录《劳动合同法》98 条，来源人社部官网，`verify_note` 记录条号 1~98 连续、且第 19/20/37/38 条与钦州检察院转载页逐字比对一致。
  2. **索引层** `tools/law_index.py`：按「第X条」切条（条号必须等于期望的下一条，否则视为正文引用并并入上一条）；`search()` 用关键词 + 同义词扩展，用户词的权重 1.0、扩展词 0.5；`get_article()` 按条号**精确取**（确定的事不交给检索猜——实测「竞业限制三年」检索会飘到第 23 条）；`check()` 把「条号连续 + 条数与声明一致 + 元数据齐全」当门禁。
  3. **导入层** `tools/law_ingest.py`：`--file raw.txt --article-count N ...` 一条命令入库，切条/校验/写 index/升 `knowledge_version` 全自动；`--dry-run` 只切不写。公网页面分页（实测钦州页只到第 50 条）这类残缺文本会被 `条数不符` 拦下。
  4. **判定层** `tools/law_cite.py`（LAW-001，确定性）：只判三类「超限即违法」的法定量——试用期 > 6 个月（第十九条）、竞业限制 > 2 年（第二十四条）、试用期工资 < 80%（第二十条）。超限报 major 并附条文原文；**未超限记「已核验」**（"查了没问题"与"没查"必须分开）；法条库缺条文时返回 `skipped`，由报告 limitations 明说"该项未执行"。
  5. **报告契约**：issue 新增 `citation` / `citations`（法名 + 章 + 条号 + 原文）；顶层新增 `law_library{available, articles_total, laws[], cited[], skipped[]}`；limitations 自报「法律依据仅限已收录的法条库：…；库外不作引用；命中条文 ≠ 条文适用」。
  6. **归口**：同句已由 `FACT-001`（blocker，事实库断言）判过时，法条原文并入它的 `evidence` 而不新增一条；同句多类违法（如既超试用期又压低工资）在报告层合成一条、依据用 ①② 并列。`FACT-004` 对「试用期工资比例」豁免（工资比例是约定值，不是披露数据）。
- **为什么**：用户的原话是「能不能让这个系统连接真实数据库，比如中华人民共和国法典，然后通过关键词匹配在对应章节找出相关依据」——前面几轮的原生痛点是「事实库只有 2 条，模型凭记忆判法律问题」。收了库以后，报告能说「依据《劳动合同法》第十九条」并给出原文，读者可以自己翻回去核对；而在此之前，evidence 里只有模型自己的措辞。同时明确划了边界：**检出条文 ≠ 条文适用**（例外情形仍要人判），所以不允许据此给"合法/违法"的总体结论。
- **验证**：
  - 法条库自检：`law_index.py --check` 通过（1 部法 / 98 条 / 条号 1~98 连续）；`--article 19` 与 `--article 第十九条` 都能取到第十九条全文。
  - 检查器：一片违法合同（试用期 12 个月 + 工资 60% + 竞业 3 年）报 3 条 LAW-001，条号分别 19/20/24；合法样本（3 个月 / 85% / 2 年）0 条 issue、3 条「已核验」；「试用期不得超过十二个月」仍报（把上限说错也是错）；「营收同比增长 60%」不报。
  - 导入门禁：50 条文本声明 98 条 → `[FAIL] 条数不符`、退出码 1；5 条文本声明 5 条 → 退出码 0。
  - 端到端：合同用例报告 `law_library.cited` = {第十九条, 第二十条, 第二十四条}，limitations 含「法律依据仅限已收录的法条库」；`rule_execution.deterministic` 26 条（含 LAW-001），不再是「仅提示」。
  - 回归：`tests/test_law_library.py` **16 项 OK**（含金标准外的排序用例：`竞业限制 不得超过二年` 必须排到第 24 条——初版被长同义词压到第 23 条，已用「用户词 1.0 / 扩展词 0.5」修正）。
  - 金标准：**35 例 / 23 标注 / 召回 100% / 精确率 100% / 误报 0**（新增 p20 阳性 + c16 干净对照，基线已重存）。首跑曾因 p16 同句被 FACT-001 与 LAW-001 各报一条而产生 1 条误报，改为「并入 FACT-001」后归零。
  - 全量回归 **537 项 OK / 244.9s**（含本轮新增的 2 项守门测试）；docx 暴测 A 14 + B 17 + 31 全绿；`integrity.py verify`：ledger 223 条链完整、161 份报告 STALE（规则/知识库/Agent 升级，属正常演进）、**0 TAMPER**。

### 2026-10-28 · 修改 · 事实库 7 → 9 条：新增「上限」类型与年度口径（红队 A1/A2/A3）

- **文件**：`knowledge/facts.json`（7 → 9 条断言 + `watchlist` 4 条）、`knowledge/kb_laws.md`（新增「数值类条款」表）、`config/knowledge_version.txt`（1.0.2 → 1.0.3）、`review.py`（`fact_review` 新增 `max` 分支 / `year_ctx` / `watchlist` 采集；`limitations` 自报未核实体）、`tests/test_fact_library.py`、`tests/test_redteam_20261028.py`（新增）
- **改了什么**：
  1. 新增断言 `劳动合同法·试用期`（`type: "max"`，上限 6 个月，`unit_hint: "个月"`）与 `华为·出货量`（2024 年约 5,000 万台，容差 2 倍，`year_ctx: 2024`）。
  2. `type` 从 {number, date} 扩到第三类 **max**：上限必须单侧判定。旧的 number 是对称容差（`ratio < 1/tol` 也报），拿它判上限会把合法的「试用期 3 个月」算成「偏差 0.5 倍」。
  3. `year_ctx`：句内没写声明年份就不判——华为 2019 年 2.4 亿台是对的，不能被 2024 年的口径判错。
  4. `watchlist`：微信 / iPhone / 抖音 / 民法典 四个高频实体**故意不写断言**（口径按季变、单值会互相误伤），改为在报告 limitations 里明说「其数字/日期未做真伪判定」。
- **为什么**：红队 A1/A2 打得很准——st03（华为 5 亿台）、st04（试用期 12 个月）都抓到了，但抓它们的是 **LOGIC-004 的模型常识**（confidence 0.85，basis=model），事实库一条依据都没有。这正是用户说的"靠模型脑补，独立裁判就破产"：蒙对是运气，且下次不一定蒙对。补库后同一现象由 `FACT-001` 的 `rule` 级命中，evidence 直接指到条款号/数据源。
- **验证**：
  - 红队 selftest：st03/st04 从 `LOGIC-004(model, major)` → `FACT-001(rule, blocker)`，`evidence` 含「第十九条」「Canalys 2025-01」，仅凭规则层即可复现。
  - 9 组突变：12 个月报 / 6 个月不报 / 3 个月不报；华为 5 亿报 / 4840 万不报 / 2019 年 2.4 亿不报（year_ctx 挡住）；iPhone 正确陈述不报且 limitations 出现「未做真伪判定」。
  - 事实库 schema 锁：`max` 必须带 `unit_hint`（否则「3 人」会被当成 3 个月）；note 必须给日期或条款号（新增 `第X条` 分支）。
  - 全量回归 **519 项 OK / 225.6s**；金标准 33 例 / 21 标注 / 召回 100% / 精确率 100% / 误报 0。

### 2026-10-28 · 修复 · 缺失条款的证据锚在文档首行，指向「有甲方」而不是「缺争议条款」（红队 C1）

- **文件**：`review.py`（新增 `clause_anchor(text, where)`，`compliance_review` 的 COMP-001/002/003 分别取头/尾锚点）、`tests/test_redteam_20261028.py`
- **改了什么**：缺失型问题原来一律用 `doc_anchor(text)`（首个非空行）当 `quote`，于是 st17「缺争议解决条款」的 quote 是「甲方：北京示例科技有限公司」——证据指向"有甲方"。现在 `COMP-001`（主体缺失）锚开头，`COMP-002/003`（生效/争议）锚**文末最后一条**，`location` 写成「文末（最后一条之后，应在此处补该条款）」，`evidence` 加「缺失定位：文末「…」之后未见该要件」。
- **为什么**：`quote` 必须原文逐字是为了前端高亮与回查；但缺失型问题没有病句可引，"逐字可回查"就退化成了"随便找一个真实句子"。锚到文末，读者一眼能对上"本该在这一条后面再补一条"。
- **验证**：st17 的 `COMP-003.quote` = `本合同自双方签字盖章之日起生效。`（= 原文最后一行，仍逐字）；回归锁 `test_missing_clause_anchors_at_document_tail` 断言 quote 等于末行、location 含「文末」、evidence 含「缺失定位」、且 quote ∈ `target_text`。

### 2026-10-28 · 修改 · 同一现象两条规则一轻一重：FACT-003/004 归口到 LOGIC，折叠留痕（红队 B1/B2）

- **文件**：`review.py`（新增 `collapse_rule_overlap` + `FOLD_INTO` 表，在语义审之后调用；`Session.folded`）、`rules/fact.yaml`（FACT-003/004 的 desc 写明分工）、`config/rules_version.txt`（a3 → a4）、`targets/selftest/{gen_selftest.py,manifest.json}`、`docs/API接口对照表.md`
- **改了什么**：
  1. 判据写进规则库：**fact 侧管「有没有前提/口径」，logic 侧管「证据强度/算术矛盾」**；同一句同时命中时由 LOGIC 主判，FACT-003/004 折叠进 `suppressed`，带 `folded_into` + `fold_reason`。
  2. FACT-003 启发式按**结构**分两档：因果连接词 + 成效/归因词同现 → 按规则原级 `major`（0.72）；只有连接词 → 线索级 `minor`（0.6）。不再靠调阈值，而是靠"句子里有没有成效词"。
  3. 没有对手（纯规则模式）时线索照常保留——不能为了去重把唯一的判断吞掉。
- **为什么**：红队 B1/B2 说的是同一件事：st06 的同一句被 FACT-003（minor 0.6）与 LOGIC-003（major）同时报，st07 被 FACT-004（minor）与 LOGIC-001（major）同时报。用户看到的是轻的那条，真问题被埋。
- **验证**：st06 现只剩 `LOGIC-003 major`、st07 只剩 `LOGIC-001 major`（`HIT`，不再 `HIT_DUP`）；回归锁 4 项覆盖"折叠到结论级""严重度相同也折叠""无对手时保留""纯规则模式不吞线索"。

### 2026-10-28 · 修复 · COMP-004 把内部周报的「联调完成 8/10」当对外披露数据（红队 B3）

- **文件**：`review.py`（新增 `INTERNAL_SCOPE` + `drop_internal_progress_comp004`）、`rules/compliance.yaml`（COMP-004 desc 写明不适用内部进度）、`tests/test_redteam_20261028.py`
- **改了什么**：COMP-004 命中内部语境（本周/进度/里程碑/联调/内测/完成…）**且**句内没有外部数据语境（行业/市场/全国/第三方/白皮书…）时豁免，条目带 `fold_reason` 进 `suppressed`；内部周报里引用行业数据照样要求标来源。
- **为什么**：COMP-004 的原文是"披露具体数据/统计结论时须标注口径与来源"。对内的工作进度既不是披露，也没有外部来源可标——要求它给自己标来源是范畴错误。st18 一份完全正常的周报被判 `COMP-004 minor`，属于"为了报而报"。
- **验证**：st18 从 1 条 minor → **0 条 issue，文档裁定 pass**；回归锁同时锁反向（内部周报引用"行业市场规模 120 亿元"仍报 COMP-004），防止豁免扩大成"内部两字就免死"。

### 2026-10-28 · 新增 · 规则清单外发现（OPEN-*）单独成列并自报来源（红队 C2）

- **文件**：`review.py`（报告新增 `open_discoveries[]`；limitations 自报条数）、`docs/API接口对照表.md` §3.5 + 字段表
- **改了什么**：`OPEN-*` 本来就带 `open_discovery: true` / `confidence_basis: "model"` / `rule_text`（未经规则库定义）/ 单次上限 3 条 / 默认 minor；这次再加两处可追溯性——报告顶层单独给 `open_discoveries[]{rule_id,severity,quote,confidence,source:"model"}`，`limitations` 写明「模型另报 N 条规则清单外发现，不参与规则覆盖率统计，须人工确认」。顺带修正文档里 `OPEN-*` 默认档写成 major 的过期描述（代码一直是 minor）。
- **为什么**：红队质疑"OPEN-* 是不是模型即兴发挥"。结论是**有治理但不够显眼**：它混在 `issues[]` 里，rule_id 长得像规则库条目，审计时分不清"规则判定"和"模型猜测"。不列出来，等于让模型猜测借规则的名义发言。
- **验证**：回归锁 `test_open_discoveries_are_listed_separately` 断言字段存在、`source == "model"`、rule_id 以 `OPEN-` 开头；st06/st09/st12 的 `OPEN-*` 仍正常产出（未被误收敛）。

### 2026-10-28 · 修改 · 红队 selftest 从"人肉数成绩"改成自动判定，用例预期随整改同步

- **文件**：`targets/selftest/run_selftest.py`（重写：`judge()` / `--rejudge` / 退出码）、`targets/selftest/gen_selftest.py`（st03/st04 由 `expect_miss_gap` 升为 `should_catch`；st06/st07 加 `expect_rules` + `expect_severity`）、`targets/selftest/manifest.json`、`run_result.json`、`eval_summary.json`
- **改了什么**：新增判定口径 `HIT / MISS / GAP / GAP_FILLED / CLEAN / FP / HIT_DUP / MISS_SEV`，退出码 1 = 出现 MISS/FP/HIT_DUP/MISS_SEV；`--rejudge` 用已有原始 issues 按新口径重算（不重跑模型、不产生新报告）。旧脚本把**被审文档的裁定**命名为 `verdict`，与"用例是否通过"极易混淆，已改为 `doc_verdict` + `outcome`。
- **为什么**：上一版报告写"14/14 召回"，但原始结果里只有 issues，判定是人肉数的——换个读者就会数出别的数。测试成绩必须机器可复现。用例预期同步是**整改的必然结果**，不是迁就实现：st03/st04 的事实补上了就不该再"预期缺口"；st06/st07 归口后应断言"两条里恰好一条以 major 出现"，因此新增 `HIT_DUP` 来抓"两条同时出现"。
- **验证**：`py -3 targets/selftest/run_selftest.py --rejudge` → **HIT 16 + CLEAN 4 = 20/20，退出码 0**；同一份原始数据在新口径下 st03/st04 为 `HIT`（此前 `GAP_FILLED`）、st06/st07 为 `HIT`（此前 `MISS`）。重跑产生的 R-20261028-022~041 共 20 份合成报告已按规程删除（`outputs/.trash/` + ledger 原位墓碑），原始 issues 保留在 `run_result.json`。判定函数本身也有 5 项回归锁（`SelftestJudgeTests`：HIT / MISS / HIT_DUP / MISS_SEV / FP / GAP），避免"用例判定悄悄改了口径"——这与第 14 轮 `bruteforce_docx.py` 过期是同一类坑。

### 2026-10-28 · 新增 · 金标准 25 → 33 例，覆盖新增的事实断言与锚点行为

- **文件**：`tests/golden/cases/`（+4 阳性 +4 干净对照）、`tests/golden/labels/`（+4）、`tests/golden/baseline.json`、`tools/golden_eval.py`（无改动，沿用）
- **改了什么**：新增阳性 `p16`（试用期 12 个月）、`p17`（华为 5 亿台）、`p18`（合同缺争议条款，锚点断言）、`p19`（销量翻倍归因成功）；新增干净对照 `c11`（试用期 3 个月）、`c12`（华为 4840 万台）、`c13`（华为 2019 年 2.4 亿台，year_ctx）、`c14`（内部进度 8/10）。
- **为什么**：金标准是"改规则的唯一标尺"，新加的两类断言（上限、年度口径）与两类行为（锚点、豁免）如果没有正反用例，下次重构只能靠感觉。
- **验证**：33 用例 / 21 标注 / **召回 100% / 精确率 100% / 误报 0**，基线已重存（`baseline.json` 同步为 33 例）。删掉中间 5 次试跑产物，`outputs/bench/` 只留最新一份。

### 2026-10-28 · 新增 · 事实库从 2 条扩到 7 条：把法条口径写成可核验断言

- **文件**：`knowledge/facts.json`（2 → 7 条断言）、`knowledge/kb_laws.md`（新增，人读版口径）、`config/knowledge_version.txt`（1.0.0 → 1.0.2）、`tests/test_fact_library.py`（新增）
- **改了什么**：按原 schema（`entity/attr/type/expected/tolerance/note/exclude_ctx`）补入 5 条法条断言——
  `个人信息保护法`（2021 通过/施行）、`数据安全法`（2021）、`网络安全法`（施行 2026，同时 `exclude_ctx` 排除「数据安全管理条例」）、
  `网络数据安全管理条例`（2025 施行）、`生成式人工智能服务管理暂行办法`（2023 公布/施行）；每条 `note` 写清公布日/通过日/施行日，
  不是"年份应为 X"这种无法复查的结论。
- **为什么**：用户的判断很直接——**哈希链、台账、自学习、500 项回归都建好了，事实审的弹药只有 2 条豆包断言**。
  任何不涉及豆包的文本，事实审要么哑火，要么退回模型的参数记忆判真假；一旦如此，"独立裁判"就破产了，
  它和通用 agent 的差别只剩一条哈希链，而哈希链只能证明"记录没被改"，证明不了"记录是对的"。
- **验证**：`tests/test_fact_library.py` 12 项 OK（含「note 必须带可核验时间/口径」）。
  同一份 `targets/uploads/…智能招聘平台…报告.docx`（111 段）：扩库前匹配断言 **0**（不含豆包，事实审等于没跑），扩库后 **4 / 7**。

### 2026-10-28 · 修复 · 事实年份跨句偷日期：40 字窗口 → 同句最近制

- **文件**：`review.py`（`_sent_start` / `_sent_end` 新增；date 分支重写）、`tools/regex_check.py`（`DATE_RE` 支持 ISO `YYYY-MM-DD`）
- **改了什么**：删掉「取实体前 40 字窗口里第一个日期」的写法，改成在**实体所在句**内取离实体最近的日期；
  同句没有日期就不判，并在 decision 里写明「同句内无日期（宁漏不误）」。
  `DATE_RE` 原来只认「2024 年 3 月」，`2026-01-01` 这种 ISO 写法整个漏掉，一并补上。
- **为什么**：语料里三条法规挤在同一段，窗口会把隔壁条目的日期偷过来——
  「网络数据安全管理条例，2026-01-01 施行」被读成 2021（拿了个保法的），正确文本被判错、错误文本反被判对。
  事实审是 blocker 级，一次跨实体误判就足以让人放弃整份清单。
- **验证**：9 组突变 —— 写错年份 5 例全中（条例 2026 / 个保法 2022 / 生成式办法 2024 / 豆包推出 2022 / 豆包 MAU 10 亿）；
  同段三法规全对 → 0 条；同段三法规错 1 处 → 只报那 1 条；豆包写对 → 0 条；实体与日期跨句 → 0 条。

### 2026-10-28 · 新增 · 四个确定性检查器 + LOGIC-009，吃掉 logic 类漏检

- **文件**：`tools/arith_check.py`（6 → 10 个检查器）、`rules/logic.yaml`（新增 LOGIC-009）、`tools/rule_exec.py`（登记执行方式）、`config/rules_version.txt`（→ `v1-2026-10-28-a3`）
- **改了什么**：新增 `check_signed_direction`（方向词 + 负号同现）、`check_share_sum`（占比分项和 ≠ 100%）、
  `check_unit_convert`（「1.2 亿（即 1200 万）」量级换算错）、`check_period_clash`（同句同口径反向断言），
  四个都进了 `CHECKS`；LOGIC-009 按规矩登记为 `arith_check·方向词与负号同现`（未登记 = 报告里标「仅提示」，等于虚报覆盖面）。
  每个检查器都带反向护栏：方向词与负号必须**紧邻**（「增长率为 -3.4%」不算）、占比必须有语境词 + 合计词、
  换算只认「即/等于/折合/=」等价式、口径打架必须**同口径**且主语相同。
- **为什么**：用户亲自标的 4 条漏检全是 logic 类，且全是形式自相矛盾——「同比下降 -3.2%」「45%+35%+30%=110%」。
  这类问题**根本不该让 LLM 猜**：LLM 负责语义，算术和形式自洽交给脚本，误报率近零、召回立竿见影。
- **验证**：18 例快速验证全绿（12 正例 / 6 负例）。反向样本「占比 40%、30%、35%，合计 100%」证明
  `check_sums` 查不出来（分项和 105 与合计 100 自洽），必须靠 `share_sum` 单独判「占比之和 = 100」。

### 2026-10-28 · 新增 · 金标准测试集 25 例 + golden_eval 驱动 + 三把防假绿灯的锁

- **文件**：`tests/golden/`（cases 25 / labels 15 个共 17 条标注 / baseline.json）、`tools/golden_eval.py`（新增）、`tests/test_golden_suite.py`（新增 9 项）、`.gitignore`（`tests/` → `tests/*` + `!tests/golden/**`）
- **改了什么**：15 条阳性（含 `annotations.jsonl` 里 4 条真实漏检）+ 10 条干净对照，标注用 `eval_review` 的 AND-of-OR 格式；
  驱动脚本输出 recall / precision / 误报并与 `baseline.json` 比较（退出码 1 = 变差）；
  三把锁：① 每条标注必须**真能命中**（防止正则写错变成空断言）② 干净对照必须真的 0 issue ③ 基线用例集必须与磁盘一致。
  `.gitignore` 把 `tests/` 改成 `tests/*`，单独捞回 `tests/golden/`——金标准集是判定基准（数据），必须随仓库走。
- **为什么**：只有 4 条标注、没有标准答案集，改一条规则根本不知道是涨了召回还是误伤了干净文本，自学习闭环在空转。
  另外 `bruteforce_docx.py` 已经演过一次"脚本过期 → 27 PASS 假红"，金标准集同样不能只靠记性。
- **验证**：25 用例 / 标注 17 命中 17 → **召回 100% / 精确率 100% / 误报 0**，单轮 2.8s（无 LLM）。
  这套锁当场咬出我自己的错：第一版"干净对照" `c09` 写 360+210+480=**1080**（实际 1050），被 LOGIC-001 报出——
  不是规则误报，是我的干净样本算错了。改成 1050，错的那条升为阳性用例 `p15_sum_wrong_1080` 并加标注。

### 2026-10-28 · 新增 · 事实库进 prompt + FACT-001→FACT-002 硬守门（禁止凭记忆判真伪）

- **文件**：`tools/llm_review.py`（`render_facts` / `load_facts` / `DIM_FOCUS["fact"]` / `build_messages(facts=)` / `validate(dim=,facts=)` / `review_dimension(facts=)` / `_cache_key(facts_sig)`）、`review.py`（`llm_facts` 注入 + limitations 自报边界）
- **改了什么**：事实维度 prompt 里注入 FACTS 区块（7 条断言 + 三条硬约束：只准引用区块内实体、区块外不得断言真伪、命中 `exclude_ctx` 不比对）；
  校验层加兜底：FACT-001 的 quote 里**一个事实库实体都没有**时改判 FACT-002（缺可核验出处），evidence 前置说明并写进 `dropped` 留痕，严重度仍取规则库；
  语义缓存键加入事实库签名；`limitations` 新增「事实审证据仅限库内 N 条，库外只判缺出处」。
- **为什么**：只填库不够——库没进 prompt，模型仍然拿参数记忆判真伪。而且事实库改了而缓存不失效，等于拿旧证据判新文本。
  改判而不是丢弃的理由：**"这个数字我核不了"是可交付的结论，"这个数字是假的"不是。**
- **验证**：`tests/test_fact_library.py::FactsGuardTests` 4 项 —— 命中库内实体仍是 FACT-001；库外实体（ChatGPT 月活）改判 FACT-002 且留痕；
  改判后严重度等于规则库 FACT-002；不传 facts 的旧调用行为不变。`FactsPromptTests` 4 项 —— FACTS 区块只进事实维度、缓存键随事实库变。

### 2026-10-28 · 修改 · 硬编码事实断言数改动态；自学习门禁用例改判「已被 LOGIC-009 覆盖」

- **文件**：`tests/test_review.py`、`tests/test_api_contract.py`、`tests/test_learning.py`
- **改了什么**：两处 `assertEqual(fact_assertions_total, 2)` 改成从 `knowledge/facts.json` 动态读条数；
  `test_narrow_pattern_passes_gate` 拆成两项：一项断言「环比增长 -1.8%」候选**被冗余门禁拒绝且理由点名 LOGIC-009」，
  另一项用 ROOT 内合成语料证明「确定性规则覆盖不到的新表述仍能通过门禁」。
- **为什么**：扩库后两处硬编码必然失败（写死数字正是 MEMORY 里记过的坑）；自学习用例的翻转不是回归——
  2 处命中现在全由 LOGIC-009 覆盖，冗余门禁正确地拒绝了重复学习的候选，**这是 ② 生效的证据**。
- **验证**：全量 `py -3 -m unittest discover -s tests` → **501 项 OK / 190.7s**（原 500：新增 golden 9 + fact 12，learning 1 拆 2）；
  `tests/test_learning.py` 37 OK；docx 暴测仍 31 PASS / 0 FAIL。

# 2026-10-27

### 2026-10-27 · 修复 · docx 暴测脚本口径过期（27 PASS 假红），并把它本身锁进回归

- **文件**：`tests/bruteforce_docx.py`、`tests/test_bruteforce_regressions.py`（新增 `DocxFormatBruteforceHarnessTests` 2 项）、`暴力测试·docx格式审查·输入输出报告.md`（新增 §7 复核轮）
- **改了什么**：
  1. 脚本顶部加薄包装 `SPEC = format_spec.get("course-report-v1")["check"]` +
     `run_all(info, rids=None) → docx_format_checks.run_all(info, rids or ALL, SPEC)`，
     20 个调用点全部改走它；脚本内自此**不含任何阈值数字**（阈值只在 `config/format_specs.yaml`）；
  2. `a4` 期望反转：从「畸形 XML 抛解析异常即算过」改为「必须抛 `ValueError` 且文案含『损坏』，
     绝不能漏出 `ET.ParseError`」；
  3. `main()` 增加 `groups_only` / `write_report` 形参，并在开头 `RESULTS.clear()`——
     让单测能只跑无副作用的 A+B 组（C 组会往 `outputs/` 写真实报告，不能进 unittest）；
  4. 新增两项回归锁：`test_ab_groups_all_green`（A+B 必须 31 项全绿且用例数不得少）、
     `test_spec_carries_thresholds`（包装器必须带 5 个关键阈值，合规段落不得报版式违规）。
- **为什么**：用户复核这份报告时重跑脚本，得到 **27 PASS / 9 FAIL**，报告结论不可复现。
  根因不是产品代码：`run_all` 已改成 `run_all(info, ridset, spec)`，脚本仍传两个参数，
  `spec=None → {}`，而「规格里没写的项不检查」是既定设计——于是 B 组 8 条判定被静默跳过
  （B1/B2/B5/B7/B9/B11/B13/B16），断言落空。危险在于镜像情形：同样的空跑会让**反转的断言变绿**。
  另外 A4 在 `ET.ParseError → ValueError` 改造后与旧期望正好相反，修好反而报红。
  脚本文件名不是 `test_*`，不在 `unittest discover` 收集范围，这是漂移能潜伏一整轮的结构性原因。
- **验证**：`py -3 tests\bruteforce_docx.py` → **36 PASS / 0 FAIL / 4 OBS-INFO**（修复前 27/9）；
  A 组 14、B 组 17 全绿；新增 2 项单测 OK；全量 `unittest discover -s tests` → **479 OK / 207s**（原 476）。

### 2026-10-27 · 修复 · 删除报告只认 .json，中断后留下清不掉的孤儿 .md

- **文件**：`server.py`（`_delete_report` 存在性判断）、`tests/test_api_contract.py`（新增 1 项）
- **改了什么**：存在性判断从「`{rid}.json` 存在」改为「`.json` 与 `.md` **都不存在**才算未找到」；
  只剩 `.md` 时照常移入 `outputs/.trash/` 并打 ledger 墓碑。
- **为什么**：本轮清理暴测报告时后台进程被杀，`R-20261027-006.json` 已进回收站、`.md` 还留在 `outputs/`；
  再调 `DELETE /api/report` 直接报「未找到报告」，那个 `.md` 从此没有任何接口能处理。
  删除本身是「移 json → 移 md → 移 logs → 打墓碑」四步，不是原子操作——判存在性不能只看第一步的产物。
- **验证**：新增 `test_delete_cleans_orphan_md_left_by_interruption`（先删 json 再删，断言 `moved` 含 `.md` 且文件消失）；
  `tests.test_api_contract` → **148 项 OK / 161s**；`tools/integrity.py verify` → **182 条记录链完整**（+4 墓碑）。

### 2026-10-27 · 新增 · 全系统非针对性暴力测试（113 项，6 个面）

- **文件**：`tests/bruteforce_global.py`（新增）、`tests/bruteforce_global_report.json`（产物）、`tests/test_bruteforce_regressions.py`（新增 10 项）、`暴力测试·全系统·20261027.md`（新增报告）、`README.md` §34
- **改了什么**：新写一个可复跑的全局压测脚本，不预设靶子，从 6 个面同时压系统：
  A 输入模糊（22 份畸形/极端输入走完整 `run_one`：空文件、单字符、只有 BOM、只有零宽字符、夹 NUL、纯标点、12 万字符单行/多段、emoji+扩展 B 区、怪文件名、5 类不可读目标）；
  B 报告不变式（140 份历史报告逐条查字段/severity 域/quote 逐字溯源/覆盖率算术/裁定一致性/完整性链，旧 schema 报告单独按历史契约判）；
  C 配置对抗（28 种写坏配置：scenario / format_spec / agent manifest / rules / 版本文件）；
  D API 模糊（42 项：坏 JSON、错类型、穿越、超限 body、跨站头、未知路由、20 并发、滥用后存活）；
  E 并发与确定性（6 路并发 + 同输入三跑）；
  F 摄取对抗（bomb.docx、空/炸弹 PDF、PNG、旧格式、zip 成员超限/穿越/嵌套、超长文本截断）。
  全部在临时沙箱里跑（`outputs/logs/ledger/targets` 整体重定向），不碰真实台账。配套 10 项回归测试把本轮每个修复钉死。
- **为什么**：用户要求「针对该系统进行不针对性、全局性的暴力测试」。前几轮都是**已知功能**定向打（docx 格式、目的、对照矩阵），能证明单个功能对不对，证明不了"系统整体还站得住"。这轮改查**契约**：是否崩溃 / 是否静默降级 / 报告是否自洽 / 链是否完整 / 结论是否可复现——这五条与具体规则无关，任何改动都可能破坏。
- **验证**：逐组跑共报出 **9 条 FAIL**——6 条是系统真缺陷（见下一条），3 条是测试脚本自身的问题（临时目录在 ROOT 外触发 `relative_to`、`urllib` 在 413 后 `e.read()` 自己抛、用新契约判 6 份旧 schema 报告），已一并修正并写进报告 §4.1。修复后全量 **113 PASS / 0 FAIL / 2 OBS**，单轮 7.3s，连续 3 次结果一致。因沙箱隔离，真实 `ledger` 未被写入，仍为 **174 条链完整**。

### 2026-10-27 · 修复 · 并发写台账把 hash 链写断 + 4 处「静默降级/异常未翻译」

- **文件**：`review.py`（台账锁 + 读取异常兜底）、`tools/rules_store.py`（规则文件结构校验）、`tools/docx_extract.py`（XML 解析错误翻译）、`tools/ingest.py`（截断 vs 空文档的表述）、`tools/integrity.py`（版本号去 BOM）、`server.py`（PUT/PATCH 405 JSON）、`config/rules_version.txt`（去 BOM）
- **改了什么**：
  1. **台账并发**：新增 `_acquire_ledger_lock()` / `_release_ledger_lock()`，用 `O_EXCL` 锁文件把「读 prev_hash → 算 report_hash → 追加一行」包成原子段；陈旧锁（mtime>60s）自动回收，等待 30s 超时明确报错；
  2. **异常翻译**：`docx_extract.extract` 把 `ET.ParseError` 翻成 `ValueError("Word 文档结构损坏…")`；`run_one` 在 `(ValueError, OSError)` 之外再加 `except Exception` → `UnreadableTargetError`，并把异常类型写进消息；
  3. **规则文件**：`load_all_rules` 顶层非列表、条目缺 `rule_id` 一律 `ValueError` 并点名文件/序号（原来 `continue` 静默跳过 = 整类规则凭空消失）；
  4. **版本号**：`integrity.rules_version()` 改 `utf-8-sig` 读 + `lstrip("\ufeff")`，并清掉 `config/rules_version.txt` 里的 BOM；
  5. **摄取表述**：`ingest.extract_docx` 记录被截断成员，正文为空且发生过截断时告警改为「超出上限已截断，疑似压缩炸弹或结构损坏，不要当作空文档处理」，不再谎称「内容为空」；
  6. **HTTP 方法**：`do_PUT/do_PATCH/do_OPTIONS` → 405 + JSON + `Allow` 头（原来落到基类，返回 501 + HTML）。
- **为什么**：全系统暴力测试一次挖出来的 6 个缺陷，共同点是**生产时无感、审计时才炸**：
  ① 6 路并发后 `verify_ledger` 报 `序号不连续 @line2`，而两份报告本身都自洽；
  ② `bomb.docx` 抛 `ParseError` 冒到进程顶部，CLI 打 traceback、服务端 500；
  ③ 规则文件写成对象 → 返回 0 条规则、零提示，报告"正常"但整类检查没跑；
  ④ 报告里 `rules_version` 首字符是 `\ufeff`，肉眼不可见却让版本比较/展示失准，且换个编辑器保存就会让全部历史报告变 stale；
  ⑤ `PUT /api/review` 返回 501（5xx）+ HTML，前端 `res.json()` 直接抛错；
  ⑥ 解压炸弹 docx 被告知「内容为空」——"读不出来"和"本来就没字"是两种处置，说错了等于替攻击者打掩护。
- **验证**：修复前后逐条实测——并发 6 路：`序号不连续 @line2` → `共 9 条记录，链完整`；`bomb.docx`：`ParseError: no element found` → `无法读取待审文件 bomb.docx：Word 文档结构损坏…`；规则文件写成对象：`0 条无提示` → `ValueError: 顶层必须是列表`；版本号：`'\ufeffv1-2026-10-27-a1'` → `'v1-2026-10-27-a2'`；PUT：`501 HTML` → `405 JSON`；bomb.docx 摄取告警：`["Word 文档内容为空"]` → `["…超出上限，已截断读取", "…疑似压缩炸弹或文件结构损坏——请人工确认，不要当作空文档处理"]`。`tests/test_bruteforce_regressions.py` **10 项 OK**，全量回归 **476 项 OK**。

### 2026-10-27 · 修改 · FORMAT 规则拆成「维度清单 + 可选的格式规格」，格式要求不再住进规则库

- **文件**：`config/format_specs.yaml`（新增）、`tools/format_spec.py`（新增）、`tools/docx_format_checks.py`（阈值全部改为 spec 驱动）、`rules/format.yaml`（desc 去数值 + `spec_keys`）、`review.py`（`run_one(format_spec=)` + 规则过滤 + 报告字段 + limitations + CLI）、`server.py`（`/api/format-specs` + 参数校验 + 子进程传参）、`web/index.html`、`web/app.js`、`web/style.css`、`config/rules_version.txt`（a1→a2）、`tests/test_format_spec.py`（新增 26 项）、`tests/test_docx_format.py`（显式传规格）、`tests/test_api_contract.py`（接口正则修字符集）、`README.md` §2/§33、`docs/API接口对照表.md` §2.9/§3.7
- **改了什么**：
  1. `rules/format.yaml` 六条规则的描述去掉所有具体数值（宋体/小四/2 字符/22 磅/两三级），改为"应符合所选格式规格"；新增 `spec_keys` 字段声明依赖哪些规格键；
  2. 新增 `config/format_specs.yaml`：`none`（默认，只判图表编号）+ `course-report-v1`（原课程报告数值），并附一份期刊规格的注释样例；
  3. 新增 `tools/format_spec.py`：`load/get/apply/public_list`，校验未知键名、数值必须正整数、缩进区间必须成对、`line_rule` 只能 exact/auto、id 唯一、default 必须存在；
  4. `docx_format_checks.py` 删除 `OK_EAST/SZ_XIAOSI/FIRST_LINE_OK/LINE_OK` 四个硬编码常量，`run_all(info, ridset, spec)` 由 spec 决定阈值；spec 缺某个键 → 该项直接不跑；
  5. `review.py`：只在目标是 `.docx/.docm` 时套用规格（没选就用 default `none`），位置在 `scenario` 之后、`set_rule_index` **之前**；报告新增 `format_spec{id,name,version,source,desc,criteria,disabled_rules,disabled_detail}`；CLI `--format-spec` / `--list-format-specs`；
  6. 前端左栏新增「格式规格」下拉（选项带"会判哪几项"），状态条新增绿色 `rs-fmt` chip（`none` 时灰色显示「版式：未提供标准」），技术参数行写明"判 N 项 / 未判"。
- **为什么**：用户截图质问「为何会写入规则库？我只是这次是这样的格式要求，下次是另一个，最后我选择哪个标准呢」。核查确认 `rules/format.yaml` 的 `source` 全是「课程报告格式要求 v1」，且阈值硬编码在 Python 里——换一份期刊标准只能改代码，规则库还会攒下多套互相矛盾的标准。根因是**层级放错**：格式要求是"这一次任务的参数"，不是"长期裁判尺度"。同时对默认行为做了取舍：不选规格 = 不判字体/字号/缩进/行距/标题层级（这些没有通用正确答案），并在 limitations 写明「未报出版式问题 ≠ 版式合规」。
- **验证**：
  - 真实 docx（`targets/uploads/…智能招聘平台…报告.docx`，111 个正文段）三套标准实测：不选规格 **0 条格式问题**（limitations 有说明）；`course-report-v1` **4 条**（字号 110 段 11pt / 缩进 10 段 / 行距 111 段 / 无标题样式）；另一份"11pt + 非固定行距 + 不限缩进"规格 **3 条**（缩进完全不判，字号只剩 1 段五号）。**同一批段落随标准从违规变合规**，证明判定跟着标准走而不是固定在代码里。
  - `tests/test_format_spec.py` **26 项 OK**（配置校验 9 / 规格驱动判定 5 / run_one 集成 3 / HTTP 接口 5 / 前端契约 3）；`tests/test_docx_format.py` **17 项 OK**；全量回归 **458 项 OK**。
  - 修 `FrontendParityTests` 正则字符集缺 `-`（`/api/format-specs` 被截成 `/api/format` 误报）。
  - `config/rules_version.txt` 升至 `v1-2026-10-27-a2`：`rules/format.yaml` 变了，历史报告会显示 stale（黄），属既定机制。

### 2026-10-27 · 新增 · docx 格式审查暴力测试（36 PASS / 0 FAIL / 4 OBS）

- **文件**：`tests/bruteforce_docx.py`（新增）、`tests/bruteforce_docx_report.json`（新增，测试产物）、`暴力测试·docx格式审查·输入输出报告.md`（新增，根目录）
- **改了什么**：新增 docx 格式审查的破坏性输入暴力测试脚本与报告，延续项目暴力测试口径（逐用例带「期望」+ PASS/FAIL/OBS 统计 + JSON 落盘）：A 提取层 14 例（损坏 zip/缺部件/畸形 XML/非法编码/嵌套表格/一万段/10 万段性能）、B 检查层 17 例（属性极端值与边界阈值：字号 0·-5·1000、缩进缇值边界、行距 0·auto·atLeast·None、outline_lvl 0·99、Heading9/99、9-10 字正文边界、22 半磅/40 字标题阈值、题注判定 12 边界、表格豁免）、C 集成层 3 例（run_one 端到端：合规出报告/损坏明确报错/空 body 出报告）、D 性能 2 例。
- **为什么**：用户要求「继续暴力测试」——延续每轮迭代对最新能力（docx 格式审查，上一轮真实问题测试交付）做破坏性输入验证的系列口径。
- **验证**：`py -3 tests\bruteforce_docx.py` → 36 PASS / 0 FAIL / 4 OBS-INFO；挖出 3 处真缺陷（F1/F2/F3，见下条修复记录）；端到端合成报告 R-20261027-002/003 已按规程删除（入 outputs/.trash + ledger 墓碑）；全量 unittest 440 OK。

### 2026-10-27 · 修复 · 暴力测试挖出的 docx 检查层/集成层 3 处缺陷（F1 空列表、F2 None 文本、F3 损坏文件崩溃）

- **文件**：`tools/docx_format_checks.py`、`review.py`、`tests/test_docx_format.py`（+3 项回归保护）
- **改了什么**：① F1：`check_heading` 两处 `paras[0]` 在空段列表时 IndexError——改为 `p0 = first_head or (paras[0] if paras else None)`，空文档回退「（文档首段）」（空文档无标题报 FORMAT-005 属合理语义）；② F2：段落 `text=None` 时 `p.get("text","").strip()` 抛 AttributeError（`dict.get` 默认值只在键缺失时生效）——全部 9 处文本读取点改为 `(p.get("text") or "")`；③ F3：损坏/非 docx 文件在 `read_target_text` docx 分支抛 ValueError，`run_one` 无兜底致 CLI traceback 崩溃、server 只拿到 stderr 尾巴——新增 `UnreadableTargetError(ValueError)`，`run_one` 捕获 `(ValueError, OSError)` 转明确报错（含文件名与原因），`main()` 打印「目标不可读：…」退出 1，不产出「看起来没问题」的空报告；server 既有非零退出兜底自动透传。
- **为什么**：暴力测试 C2 实测损坏 docx 让 run_one 整条崩溃——违反项目底线「对任意输入都给结论」（不可读输入要么给结论、要么明确报错，唯独不能裸 traceback）；F1/F2 是空/None 输入防御缺口。
- **验证**：`tests/test_docx_format.py` 17 项 OK（新增 `test_empty_paragraphs_no_crash`、`test_none_text_no_crash`、`test_broken_docx_raises_clean_error`，后者含「不产出任何报告」断言）；暴力测试 36 PASS / 0 FAIL；全量回归 **440 OK** / 146.263s；integrity ledger 174 条链完整。

### 2026-10-27 · 修改 · 公开推送清理：.gitignore 追加排除测试/报告/运行态并推送 GitHub

- **文件**：`.gitignore`、`CHANGELOG.md`（+ git 索引 `git rm --cached`）
- **改了什么**：① `.gitignore` 追加「公开推送补充排除」段：`tests/`、`targets/`、`config/llm_history.json` / `learning.json` / `report_order.json`、`docs/修复报告-*.md`、`docs/本地模型实测报告.md`、`docs/盲测协议.md`、根目录 `回归测试·*.md` / `对抗测试·*.md` / `暴力测试·*.md` / `格式合规测试·*.md` / `调教报告·*.md`、`测试结果面板/`、`裁定结果样本/`；② `git rm -r --cached tests config/llm_history.json` 解除 14 项历史跟踪（文件保留本地）；③ 新提交 `cc05de4`（feat: docx 格式审查 + 公开推送清理）推送至 `https://github.com/W-zai1/review-agent.git`（main：524c01a..cc05de4）。
- **为什么**：用户要求把当前版本上传到自己的 GitHub 账号（W-zai1/review-agent），并明确「不上传测试文件和相关报告」；此前 `tests/` 13 项与 `config/llm_history.json` 已在版本库中，根目录/`docs/` 的报告类 md 会在 `git add -A` 时进入推送——需要在公开仓库中彻底排除。
- **验证**：`git status` 复核暂存清单——无 tests/、targets/、报告类 md、运行态数据，仅源码+规则+前端+核心文档；`git push origin main` 返回 `524c01a..cc05de4 main -> main`（exit 0）；`git ls-tree HEAD` 复核无排除项。

### 2026-10-27 · 新增 · 真实问题测试交付报告《格式合规测试·真实Word文档·20261027.md》

- **文件**：`格式合规测试·真实Word文档·20261027.md`（新增）、`CHANGELOG.md`
- **改了什么**：新增真实问题测试的交付文档：以用户上传的真实课程报告 docx（网安1班_第十二组_N6_智能招聘平台…）对照教师课程规范（正文宋体/Times New Roman、小四、首行缩进 2 字符、固定 22 磅行距、两级或三级标题、图表编号与引用一致、不出现他人真实账号/密钥/完整联系方式）逐项核验，产出 8 行核对表 + 7 条异议明细 + 验证证据 + 已知边界。
- **为什么**：用户要求"根据实际问题进行测试，比如给出一个 word 文档，里面的格式是否按照要求来进行"——需要一份可直接阅读的格式合规结论，而不只是引擎报告。
- **验证**：结论与 `outputs/R-20261027-005.json` 逐条对齐（7 条异议：format 4 / fact 3）；配套证据：docx 格式单测 14 项 OK、全量回归 437 项 OK、integrity ledger 172 条链完整。

### 2026-10-27 · 删除 · 误报版报告 R-20261027-004（含"重复题注 2-1"假阳性）

- **文件**：`outputs/R-20261027-004.md/.json`、`logs/R-20261027-004/` → `outputs/.trash/`（+ `ledger/chain.log` 墓碑）
- **改了什么**：经 API `DELETE /api/report?id=R-20261027-004`（body 带 confirm 二次确认）删除；文件移入 `outputs/.trash/`，ledger 对应行原位打 `op=delete` 墓碑（seq/report_hash/prev_hash 不动），收藏表摘除该 id。
- **为什么**：R-004 是题注判定修复前的产物，把正文引用句"表 2-1 是本情境下…"误判为题注、误报"重复题注 2-1"（major）；留着会让浏览历史的人看到一份含假阳性的正式报告。
- **验证**：`outputs/` 已无 R-004 文件（仅 R-005）；`outputs/.trash/` 含 R-20261027-004.json/.md/_logs；`tools/integrity.py verify` 通过（ledger 172 条链完整，删除痕迹在链上）。

### 2026-10-27 · 修复 · 真实 docx 审查暴露的 5 处缺陷（None 排序 / 题注误判 / 标题判定 / 视觉字号 0 / declared_rules 口径）

- **文件**：`tools/docx_format_checks.py`、`review.py`、`tests/test_docx_format.py`
- **改了什么**：① `check_spacing` 对 `spacing_line=None`（继承默认）段落排序崩溃——排序键改为 `(kv[0][0] or 0, kv[0][1] or "")`；② 题注判定收紧：编号后剩余须为短行（≤60 字）、无句号分号、不以 是/为/指/表示/作/说明/用于/在/与/及/和/，/, 开头（真实题注用全角空格「表 1-1　标题」，正文引用"表 2-1 是本情境下…"不再误判）；③ 标题判定扩展：pStyle Heading/outlineLvl 直接算标题（原先漏判导致合规文档误报 FORMAT-005）；黑体短文本阈值 60→40（避免吞掉 40+ 字正文强调句）；④ FORMAT-005 视觉字号集合剔除无字号段（sz_set 为空 → 0 的脏值）；⑤ `coverage.declared_rules` 由"全库规则数"改为"当前审查维度集合内的规则数"——format 维只对 docx 激活，txt 审查不再误报"31 条中仅启用 24 条"。
- **为什么**：用户真实报告审查（首轮 R-20261027-004）暴露：继承行距段排序崩溃（TypeError）、"表 2-1 是…"正文引用被当题注（假阳性 major）、合规合成文档因无样式标题误报、全量回归 test_disabled_rule_is_excluded 因 format 规则加入打破"active = declared − disabled"不变量。
- **验证**：`tests/test_docx_format.py` 14 项 OK（含新增 pstyle 标题、长黑体正文、表格单元格豁免、题注误判回归用例）；`RuleStateTests + test_scenario + test_docx_format` 31 项 OK；全量回归 437 项 OK / 152.367s；重审真实 docx 产出 R-20261027-005（7 条异议，无假阳性）。

### 2026-10-27 · 新增 · docx 格式审查能力（结构提取 + FORMAT-001~006 版式规则 + format 维度集成）

- **文件**：`tools/docx_extract.py`（新增）、`tools/docx_format_checks.py`（新增）、`rules/format.yaml`（新增）、`tests/test_docx_format.py`（新增 14 项）、`review.py`、`agents/doc-review/manifest.json`（v1.3.0）、`tools/rule_exec.py`、`web/app.js`、`web/style.css`、`config/rules_version.txt`（v1-2026-10-27-a1）
- **改了什么**：① `docx_extract.py` 用 stdlib zipfile+ElementTree 按段落提取文本与版式属性（字体 eastAsia/ascii、半磅字号 sz、firstLineChars/firstLine、行距 line/lineRule、pStyle/outlineLvl、表格内外标记），并解析 theme1.xml minorFont(Hans) 与 styles docDefaults 作回退基准（第一版 `_walk` 只遍历 body 一层致段落全空，改为对一切容器元素递归）；② `docx_format_checks.py` 实现 FORMAT-001~006 确定性检查，正文段口径=非表格/非题注/非标题/文本≥10 字；③ `review.py` 读 `.docx/.docm` 改走 docx_extract、`detect_type_detail` 识别 docx、`DIM_FOR_TYPE["docx"]` 加 format 维度、`builtin:format` 处理器（结构解析失败只跳过格式维并记账）；④ manifest v1.3.0 新增 format 维度与 `.docx/.docm` 路由；⑤ 前端 web/app.js + style.css 加 format 维（cyan）；⑥ rules_version 升至 a1（改 rules 必须同步升版本，否则历史报告转 STALE 红）。
- **为什么**：用户要求用真实 Word 文档做格式合规测试——此前系统只把目标当纯文本/二进制读，`.docx` 进不了审查；规则库只有文字风格规则（STYLE-*），没有版式规则。
- **验证**：`tests/test_docx_format.py` 14 项全过（合成合规/越规 docx，含 theme 回退、继承行距、题注误判、表格豁免、Heading 样式、run_one 集成断言 docx 类型+format 维+FORMAT 确定性）；真实报告首轮审查 R-20261027-004（8 条，其中 1 条题注假阳性，见下条修复记录）；规则执行登记 FORMAT-001~006 为 deterministic。

### 2026-10-27 · 新增 · 第十二轮迭代回归测试报告（423 项全过 + 完整性 + API 冒烟）

- **文件**：`回归测试·20261027·第十二轮迭代.md`（新增）、`CHANGELOG.md`
- **改了什么**：新增测试报告，登记对当前迭代系统（scenario / CHANGELOG 守门 / 双向定位 / 落点居中 / NO_CONFLICT 修复 / 界面改版）的三层验证结果：全量 unittest **423 项 OK / 0 失败 / 145.352s**（test_api_contract 147、test_review 88、test_purpose 53、test_learning 36、test_blindspots_v6 35、test_match_matrix 33、test_scenario 16、test_changelog_guard 15）；`tools/integrity.py verify` 退出 0（ledger 170 条链完整，最新报告自洽，旧报告按设计 STALE）；`tools/changelog_guard.py` 退出 0；临时实例 `server.py --port 8901` API 冒烟全部 200（含新端点 `/api/scenarios`，`POST /api/llm/test` ok=true 911ms）。
- **为什么**：用户要求"根据迭代的系统进行测试"——第十二轮改动涉及审查尺度（关规则）、工程门禁（守门钩子）与前端交互（双向滚动/居中/改版），需要系统性回归确认无回归与断链。
- **验证**：回归日志 `tests/run_full_regression_20261027.log`（`Ran 423 tests in 145.352s` / OK，0 FAIL/ERROR）；integrity 与 guard 退出码均 0；冒烟探测结果逐条记录于报告；全程只读未污染 outputs/ledger。

### 2026-10-27 · 新增 · CHANGELOG 守门脚本 + pre-commit 钩子（把"每次增删改要登记"从约定变成拦截）

- **文件**：`tools/changelog_guard.py`（新增）、`.githooks/pre-commit`（新增）、`tests/test_changelog_guard.py`（新增 15 项）、`README.md`（§2 目录结构 + §32）、`.workbuddy/memory/MEMORY.md`
- **改了什么**：
  1. 新增 `tools/changelog_guard.py`：读 `git status --porcelain` / `git diff --cached`，若改动里有"值得登记"的文件（源码/规则/文档/配置/`.gitignore` 等）而 `CHANGELOG.md` 不在改动集合里 → 打印待登记清单并以退出码 **1** 拦截；取不到 git 时退出码 2 宽容放行（避免在非 git 环境里变成噪音）。忽略产物与缓存：`outputs/ logs/ ledger/ state/ .workbuddy/` 与 `__pycache__/`、`config/{favorites,report_order,llm.local,llm_history,learning}.json`、`_probe*/_dom*/_shot*`、`*.pyc/.log/.tmp`。
  2. 新增 `.githooks/pre-commit`：调用守门脚本的 `--staged` 模式；用 `core.hooksPath` 挂载，钩子随仓库走（`.git/hooks/` 不进版本控制）。
  3. 新增 15 项测试：纯函数口径（该登记的 12 个路径、不该登记的 20 个路径、rename 解析、`decide()` 三种结局）+ 4 项在**真临时 git 仓库**里跑子进程，确认真会拦（退出码 1）、登记后放行（0）、只动产物放行（0）、`--staged` 只看暂存区。
- **为什么**：上一轮已把"每次增删改必须写进 `CHANGELOG.md`"写进 `MEMORY.md`，但**约定靠记性执行，第三次改动就会被忘掉**。用户这轮再次强调"后续所有增删改都需要写入 md"，说明只靠自我提醒不够。守门脚本把约定变成退出码：漏登记时提交直接被拦，看到待登记清单，不存在"忘了"。
- **验证**：
  - `py -3 -m unittest tests.test_changelog_guard` → **15 项 OK**；
  - 真实仓库实测拦截：`git add web/app.js` 后跑钩子 → 退出码 **1**，输出"有代码/文档改动，但 CHANGELOG.md 没有对应条目"（已 `git reset` 撤销暂存）；
  - 空暂存区 → 退出码 **0**（只检查真正要提交的内容）；
  - 修掉一个真问题：`git status --porcelain` 默认把中文文件名转成 `\346\216\245` 八进制，加 `-c core.quotepath=false` 后才显示为 `docs/API接口对照表.md`，否则 `IGNORE_EXACT` 精确匹配会失效。

### 2026-10-27 · 新增 · 建立本变更记录（CHANGELOG.md）+ 格式契约测试

- **文件**：`CHANGELOG.md`（新增）、`tests/test_api_contract.py`（+`ChangelogContractTests`）、`README.md`（顶部指引 + §2 目录结构）、`.workbuddy/memory/MEMORY.md`
- **改了什么**：新建本文件并回填 2026-10-26 起的逐条记录（更早版本只留索引表）；新增 3 项契约测试锁格式：必须存在且有「登记约定」、每条必须有 `文件/改了什么/为什么/验证` 四字段、条目必须倒序。
- **为什么**：用户要求"每次增删改的内容写入到 md 文件，便于读取"——此前改动散在 README 主题章节（讲设计）、`.workbuddy/memory`（讲坑）和 `docs/修复报告-*`（讲某一轮），没有一个"按时间可查的流水"。
- **验证**：`ChangelogContractTests` 3 项 OK；全量回归 408 项 OK。

### 2026-10-27 · 修复 · 报告里出现「此处表述一致」的 issue

- **文件**：`tools/llm_review.py`（prompt 第 8 条 + `NO_CONFLICT_RE`/`CONFLICT_DENY_RE`/`_tail_sentence` + `validate()`）、`tests/test_api_contract.py`、`tests/test_review.py`、`README.md` §31
- **改了什么**：
  - prompt 补一句：比完两端结论是「表述一致 / 不构成矛盾 / 只是依据不足」的**不要输出**；
  - `validate()` 增加「自证无矛盾」判定：`NO_CONFLICT_RE` 命中 evidence **末句**、且 `CONFLICT_DENY_RE` 不命中 → 进 `retracted`（报告「已排除」区），不静默丢；
  - 反义护栏：`不一致|不相符|前后矛盾|与…矛盾` 一命中就保留原条目（反义表只加不删）。
- **为什么**：`R-20261027-001` 里一条 `LOGIC-002 / minor`，evidence 最后写着"此处两处表述一致…属时间线依据不足而非直接矛盾"——一条写着"没问题"的问题，用户直接质问"这个逻辑存在的必要在哪"。规则要留（`arith_check:weekday/duration` 是硬判据），错在模型把"核完能对上"当成命中。
- **验证**：拿该报告真实 evidence 回放 → 0 issue / 进已排除；4 个反例（`不一致`、`一致+但矛盾`、`前后矛盾`、`未标注入学`) 全部保留；全量 **405 项 OK**。

### 2026-10-27 · 修改 · 跳转落点从「贴模块顶部」改为「屏幕正中间」

- **文件**：`web/app.js`（+`tests/test_api_contract.py`、`README.md` §30）
- **改了什么**：
  - 新增 `viewportBounds(host)` / `centerScrollTop(rect, host)`：目标中线对齐**可视区中线**，`rect.height ≥ viewH*0.7` 的长片段才退化为顶部对齐；
  - `revealInViewport()` / `ensureMarkInViewport()` 判定从「是否被裁切」改为「**中心偏离中线**」（舒适带视口 12%~15%）；
  - `jumpToIssue()`：`revealInViewport($("#annotatedBlock"))` → `revealInViewport(mark, { skip: box })`；
  - 新增 `settleInBoxToViewportCenter()`：落位稳定**之后**把「标注中心 vs 屏幕中线」残余差补到框内滚动，两条护栏（中线必须落在框内、不得越过可滚最大值）。
- **为什么**：点「定位 →」后句子贴在卡片最上沿，长句首行还被卡片标题压掉；`#annotatedText` 在卡片里本身不居中（框中线比屏幕中线高 50px+），"居中在框内"≠"居中在屏幕上"。
- **验证**：Edge headless 探针，`R-20261026-011` 6 条问题 × 2 种宽度：1440 全 −13~−14px、1000 全 +28px（刻意对齐导航条以下正文区中线），目标全部在框内；修复前是 300px 量级偏上。全量 403 项 OK。

### 2026-10-27 · 修改 · 裁定结果界面改版（对齐「简洁版」样板）

- **文件**：`web/index.html`、`web/app.js`、`web/style.css`
- **改了什么**：中栏重构为「状态条 → 审查目的 → 审查裁定（三卡）→ 要求对照矩阵 → 原文证据 → 原文标注 → 问题汇总 → 问题清单 → 边界/已排除 → 技术参数」四主模块 + 四明细；`renderReport`/`renderPurpose` 重写；证据默认折叠，点裁定卡的 E 编号 chip 自动展开并高亮；`.result-card` 局部 `--rs-*` 变量 + `.rs-sec` 统一外壳，窄屏表格 `overflow-x:auto` 收在模块内。原有 id（resultView/verdictBanner/purposeBlock/issueList…）全部保留。
- **为什么**：需求是"每次审查完成后直接出现样板那样的简洁裁定界面，并按模块美化"。
- **验证**：`node --check` OK；契约测试 140 项全过；headless Edge 截图（桌面/长图/420px 窄屏）逐块核验无溢出错位，截图存 `裁定结果样本/_shots/`。

### 2026-10-27 · 修复 · 宽屏双向定位断链（点原文标注跳不到汇总表）

- **文件**：`web/app.js`（`scrollHost` / `scrollHostTo` / `revealInViewport`）、`tests/test_api_contract.py`、`README.md` §29
- **改了什么**：新增 `scrollHost(el, skipEl)` 找最近**真的能纵向滚**的祖先（`overflowY` ∈ auto/scroll/overlay **且** `scrollHeight > clientHeight + 1`），`scrollHostTo()` 滚它并在 200ms 后校验落点（差 >2px 直接 `auto` 写死）；`ensureMarkInViewport` 传 `skip=#annotatedText`。
- **为什么**：宽屏 ≥1101px 下 `.card` 是 `max-height + overflow-y:auto` 的独立滚动容器，整页 `scrollHeight === innerHeight`，`window.scrollTo()` 一点都滚不动——行确实高亮了，页面纹丝不动。
- **验证**：探针实测 cardTop 0→1961、rowTop 93（卡内）；反向仍正常（1366→890，markInBox true）。新增 `test_reverse_jump_scrolls_inner_container`。

### 2026-10-27 · 新增 · 审查情境（scenario）：不同用途关不同规则

- **文件**：`config/scenarios.yaml`（新增）、`tools/scenario.py`（新增）、`review.py`、`server.py`、`web/index.html`、`web/app.js`、`tests/test_scenario.py`（新增）、`docs/API接口对照表.md`、`README.md` §28
- **改了什么**：情境配置版本化 + 载入/校验/应用；`resume-screening` 关 `FACT-001`（具体标识可复现）、`FACT-002`（引用可定位），其余维度不动；过滤点放在 `merge_agent_rules()` 之后、`set_rule_index()` **之前**；报告新增 `scenario{id,name,version,disabled_rules,disabled_detail}` 且 limitations 首位写"关了哪几条 / 未报出 ≠ 已核实"；新增 `GET /api/scenarios`、`POST /api/review/start {scenario}`、CLI `--scenario` / `--list-scenarios`、左栏下拉 + 状态条蓝色 chip；未知情境 / 全关光 → 报错不起任务。
- **为什么**：用户指出简历初筛场景下"是否真的考过四六级"这类事实问题无足轻重——HR 不会一份份去核。全场景共用一套规则等于逼人读噪声。
- **验证**：同文件同目的对比：通用 6 条 warn（含 CET-4 / 项目地址）vs 初筛 3 条「未发现—不等于通过」，资质类全消失，参与规则 25→23。新增 16 项情境测试，全量 402 项 OK。

---

# 2026-10-26

### 2026-10-26 · 修改 · 历史列表改「纯时间倒序」+ 逐条收藏 + 标题 ⭐ 筛选（v0.6.3）

- **文件**：`server.py`、`web/index.html`、`web/app.js`、`web/style.css`、`config/favorites.json`（新增）、`.gitignore`、`tests/test_api_contract.py`、`README.md` §17、`docs/API接口对照表.md`
- **改了什么**：排序改 `sort(key=(timestamp, review_id), reverse=True)`；**拖拽排序整套删除**（`draggable` / `⋮⋮` / `moveReportInList` / `persistReportOrder` / `POST /api/reports/order` / `_save_report_order` / `_report_order`），`config/report_order.json` 文件保留但不读；新增 `POST /api/reports/favorite`；每行 `☆/★`，标题 `★ 收藏（N）` ↔ `← 全部（总数）`，纯前端筛选 `favoritesOnly`。
- **为什么**：只要时间先后（最新在最上）+ 可单独收藏。拖拽与时间倒序不可能同时成立，两套排序必然打架，所以删而不是共存；`report_order.json` 保留是因为删文件等于丢用户数据。
- **验证**：`/api/reports` 顶部 `011 → 010 → 007 → 047…`；截图核对收藏态标题「收藏（2）」、取消后行消失计数变 1；`favorite` 必须真布尔（拒绝 `"true"`/`1`）、幂等、文件损坏当空、删除报告时摘除。全量 383 项 OK。

### 2026-10-26 · 修改 · 问题汇总摘要改为「默认全文直出」

- **文件**：`web/app.js`、`web/style.css`、`tests/test_api_contract.py`、`README.md` §23.4
- **改了什么**：摘要单元格直接 `esc(brief)` 全文渲染；删净 `SUM_BRIEF_CHARS` / `sumBriefOpen` / `toggleSumBrief` / `.sum-more` / `.clamped` 及事件里的两个分支；CSS 只剩 `.sum-brief-cell{min-width:240px}` + `.sum-brief{line-height:1.65}`。
- **为什么**：用户要求"不要任何按钮，一眼看完"。演进三版（`slice(0,110)` 硬截断 → 折 3 行 + 展开按钮 → 全文直出），每版都是上一版的失败：截断丢结论；折叠栏窄时 3 行只有二三十字，还是要点。**"展示不全"优先级最高的解法通常不是加交互而是别裁。**
- **验证**：`test_summary_brief_is_always_fully_visible` 断言禁止残留上述标识、必须存在全文渲染；Edge headless 截图 7 条全部读到句末、无任何按钮。全量 383 项 OK。

### 2026-10-26 · 修复 · 「历史报告不更新」= 排序把新报告压到末尾 + 页面不刷新

- **文件**：`server.py`、`web/app.js`
- **改了什么**：`_reports()` 排序去掉 `rank.get(rid, len(rank))`（未列出的报告被当成最大 rank → 全部沉底），改为时间倒序；前端新增 `refreshReports({force})` + 标题「⟳ 刷新」+ `focus`/`visibilitychange` 自动刷新（8s 节流，服务端每次要读全部报告 JSON，不能轮询）。
- **为什么**：跑完审查，右栏顶部还是旧报告。两层原因：排序把新报告压到 140 条列表末尾；前端只在启动/完成/删除时 `loadReports()`，CLI 或另一个窗口跑完本页不知道。另有一条隐性问题：8000 端口跑的是旧进程。
- **验证**：修后顶部 `011 → 010 → 007 → 047`；新增 `test_new_report_not_in_order_sorts_first` + `test_history_list_can_be_refreshed`。注意改 `server.py` 必须重启控制台，`app.js`/`style.css` 需 Ctrl+F5。

### 2026-10-26 · 修复 · 问题汇总摘要被截断（现象修复，后被"全文直出"取代）

- **文件**：`web/app.js`、`web/style.css`
- **改了什么**：先做「全文进 DOM + CSS 折 3 行 + 展开按钮」。
- **为什么**：截图里三块文字都断在句子中间且**连省略号都没有**——根因是 `renderIssueSummary()` 的 `slice(0, 110)`，不是 CSS clamp。
- **验证**：用 R-20261026-011 复算 `slice(0,110)`，与截图三段逐字对齐（ISS-0005/0006/0004）。此方案次日被「全文直出」取代（见上）。

### 2026-10-26 · 修复 · 第七轮：审错对象 / OCR 标准失真 / 隐私规则脱离语境

- **文件**：`server.py`、`review.py`、`web/app.js`、`tools/regex_check.py`、`tools/ocr.py`、`tests/*`、`docs/修复报告-第七轮.md`（新增）
- **改了什么**：
  - `#targetSelect` 补 `change` 监听（原来是死控件：界面显示 A，`state.selected` 还是 B，后端审的是 B）；
  - `/api/targets` 加 `role`/`reviewable`，`*.labels.json` 标注文件标「标注·不可审」并默认不选，`_resolve_target` 硬拦；
  - 目的文件是图片时 OCR 风险进 `limitations`（新增 `_assemble_purpose` 第三返回值 `quality[]`），不进正文、不做成句尾括注；
  - `SAFE-003` 加语境豁免：同句含 `联系方式|电话：|手机：|邮箱：` 不报（**只豁免手机号**），模型命中一并让位并进 `excluded`。
- **为什么**：用户贴截图问"目的文件读取有乱码""为何有不相干的规则"，核出四个缺陷。`labels.json` 是 ground truth，逐条写着"某处有错"，被当正文审 → 9 条异议全来自它。
- **验证**：R-007(fail, 审标注) → R-010(warn, 审简历)；limitations[0]=OCR；目标 3/3 命中。全量 380 项 OK。

### 2026-10-26 · 修复 · 要求对照矩阵收口（F4 + 块标记）

- **文件**：`tools/match_matrix.py`、`tests/test_match_matrix.py`、`.workbuddy/memory/2026-10-26.md`
- **改了什么**：沿用 v1 53 条 / v2 26 条压测用例逐条修；空名归一「未命名」、畸形标记处理。
- **为什么**：矩阵暴力测试暴露"块标记格式"是最脆的一环（一行两标记 / 手写段伪标记 / 正文末尾恰为固定措辞）。
- **验证**：v1 53/0/5；v2 **26/0/3**；`test_match_matrix` 33 项；全量 367 OK；integrity 全绿。剩 3 条 OBS 需改块标记格式才能根治。

### 2026-10-26 · 新增 · 要求对照矩阵（双文档对比）

- **文件**：`tools/match_matrix.py`（新增）、`config/match_matrix.yaml`（新增）、`tests/test_match_matrix.py`（新增）、`docs/要求对照矩阵.md`（新增）、`web/*`
- **改了什么**：把"要求侧 / 响应侧"两份文档按块标记对齐，产出「命中 / 部分 / 缺失 / 超范围」四态矩阵，进报告与控制台。
- **为什么**：审核本质是"对照"，只给单侧问题清单答不了"要求有没有被覆盖"。
- **验证**：v1-2026-10-26-a1 基线 53 条用例，见 `docs/要求对照矩阵.md`。

### 2026-10-26 · 修复 · 「目标命中意识」缺陷（R-20261026-003 简历 + JD 截图）

- **文件**：`review.py`、`tools/purpose.py`、`web/app.js`
- **改了什么**：审查目的从"一句话"扩到"目标 → 证据 → 缺口"，缺目标时不允许给 `pass`；命中目标的问题单独标记。
- **为什么**：审出来的问题与招聘方真正关心的目标无关，读了等于没读。
- **验证**：见 `.workbuddy/memory/2026-10-26.md` 对应小节（R-003 复跑对比）。

### 2026-10-26 · 新增 · 审查台「审查目的」窗口支持多文件 / 多格式上传

- **文件**：`web/index.html`、`web/app.js`、`server.py`
- **改了什么**：目的文件上传与待审上传对齐，支持多文件、多格式（含图片 OCR）。
- **为什么**：判定标准往往不止一句话，可能是一份 JD、一张截图。
- **验证**：见 `.workbuddy/memory/2026-10-26.md` 对应小节。

---

## 历史一览（v0.2.x – v0.7.1，此前逐轮记录在 README 与 `docs/修复报告-*.md`）

| 版本 | 主题 | 详情 |
|---|---|---|
| v0.7.2 | 审查情境 + 宽屏双向定位 | README §28 / §29（本文件 2026-10-27 有逐条） |
| v0.7.1 | 向「目的」功能投暴力测试 | README §27 末 / `暴力测试·审查目的功能·系统漏洞报告.md` |
| v0.7.0 | 审查目的：一句话 → 目标 → 证据 | README §27 / `docs/审查目的.md` |
| v0.6.3 | 历史报告管理、收藏、盲区门禁 | README §17 / §26 |
| v0.6.2 | 摘要展示、规则学习面板 | README §25 / `docs/自学习闭环.md` |
| v0.6.0 | 自学习闭环：提案 → 门禁 → 受控晋升 | README §25 / `docs/自学习闭环.md` |
| v0.5.0 | 多 Agent 框架：判定标准成为数据 | README §24 / `docs/多Agent框架.md` |
| v0.4.3 | 原文标注视图（角标、双向定位） | README §23 |
| v0.4.2 | 规则集版本：区分「升级」与「篡改」 | README §22 |
| v0.4.1 | LLM 配置记忆与超时 | README §21 |
| v0.4.0 | 方向感：从「规则执行器」到「独立审查者」 | README §20 |
| v0.3.0 | PDF / Word / 图片 / 压缩包上传 | README §18 |
| v0.2.8 | 本地大模型真实测评 | README §16 / `docs/本地模型实测报告.md` |
| v0.2.6 | 常见服务商预设与本地模型 | README §15 |
| v0.2.5 | 四页控制台与配套后端 | README §14 |
| v0.2.4 | 控制台上传待审文件 | README §13 |
| v0.2.3 | 前端 LLM 配置面板 | README §12 |
| v0.2.2 | 大模型 API 接入 | README §10 / §11 |
| v0.2.1 | 首轮修复记录 | README §9 |

> 说明：v0.7.2 之前未逐条登记（当时只写 README 章节与 `docs/修复报告-*.md`）。
> 上表是索引入口，不是完整流水；从 2026-10-26 起改为逐条登记。
