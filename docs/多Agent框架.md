# 多 Agent 框架：把「一套判定标准」变成可插拔的包

> 最初设定是「在各种项目上都用 Agent」——安全方面有安全审 Agent，文档方面有文档审 Agent。
> 但实现到 v0.4.9 时，整个项目其实只有一个 Agent：维度、规则来源、检查器、任务书
> 全部写死在 `review.py` 里。这份文档记录把它拆开的过程与结果。

---

## 1. 问题：改写硬编码

重构前的判定标准分散在四个地方，且都是常量：

| 写在哪儿 | 内容 | 后果 |
|---|---|---|
| `review.py:DIM_FOR_TYPE` | 类型 → 维度 | 加一种对象类型要改代码 |
| `run_one` 里 5 行 `s = fact_review(...)` | 维度执行顺序 | 加一个维度要插入一行代码 |
| `rules_store.load_all_rules()` | 规则只从 `rules/` 读 | 安全规则没地方放，只能混进文档规则库 |
| `config/charter.json` | 全局一份任务书 | 安全审和文档审共用「后果等级=medium」这种显然错误的默认值 |

于是「加一个安全审 Agent」的真实成本不是写规则，而是**改主流程并 Regression 已有行为**。
这正是这次要拆的耦合。

---

## 2. 抽象：Agent = 一套判定标准

```
Agent
├── rules          规则来源（可指向全局 rules/ 或自己的 rules/）
├── dimensions     维度清单（id / 中文名 / 配色 / 处理器），数组顺序 = 执行顺序
├── dims_by_type   对象类型 → 本次生效的维度
├── default_dims   类型未声明时的兜底（允许为空 = 一个都不跑）
├── checks         维度审之后额外执行的检查器
├── charter        任务书默认值（config/charter.json 非空字段优先）
└── boundaries     本 Agent 不覆盖什么（直接进报告 limitations）
```

主流程退化成「按 manifest 调度」，不再认识具体维度：

```
S1 解析 → 选 Agent（显式 > 路由 > 默认）→ S2 按 dims_by_type 取维度与规则
        → S3 for dim in 维度: 调 handler；再 for check in checks
        → S4 证据锚定 → S5 交叉复核 → S6 裁定
```

处理器只有两种来源，写法统一为 `(session, text, rules, ctx)`：

- `builtin:*` —— 复用已有实现（fact / safety / style / compliance / static_scan / arith …）
- `pack:*` —— 指向 `agents/<id>/checks.py` 里的 `CHECKERS` 字典

> 统一签名是关键：如果每个处理器签名不同（`static_scan` 要 `target_path`、
> `compliance_review` 要 `target_type`），就没法用数据来调度，只能继续写 if。

---

## 3. 目录结构与 manifest

```
agents/
├── doc-review/
│   └── manifest.json           # 默认 Agent，行为与重构前逐字段一致
└── security-audit/
    ├── manifest.json
    ├── rules/security.yaml     # 14 条 SEC-* 规则（含 pattern/require/guard）
    └── checks.py               # 凭据 / 通用正则引擎 / 依赖 三个检查器
tools/agent_spec.py             # 加载、校验、路由、规则合并
```

manifest 关键字段（完整表见 `tools/agent_spec.py` 顶部文档）：

```json
{
  "id": "security-audit",
  "auto": false,
  "match": {"ext": [".py", ".js"]},
  "rules": ["rules/safety.yaml", "agents/security-audit/rules"],
  "dimensions": [{"id": "injection", "label": "注入", "color": "#dc2626",
                  "handler": "pack:injection"}],
  "dims_by_type": {"code": ["secret", "injection", "config", "dependency", "safety"]},
  "default_dims": ["secret", "injection", "config", "dependency", "safety"],
  "checks": [],
  "charter": {"consequence": "high", "strictness": "strict"},
  "boundaries": ["只做静态写法匹配，不做数据流跟踪", "..."]
}
```

### 三个刻意的设计决定

**① `auto: false` —— 换一套判定标准必须人工选。**
安全审会把一份普通代码判成 fail。如果让它参与自动路由，用户点一次「运行审查」
就可能被换掉整套标准却不知道。所以只有 `doc-review` 是 `auto`，其余靠 `--agent`。

**② `default_dims` 允许为空数组。**
「没声明」如果退化成「全部维度都跑」，路由写错就完全看不到；退化成「空」，
S2 日志会显示「无」，覆盖率与 limitations 立刻显形。宁可吵闹，不要沉默。

**③ manifest 不合法就启动即报错。**
未声明的维度、写错的 handler（`bulitin:safety`）、不在白名单的 severity，
一律抛 `AgentError` 并指出文件与原因。**静默跳过是最坏结果**——
报告看起来正常，实际少跑了一整类检查。

---

## 4. 安全审 Agent：判定条件放在 YAML

`agents/security-audit/rules/security.yaml` 的规则除 `desc/severity` 外，可带：

| 字段 | 作用 |
|---|---|
| `pattern` | 必中正则（字符串或数组，任一命中） |
| `require` | 同一行必须同时命中（如「`execute(` 且 有 SQL 关键字」） |
| `guard` | 命中即放弃（防御性语境、示例） |
| `unless` | 命中即放弃（替代的安全写法，如 `safe_load`） |
| `confidence` | 该规则的结构可信度，模型无权改 |

`checks.py` 只做通用匹配与去噪，不含任何具体判定条件。加一条规则 = 改 YAML，
不用碰代码，也就不会出现「三份报告行为不同」。

### 误报控制（第一版踩到的三个坑）

实测第一版在靶场上报出 12 条，其中 2 条是误报：

1. **文档字符串被当成实现** —— `"""[说明] shell=True ..."""` 整行报了 blocker。
   修法：`_string_lines()` 维护三引号状态，注释行与 docstring 行一律跳过。
   凭据检查是例外：注释里出现真凭据也是泄露，照样报。
2. **参数化查询被当成拼接** —— `execute("... %s", (v,))` 命中了 `%s`。
   修法：要求格式化算符**紧跟在字符串结束引号之后**（`% (var)` / `.format(` / `f"`），
   而不是「SQL 里出现了占位符」。
3. **依赖规则在业务代码上开火** —— `>=` 在业务逻辑里到处都是。
   修法：依赖规则只在 `requirements*.txt / pyproject.toml / package.json` 等清单上生效，
   非清单文件直接记 `skipped`。

### 实测（`targets/security_demo/`）

| 样本 | 结果 |
|---|---|
| `vuln_app.py`（植入 10 处） | **10/10 命中**，verdict = fail（4 blocker / 4 major / …） |
| `safe_app.py`（全安全写法） | **0 条**（参数化查询、`shell=False`、`safe_load`、环境变量凭据） |
| `requirements.txt` | 3 条：1 条 git+ 直连（major）、2 条未锁版本（minor） |

同一份 `vuln_app.py` 用 `doc-review` 审：0 条 `SEC-*`、verdict 不同——
**同一文件、两套标准**，这正是多 Agent 的意义。

---

## 5. 哈希链：判定标准也要可追溯

报告新增：

```json
"agent": {
  "id": "security-audit", "version": "1.0.0",
  "selected_by": "人工指定 Agent：安全审查 Agent（v1.0.0）",
  "dimensions": [{"id": "injection", "label": "注入", "color": "#dc2626"}],
  "checked_dimensions": ["secret", "injection", "config", "dependency", "safety"],
  "checks": [], "rule_sources": ["..."], "boundaries": ["..."]
},
"integrity": {
  "agent_dir_hash": "...", "agent_version": "1.0.0",
  "knowledge_version": "1.0.1"
}
```

- `active_rules` 已包含 Agent 自带规则 → `ruleset_hash` 覆盖规则本身；
- `agent_dir_hash` 覆盖 manifest + 该 Agent 的规则与检查器（只看本 Agent 目录，
  别的 Agent 改了不会让这份报告变可疑）；
- 校验时同样区分两件事：**版本变了 → stale（黄）**，**版本没变但 hash 变了 → 篡改（红）**。

### 顺带修掉一个我自己造成的坑

上一轮为了给「豆包大模型」加口径排除，直接改了 `knowledge/facts.json`，
结果 `knowledge_hash` 与 87 份历史报告全部失配，**整条历史全红**。
规则集有 `rules_version` 挡着，知识库却什么都没有——同一个问题漏了一半。

现在补了 `config/knowledge_version.txt`（放在 `knowledge/` 之外，否则它自己会改 hash），
校验逻辑与规则集一致：版本变 → stale，版本没变才判篡改。
87 份报告从 FAIL 回到 STALE，链完整性 `ok / chain_ok`。

---

## 6. 怎么新增一个 Agent（不需要改引擎）

1. 建目录 `agents/<id>/`，写 `manifest.json`（`dimensions` 至少一个，`dims_by_type` 必填）；
2. 规则放 `agents/<id>/rules/*.yaml`，在 `rules` 字段里声明来源；
3. 需要自定义检查器就写 `checks.py`，导出 `CHECKERS = {"名字": fn}`，
   签名 `(session, text, rules, ctx)`，manifest 里用 `pack:名字` 引用；
4. `python review.py --list-agents` 确认加载成功（manifest 不合法会在这里报出来）；
5. 拿一份**故意有错**的样本和一份**全对**的样本各跑一次：前者要全中，后者要 0 条。
   只验前者等于没验——误报才是让人放弃清单的原因。

约定：新增或修改判定标准后**必须升 manifest 的 `version`**，否则历史报告会被判成篡改
（与 `rules_version` 同一套机制）。

---

## 7. 现有两个 Agent 对照

| | doc-review | security-audit |
|---|---|---|
| 判什么 | 文字是否准确、合规、得体 | 写法是否可被利用 |
| 维度 | 事实/安全/风格/合规/逻辑 | 凭据/注入/配置/依赖/内容红线 |
| 规则 | 24 条（全局 `rules/*.yaml`） | 18 条（`rules/safety.yaml` + SEC-*） |
| 检查器 | static_scan / arith / comp004 去重 | 凭据 / 通用正则 / 依赖 |
| 任务书 | 通用（后果 medium） | 安全（后果 high、严格度 strict） |
| 自动路由 | 是（默认） | 否，必须显式 `--agent` |
| 典型结论 | 「1080 应为 1050」 | 「SQL 用 f-string 拼接，建议参数化」 |

---

## 8. 这套抽象的边界（如实）

| 没做 | 为什么 / 影响 |
|---|---|
| **Agent 之间不能组合** | 现在一次审查只用一个 Agent。要「同时按文档标准和代码标准审」需要多次运行、人工合并 |
| **不并行** | 维度仍然顺序执行（原本就是）。真并行要处理日志/进度写入竞争 |
| **不支持 Agent 升级迁移** | 改了 manifest 只能升 `version`，旧报告变 stale，不能自动重算 |
| **安全审不做数据流分析** | 只看写法，不看「外部输入是否真的流到了这里」。别名赋值、跨文件调用链都识别不了 |
| **无 CVE 比对** | 只判依赖是否锁定版本与来源，不判依赖本身有没有已知漏洞 |
| **`config` 维度靠类型兜底** | `.yaml/.ini` 会被判成 `llm_response` 走 `default_dims`（全维度），而不是专门的配置维度 |

一句话：**这是一层调度抽象，不是多智能体协作。** 它解决的是「加一个场景要改主流程」，
不是「多个 Agent 互相辩论」——后者（交叉复核）目前只在维度之间做，不在 Agent 之间做。
