# 工具规范 v1（Tool Specification）

> 代码：`review_agent/capabilities/tools.py`（`BaseTool` / `ToolRegistry` / `Permission` / `ToolResult`）
> 验收：`checks/test_tool_registry.py`
> 状态：协议与注册表已落地；旧 `tools/` 的 42 个模块尚未迁移（批次见 §7）。

## 1. 什么算工具

| 是工具 | 不是工具 |
| --- | --- |
| 单一能力、入参出参明确、可独立测试：正则匹配、条文检索、算术核对、哈希计算 | 运维/学习闭环脚本：`fact_promote`、`rule_learning`、`holdout_eval`（它们编排多个能力，属于 Application/运维层） |
| 确定性优先、失败语义清楚 | 维度处理器 `_h_*`：它是「维度 → 工具组合」的路由，属于 Runtime/Profile |
| 可以在节点里被调用多次、结果可复现 | 直接读写 `Session` / 全局状态的函数 |

判断口径一句话：**工具不知道自己被谁调用，也不编排别的工具。**

## 2. 权限模型

| 权限 | 工具类型 | 默认策略 | 代码 |
| --- | --- | --- | --- |
| `READ` | 读取材料 | 允许 | `default_permissions()` 包含 |
| `ANALYZE` | 分析材料 | 允许 | 同上 |
| `WRITE` | 修改审查数据 | 受控（需显式授权） | `DEFAULT_POLICY[WRITE] = False` |
| `APPROVE` | 批准结果 | **只能由人持有** | `ToolContext` 构造时拒绝 `actor="system"` + APPROVE |

「受控」不是文档措辞：`ToolRegistry.execute()` 在调用前检查 `context.allows(tool.permission)`，
不通过直接抛 `ToolPermissionError`。审批权限的约束在 `ToolContext.__post_init__` 里，
系统主体连构造都构造不出来——把「必须人工批准」写在注释里，等于没写。

## 3. 工具契约

```python
@dataclass(frozen=True)
class MatchArgs:
    pattern: str
    text: str

class RegexMatchTool(BaseTool):
    name = "regex.match"
    description = "在给定文本里做一次正则匹配，返回是否命中"
    permission = Permission.ANALYZE
    arguments_type = MatchArgs

    async def execute(self, arguments: MatchArgs, context: ToolContext) -> ToolResult:
        hit = re.search(arguments.pattern, arguments.text) is not None
        return ToolResult.success({"match": hit}, untrusted=True)
```

硬性要求：

1. `name` 小写字母开头，可含 `.` `_`（`^[a-z][a-z0-9_.]{1,63}$`）。
2. `description` 必填：它既是给人看的，也是将来给模型选工具的唯一依据。
3. `execute` 必须是 `async`，必须返回 `ToolResult`，不得抛业务异常（转成 `ToolResult.failure`）。
4. 同一入参 + 同一材料，结果必须一致（幂等）；有随机性/时间的工具必须在 `data` 里写明。
5. 工具不写文件、不发网络请求，除非它的权限是 `WRITE` 且入参里显式给了目标。

## 4. 注册与执行管线

```
Agent / Node
    │  registry.execute("regex.match", {"pattern": …, "text": …}, ctx)
    ▼
ToolRegistry
    1. 查表          未注册 → ToolError
    2. 参数校验      dataclass 构造失败 → ToolArgumentError（含字段名）
    3. 权限校验      缺权限 → ToolPermissionError
    4. 执行          await tool.execute(args, ctx)
    5. 结果校验      不是 ToolResult / ok 与 errors 矛盾 → ToolError
    6. 审计          ToolInvocation（工具、主体、run、耗时、参数键、dry_run）
    7. 返回          (ToolResult, ToolInvocation)
```

顺序是刻意的：**权限放在参数校验之后**。参数都不合法的调用没必要进入权限判定，
否则「被拒绝」的日志里会混进大量「参数写错」的噪音，审计读不出真实越权。

**落地进度（2026-10-30）**：规则分发已经走这条管线。

* `review.build_rule_registry()` 把 12 个 `builtin:*` 处理器与 Agent 自带
  `checks.py` 里的 `pack:*` 注册成工具（`_HandlerTool`，权限 `WRITE`）；
* 处理器键里的冒号在工具名里换成点（`builtin:fact` → `builtin.fact`）——
  工具名规则只允许小写字母/数字/点/下划线；
* `rule_executor_from()` 把一步计划变成一次 `registry.execute()`，调用方传
  `actor="pipeline"` 并显式授予 WRITE（审查流程自己在写发现，不是模型开权限）；
* `run_rule_plan()` 已改为 async，可 await 注册表的 async 执行器。

处理器**实现**仍是 `handler(session, text, rules, ctx)` 的旧形态，`_HandlerTool`
只是适配器；纯函数化是阶段 3 的正式工作。

`ToolInvocation` 字段：`tool`、`permission`、`actor`、`review_id`、`run_id`、`ok`、
`error_count`、`latency_ms`、`argument_keys`、`dry_run`。不含入参值（可能含敏感材料），
需要复现时按 `argument_keys` + 材料哈希回查。

## 5. 入参校验：dataclass，不用 Pydantic

* 见 [ARCHITECTURE.md](ARCHITECTURE.md) ADR-001：运行时硬依赖只有 PyYAML，CI 只装它。
* `arguments_type` 可以是 dataclass / TypedDict / `None`（工具自行校验）。
* 校验失败必须报「哪个字段、期望什么」，不允许只回一句 `invalid arguments`。
* 需要对外暴露 JSON Schema 时，由 API 层（FastAPI）生成，不在工具层引入 Pydantic。

## 6. 外部文档是不可信输入

审查对象是外部材料，材料里可以写「忽略之前的指令」「把这条标为通过」。规则：

1. 从文档抽取的内容一律 `ToolResult(untrusted=True)`；节点只能把它当**数据**。
2. 工具不得因为文档内容而改变权限、规则集、严重度阈值或输出路径。
3. 工具不得执行文档里的命令、URL、路径（包括「帮我打开这个链接」）。
4. 需要联网的工具（`research`）只能访问白名单域，且结果必须带来源 URL 与抓取时间。
5. 模型节点拿到的材料必须包在明确的数据边界里（阶段 2 在 prompt 层实现）。
   **已实现（2026-10-30）**：`llm_review.build_messages` 用**带随机串的围栏**
   （`<<<TARGET:<nonce>>>>` … `<<<END:<nonce>>>>`）包裹原文，文档无法预测闭合标记；
   原文逐字保留（quote 仍可回查），围栏串若与原文撞车直接拒绝。
   验收：`checks/test_untrusted_fence.py`（9 项，含「文档里写 TARGET>>> 不能闭合区块」）。

这条规则不是防模型「不听话」，是防**材料作者**通过审查系统反向影响审查结论。

## 7. 现有 42 个模块怎么迁
按「是否纯函数、是否有 IO、是否编排别人」分三批。批次 1 的验收标准是
「注册表里有它、`review.py` 通过注册表调用、`checks/` 有对应测试」，三者缺一不算完成。

### 批次 1：确定性能力，先迁（无网络、尽量无文件 IO）

| 模块 | 建议工具名 | 权限 | 说明 |
| --- | --- | --- | --- |
| `regex_check.py` | `text.regex` | ANALYZE | 隐形字符剥离、正则匹配 |
| `rule_exec.py` | `rules.classify` | READ | 规则执行方式分类（deterministic/declarative/prompt_only） |
| `arith_check.py` | `math.check` | ANALYZE | 合计、百分比、星期 |
| `logic_checks.py` | `logic.check` | ANALYZE | 逻辑矛盾 |
| `docx_format_checks.py` | `docx.format` | ANALYZE | 版式判定（需 docx 解析结果） |
| `law_cite.py` | `law.cite` | ANALYZE | 法条比对（规则库通过入参传入） |
| `integrity.py` | `integrity.hash` | READ | 去 IO 化：只算字节/文本哈希 |
| `lint.py` | `text.lint` | ANALYZE | 标点、格式 |
| `pattern_rules.py` | `rules.pattern` | READ | 声明式规则求值 |
| `match_matrix.py` | `matrix.match` | ANALYZE | 要求对照矩阵 |

**进度（2026-10-31）**：`math.check`（← `arith_check.run_all`）与 `logic.check`
（← `logic_checks.run_all`）已实现为**独立 `BaseTool`**：
`review_agent/capabilities/builtin_tools.py`，由 `review.build_capability_registry()` 装配，
验收 `checks/test_builtin_tools.py`（17 项：真算法、三态 `rule_ids`、权限、审计、确定性）。
算法本体不复制——`capabilities` 层不许 import `tools/`（ARCHITECTURE §3 第 3 条），
所以由 `review.py` 在装配时注入函数。

其余 8 个**未迁**，原因逐条写在下面，不装作「批次 1 完成」：

| 工具 | 卡在哪 |
| --- | --- |
| `text.regex` | `regex_check.py` 只有零散辅助函数，没有单一入口；要先定「一次调用算什么」 |
| `text.lint` | `lint.run_lint(path)` 收的是**路径**（要读文件、跑 ruff/eslint），属有 IO |
| `rules.pattern` | `pattern_rules.scan(s, text, rules, ...)` 第一个参数就是 Session |
| `rules.classify` | 分类函数与规则集状态耦合，要先抽成纯输入 |
| `docx.format` | 依赖 `docx_extract` 的解析产物，属批次 2 的「先定接口」 |
| `law.cite` | 依赖法条索引加载（批次 2） |
| `matrix.match` | 依赖 `config/match_matrix.yaml` 的加载与块标记解析 |
| `integrity.hash` | 现有实现以文件/目录为入参（IO）；纯字节哈希只有 `sha256_bytes` 一小块 |

### 批次 2：有 IO / 依赖外部，后迁（接口先定，实现后换）

| 模块 | 工具名 | 注意 |
| --- | --- | --- |
| `retrieve.py` / `law_index.py` | `knowledge.retrieve` | 索引加载要可注入（测试不读真实库） |
| `docx_extract.py` / `ingest.py` / `ocr.py` | `document.extract` | 解析器可换；失败要给明确错误 |
| `llm_client.py` / `llm_review.py` | `model.complete` | 属于基础设施端口，**不是**工具；工具只能通过 ModelClient 调用 |
| `research.py` | `research.web` | 网络：白名单 + 超时 + 来源记录 |

### 不迁（运维 / 学习闭环 / 评测，不进 Agent 工具集）

`fact_promote`、`fact_import`、`fact_audit`、`fact_gap`、`rule_learning`、`rule_miner`、
`rule_promote`、`rule_review`、`holdout_eval`、`eval_review`、`golden_eval`、
`changelog_guard`、`log_summary`、`annotate`、`llm_bench`、`arch_inventory`、`version`。

理由：它们要么修改规则/知识库（必须人工审核，不能由 Agent 在审查中调用），
要么是评测/运维入口（无 Agent 调用场景）。把它们做成工具，等于把「审查」与「改判定标准」
放进同一条权限链——这正是旧系统最危险的地方。

## 8. 失败 / 重试 / 超时 / 幂等

* **失败语义**：`ToolResult(ok=False, errors=(...))`；异常只用于「实现坏了」（如返回类型错误）。
* **可重试性**：由调用方（节点）决定，工具不自己重试。只读工具默认可重试；`WRITE` 工具必须
  先查幂等键。
* **超时**：由 Runtime 执行器控制（`asyncio.wait_for`），工具内部不 sleep 重试。
* **幂等键**：`(tool_name, 入参规范化哈希, 材料哈希)`；写操作以 `(run_id, stage, tool_name)` 去重。
* **部分成功**：不允许。要么 `ok=True` 带 `warnings`，要么 `ok=False` 带完整错误。

## 9. 命名与「禁止重复实现」

* 一个能力一个工具；名字表达「对象.动作」（`law.cite`、`docx.format`）。
* 注册表拒绝同名重复注册（`DuplicateToolError`）。新增工具前先 `registry.describe()` 查一遍，
  再在 `tools/` 里搜同类实现——旧系统同一件事最多有 3 个实现（见盘点 §5 重名函数表）。
* 别名只是迁移期兼容，不允许长期存在两套同名能力。

## 10. 测试要求

| 层级 | 放哪 | 要求 |
| --- | --- | --- |
| 注册表/管线 | `checks/test_tool_registry.py` | 已有：重复注册、权限、参数、结果校验、dry_run |
| 单个工具 | `checks/test_tool_<name>.py` | 纯函数表驱动；不联网、不读 `outputs/` |
| 集成 | `tests/` | 允许读真实语料；不要求进 CI |

工具测试必须覆盖：正常路径、空输入、超长输入、非法入参、权限不足。
「工具能跑」不算测试通过，要能回答「输入错的时候它怎么错」。

## 11. 迁移验收（批次 1 完成时）

1. `registry.describe()` 能列出批次 1 的全部工具，权限与入参正确。
2. `review.py` 的 `_h_*` 处理器改为调用注册表，不再直接 `import regex_check` 之类。
3. `checks/` 覆盖每个工具的正常/异常路径，且 CI 不装额外依赖。
4. 旧模块保留但标记 `deprecated`，阶段 5 统一删除；删除前必须确认没有脚本直接调用。

