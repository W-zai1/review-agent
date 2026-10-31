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

**进度（2026-10-31）**：批次 1 **10/10 全部迁移完成**，都实现为独立 `BaseTool`：

| 工具 | 算法来源 |
| --- | --- |
| `math.check` | `arith_check.run_all` |
| `logic.check` | `logic_checks.run_all` |
| `rules.classify` | `rule_exec.summarize` / `counts` |
| `integrity.hash` | `integrity.sha256_bytes` |
| `rules.pattern` | 新增纯核心 `pattern_rules.scan_hits`，旧的 `scan()` 改为委托 |
| `text.regex` | 新增 `regex_check.strip_invisible_with_map`（带原文位置映射） |
| `matrix.match` | `match_matrix.build`，配置由装配层注入 |
| `law.cite` | 新增 `law_cite.load_articles`，条文由入参注入 |
| `docx.format` | `docx_format_checks.run_all`，解析产物 `info` 可入参直传 |
| `text.lint` | `lint.run_lint`（外部 ruff/eslint + 密钥扫描） |

实现在 `review_agent/capabilities/builtin_tools.py`，由 `review.build_capability_registry()` 装配，
验收 `checks/test_builtin_tools.py`（66 项：真算法、三态 `rule_ids`、权限分级、审计、
确定性、坐标映射、配置/条文/解析产物注入、`skipped` 语义，以及 `rules.pattern` 与旧
`scan()` 的**差分一致性**）。
算法本体不复制——`capabilities` 层不许 import `tools/`（ARCHITECTURE §3 第 3 条），
所以由 `review.py` 在装配时注入函数。

**统一的注入口径**（十次迁移踩出来的同一条路）：IO / 外部命令 / 配置加载都收进
**注入的可调用对象**，工具本身只做「校验入参 → 调 → 归一结果 → 定性（成功/失败/skipped）」。
所以这十个工具在没有文件系统、没有网络、没有法条库、没有 ruff 的环境里都能被单测。

`rules.pattern` 的抽法值得单独记一句：`scan(s, ...)` 的第一个参数是 Session，
工具不该造一个假 Session 来跑。做法是把匹配语义抽成纯核心 `scan_hits(text, rules, ...)`
返回 `(参与匹配的规则数, [命中])`，`scan()` 只负责把命中写进 Session。
判定条件（pattern / require / guard / unless / 注释去噪 / 文档字符串去噪）仍然只有一份。

`text.regex` 同理：`strip_invisible` 只返回清洗后的文本，匹配偏移拿去切原文会错位
（少掉几个零宽字符），证据就回查不到原句。新增的 `strip_invisible_with_map` 与它
**共用同一个 `INVISIBLE_RE`**，只是多返回一张位置映射表；工具给出的 `start` / `end`
是原文坐标。

`matrix.match` 的卡点原本是「配置从哪来」：`build(..., cfg)` 本来就支持注入，
但 `cfg=None` 时会自己去读 `config/match_matrix.yaml`。做法是把 `load_config`
也当成注入项——工具拿到 `config` 就用，拿不到才让注入的加载器去读。
另外「没有要求文件 → 没有矩阵」**不是错误**，工具返回 `ok=True` + `available=False` +
一条 warning：不说出来的话，调用方会把「没对照过」当成「全满足」。

`law.cite` 的卡点原本是「`check()` 内部自己 `law_index.load_all()`」。做法是把
「取哪三条」抽成 `law_cite.load_articles()`，`check(text, min_conf, *, articles=...)`
改成接受注入的条文字典。顺带修掉一个真实缺陷：**条文缺失时 `check()` 会 `TypeError`**
（`art19['law_short']` 打在 `None` 上）——现在缺哪条就整段跳过，并把「该项未执行」
写进 `skipped`，再由工具提成 warning。

`docx.format` 与 `text.lint` 原先被划进「批次 2（有 IO）」，实际做法与前几个一致：
`docx.format` 可以**直接给解析产物 `info`**（纯函数式），也可以只给 `path` 让注入的
`extract` 去解析；`text.lint` 把 `run_lint` 整个注入——工具不关心它跑的是 ruff、
eslint 还是别的，只把 `ok / issues / skipped / failed` 四种状态如实带出来。
所以两者都迁进了批次 1，批次 1 到此收口。

`rules.classify` 与 `integrity.hash` 的权限是 **READ**——它们只查表/只算哈希，不读材料，
不该要求 `ANALYZE`。权限收紧比放宽难，先按最小权限给。

> 实测补充：当前 `rules/` 里的 33 条规则**没有一条**自带 `pattern`（全部走 `method`
> + 内置执行器）。所以 `rules.pattern` 在真实规则集上会明确返回「没有带 pattern 的规则」，
> 而不是假装扫过一遍——这条由 `checks/test_builtin_tools.py::test_real_ruleset_is_handled_honestly`
> 钉住；将来规则集加了声明式规则，这条用例会变成正向断言。

**批次 1 之后还剩什么**：`tools/` 里其余模块（检索、OCR、事实库、学习闭环、评测运维）
按下面两节处理；以及「重复实现清理」（盘点 §5 的重名函数），那需要逐个人工确认，
不是机械替换。

### 批次 2：有 IO / 依赖外部，后迁（接口先定，实现后换）

| 模块 | 工具名 | 注意 | 状态 |
| --- | --- | --- | --- |
| `retrieve.py` / `law_index.py` | `knowledge.retrieve` | 索引加载要可注入（测试不读真实库） | ✅ **已迁（2026-10-31）**：`search_articles` / `grep_facts` / `cite` 全部注入，`scope` 取 laws / facts / all。关键口径：知识库是**系统自己的资产**，结果 `untrusted=False`（与待审文档区分开）；没命中给 warning（「没命中 ≠ 没有相关规定」） |
| `docx_extract.py` / `ingest.py` / `ocr.py` | `document.extract` | 解析器可换；失败要给明确错误 | ✅ **已迁（2026-10-31）**：`ingest_file` 注入，覆盖 txt / pdf / docx / 图片 / 压缩包；抽出的是**待审内容** → `untrusted=True`（与 `knowledge.retrieve` 正好相反）；解析失败与 `meta.warnings` 都如实带出 |
| `llm_client.py` / `llm_review.py` | `model.complete` | 属于基础设施端口，**不是**工具；工具只能通过 ModelClient 调用 | ✅ **已建端口（2026-10-31）**：`ModelClient` + `ChatClientAdapter`，见 ARCHITECTURE §9 |
| `research.py` | `research.web` | 网络：白名单 + 超时 + 来源记录 | 未迁：联网核验是**对外副作用**，要先有白名单与来源落库 |

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

