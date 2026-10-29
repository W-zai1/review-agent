# 审查 Agent 项目（Demo v0.2.7）

> 来源定义：`review-agent-framework.md`（已随仓库归档，审查 Agent 框架定义 v0.2，Demo 版）
> 当前实现：v0.2.7 —— 按诊断报告修复 F1~F5 与 O1/O2/O5：事实维度补齐、规则目录哈希、服务请求防护、合同否定语境、交叉复核用例、ID 锁、日志即时落盘、回归测试。
> 定位：个人日常 / Demo。只读、独立裁判、可举证、可迭代。**跑通一次完整审查闭环**即可。
>
> 改动流水见 **`CHANGELOG.md`**（逐条登记增删改：文件 / 改了什么 / 为什么 / 怎么验的）。
> 本 README 只讲「为什么这样设计」，不复述流水。
> **这条约定现在有守门**：`python tools/changelog_guard.py` 会在漏登记时返回 1，
> 已挂进 `.githooks/pre-commit`（一次配置 `git config core.hooksPath .githooks`，之后自动生效）。

一句话：**「我不替你写，我只替你查；我给出每一条异议的出处和规则依据。」**

---

## 1. 它做了什么

对 `targets/` 里的待审对象（LLM 回答 / 提示词 / 合同 / 代码）跑六步闭环 S1–S6：

1. **S1 接收解析**：识别对象类型（llm_response / contract / code），加载对应规则集
2. **S2 规则匹配**：按类型裁剪生效规则（事实 / 安全 / 合规 / 风格 四维度）
3. **S3 分维审查**：四个子审查器顺序跑，各出 issue 列表
4. **S4 证据锚定**：每条 issue 绑定 `quote + rule_id + evidence + tool + confidence`
5. **S5 交叉复核**：四维互查冲突 → `disputes`，无冲突则记决策
6. **S6 裁定汇总**：`pass / warn / fail`，产出 `outputs/<id>.json` + `.md`

其中 S3 的规则审**始终执行**；配置好大模型后额外跑分维语义审（每维一次调用），调用失败自动降级为纯规则审。

核心约束（来自框架五原则）：独立裁判、规则显示化（`rule_id`）、最小权限只读（只写 `outputs/` `logs/`）、可追溯举证、多视角对抗。

---

## 2. 目录结构

```
review-agent/
├── README.md
├── CHANGELOG.md            # 改动流水：每次增删改逐条登记（想查"谁动了哪一处"看这里）
├── .githooks/pre-commit    # 提交前调用 changelog_guard，漏登记就拦（core.hooksPath 指向它）
├── review-agent-framework.md  # 框架定义原文（v0.2，来源文档）
├── review.py               # 主流程 S1~S6
├── config/
│   ├── llm.yaml            # 大模型端点/模型/超时/重试（密钥走环境变量）
│   ├── scenarios.yaml      # 审查情境（哪些规则参与，按用途选）
│   ├── format_specs.yaml   # 格式规格（Word 版式的**数值**：字体/字号/行距…）
│   ├── llm.local.json      # 前端保存的覆盖配置（运行时生成，含 Key，勿提交）
│   └── rules.state.json    # 规则启停状态（运行时生成，勿提交）
├── rules/                  # 规则库（YAML，16 条）
│   ├── fact.yaml           #   FACT-001~004
│   ├── safety.yaml         #   SAFE-001~004
│   ├── compliance.yaml     #   COMP-001~004
│   ├── format.yaml         #   FORMAT-001~006（只声明查哪些维度，数值在 config/format_specs.yaml）
│   └── style.yaml          #   STYLE-001~004（004 = 硬编码密钥 + lint）
├── knowledge/              # 事实库 / 政策基线
│   ├── facts.json          #   机器可读事实断言（事实审比对用）
│   ├── kb_doubao.md        #   豆包事实文档
│   └── kb_policy.md        #   合同/披露合规基线
├── targets/                # 待审对象（只读）
│   ├── answer_001.txt      #   含事实硬伤的样例
│   ├── contract_001.md     #   缺争议条款的样例
│   ├── code_001.py         #   含硬编码密钥的代码样例
│   └── uploads/            #   控制台上传的文件（运行时生成，已 gitignore）
│       ├── _originals/     #     二进制原件归档（PDF/Word/图片/ZIP，不进待审列表）
│       └── _meta/          #     摄取元数据（报告里标注来源用，不进待审列表）
├── tools/                  # 检查脚本（均可独立 CLI 运行）
│   ├── regex_check.py      #   数字/日期/敏感词/风格抽取 + 密钥定位
│   ├── retrieve.py         #   知识库 grep + 加载 facts.json
│   ├── lint.py             #   ruff/eslint 封装 + 结构化 findings
│   ├── llm_client.py       #   OpenAI 兼容客户端（urllib，零依赖）
│   ├── llm_review.py       #   分维语义审 prompt + JSON 校验
│   ├── ingest.py           #   文档摄取：PDF/Word/图片/ZIP → 可审文本（零依赖）
│   ├── ocr.py              #   多引擎 OCR 适配层（自动探测与降级）
│   ├── ocr_winrt.ps1       #   Windows 内置 OCR（WinRT，零安装；须 ASCII + BOM）
│   ├── rules_store.py      #   规则加载 + 启停状态
│   ├── fact_gap.py         #   知识库缺口清单（学「该补什么」，只写 state/）
│   ├── fact_promote.py     #   候选断言门禁/晋升/回滚（唯一写 facts.json 的入口）
│   ├── fact_import.py      #   事实断言 CSV/JSON 批量导入（走 fact_promote 治理路径）
│   ├── integrity.py        #   SHA-256 + Ledger 校验
│   └── log_summary.py      #   日志批量统计
├── web/                    # 零依赖前端控制台
│   ├── index.html
│   ├── config.js           # 前端 API Base 等运行配置
│   ├── style.css
│   └── app.js
├── outputs/                # 审查产物（可写）
│   └── R-YYYYMMDD-NNN.json / .md
├── ledger/                 # 审计链（append-only）
│   └── chain.log
└── logs/                   # 运行日志（按 review_id 分目录）
    └── R-YYYYMMDD-NNN/{task.json,decisions.jsonl,tools.jsonl,cost.json}
```

另外包含 `tests/`（unittest 回归测试，不复制到运行时产物）与 `docs/`（接口文档）：

```
tests/
├── test_review.py          # 审查引擎 golden / 规则状态 / 交叉复核 / 完整性回归
└── test_api_contract.py    # 前后端接口契约测试（起真实 server.py 逐字段断言）
state/                      # 自学习闭环的数据（刻意放在 rules/ 之外，见第 25 节）
├── annotations.jsonl       # 人工标注（误报 / 漏检）
├── candidates/             # 候选规则 + 门禁指标
├── shadow/                 # 预演结果
├── rules_log.jsonl         # 晋升 / 回滚审计（append-only）
└── rules_snapshots/        # 晋升前的规则库快照
agents/                     # 审查 Agent（判定标准以数据形式声明，见第 24 节）
├── doc-review/manifest.json        # 默认：事实/逻辑/安全/合规/风格
└── security-audit/                 # 安全审：凭据/注入/配置/依赖
    ├── manifest.json
    ├── rules/security.yaml
    └── checks.py
tools/
├── agent_spec.py           # Agent manifest 加载/校验/路由 + 规则合并
├── llm_bench.py            # 本地大模型真实测评（环境/预算探针/封装/单维/全量 五阶段）
├── arith_check.py          # 确定性算术/日历校验（加总/星期/历时/占比，不调模型）
└── eval_review.py          # 审查质量评测：recall / 标注命中率 / 跨次稳定性（隔离运行）
docs/
├── API接口对照表.md         # 前端调用点 ↔ 后端路由 ↔ 响应契约 单一事实来源
├── 多Agent框架.md           # Agent 抽象：manifest 字段、如何新增 Agent、边界
├── 自学习闭环.md             # 提案→门禁→受控晋升：威胁模型、指标口径、治理
└── 本地模型实测报告.md      # deepseek-r1:8b 端到端实测数据与复现步骤
```

---

## 3. 运行方式

```bash
# 依赖（解释器不再写死：优先 REVIEW_AGENT_PY，其次项目 .venv，最后当前解释器）
python -m venv .venv && .venv/Scripts/python -m pip install pyyaml   # Windows
# python -m venv .venv && .venv/bin/python -m pip install pyyaml      # macOS / Linux

# 可选：显式指定解释器（供 server.py 调 review.py 用）
set REVIEW_AGENT_PY=D:\Python312\python.exe

# 可选：接入大模型（OpenAI 兼容；不配则自动纯规则审）
# 方式 A：环境变量
set REVIEW_AGENT_LLM_API_KEY=sk-xxxx
set REVIEW_AGENT_LLM_BASE_URL=https://api.deepseek.com/v1
set REVIEW_AGENT_LLM_MODEL=deepseek-chat
python tools/llm_client.py --check
# 方式 B：启动控制台后点右上角「LLM 未配置」直接填（见第 12 节）

# 列出可审对象
python review.py --list

# 列出审查 Agent（每个 Agent = 一套判定标准）
python review.py --list-agents

# 自学习闭环：标注 → 挖掘 → 门禁 → 预演 → 晋升（见第 25 节）
python tools/annotate.py --kind false_negative --by wang --target targets/x.txt \
    --dimension logic --quote "同比下降 -3.2%"
python tools/rule_miner.py mine && python tools/rule_miner.py gate AUTO-LOGIC-XXXX
python tools/rule_promote.py shadow AUTO-LOGIC-XXXX
python tools/rule_promote.py promote AUTO-LOGIC-XXXX --by wang

# 审一份（auto：有凭据就调 LLM，否则纯规则）
python review.py targets/answer_001.txt
python review.py targets/contract_001.md
python review.py targets/code_001.py
python review.py targets/answer_001.txt --no-llm   # 强制纯规则审
python review.py targets/answer_001.txt --llm      # 强制调用（无凭据会失败并降级）
python review.py targets/code_001.py --agent security-audit --no-llm   # 换一套标准：安全审

# 完整性校验（ledger 链 + 每份报告自洽）
python tools/integrity.py verify
python tools/integrity.py verify R-20261022-001

# 审查质量评测：recall / 标注命中率 / 跨次稳定性（隔离运行，不污染历史报告）
python tools/eval_review.py --runs 3

# 单独试跑确定性算术校验（不调模型，零 token）
python tools/arith_check.py targets/adversarial_meeting_001.txt

# 日志统计（成功率 / 耗时 / token）
python tools/log_summary.py

# 回归测试（stdlib unittest，无需 pytest）
python -m unittest discover -s tests -v

# 仅跑前后端接口契约测试（锁定 web/app.js ↔ server.py，约 50s）
python -m unittest tests.test_api_contract -v

# 批量导入事实断言（CSV / JSON）
python tools/fact_import.py --csv my_facts.csv
```

### 3.1 整改单与库外事实核验

审查结果不再停在「可疑」。每条 issue 都带一个 `fix` 对象：

- `where`：改哪一句 / 缺的条款插在哪；
- `from` / `to`：原文与可直接替换的改写文本（法条上限、事实数值、用词、隐私脱敏、
  缺失条款模板）；
- `consequence`：不改会怎样（具体到钱/权/时间，不写「有风险」）。

能确定性改的给文本；改不了的标 `action=manual`，不假装有方案。
报告 Markdown 顶部有「整改单」表，Web 报告页同样展示；JSON 里 `action_plan` + 每条 `issue.fix`。

```bash
# 对事实库外的「实体 + 指标 + 数值」主张做检索核验（默认关闭）
python review.py targets/selftest/st21_fact_xiaomi_research.txt --no-llm --research

# 只查本地来源库，不联网：把 config/research.yaml 的 providers 设为 [local]
python review.py targets/xxx.txt --no-llm --research

# 往来源库加一页（下次同一实体的主张先查本地）
python tools/research.py --add-source https://example.com/report.html --title "来源标题"

# 单条主张调试 / 查看已学结论
python tools/research.py "小米 2024 年全球智能手机出货量为 5 亿台" --json
python tools/research.py --learned
```

核验顺序：**人工来源库 `knowledge/sources/` → 已学结论 `state/learned_facts.jsonl`
→ 检索缓存 → 公开网页（360/搜狗，失败降级）**。只有本地能判或两个独立来源一致时
才自动改写；单一来源只提示「检索到不同数值，先确认口径」。未找到不生成 issue，
只进报告「事实核验」区并写明检索过什么——「没查到」不等于「虚假」。

### 3.2 3 分钟演示（无真实文档）

`demo/` 下有两份**合成样例**和一份演示脚本：劳动合同（试用期/工资/竞业/缺条款）
与事实报告（小米出货量/渗透率/豆包 MAU）。见 [`demo/README.md`](demo/README.md)。

```bash
py -3 tools/research.py --clear-cache
py -3 review.py demo/contract_labor.txt --no-llm --no-research
py -3 review.py demo/claim_report.txt --no-llm --research
```

---

## 4. 内置样例结果

| 待审对象 | 类型 | 裁定 | 主要异议 |
|---|---|---|---|
| `answer_001.txt` | llm_response | **fail** | FACT-001 `blocker`：豆包 MAU「10 亿」与公开口径「约 1 亿」差一个数量级；COMP-004 `minor`（conf 0.6，进 to_confirm）：含数字无来源 |
| `contract_001.md` | contract | **warn** | COMP-003 `major`：缺失争议解决条款 |
| `code_001.py` | code | **warn** | STYLE-004 `major`：`API_KEY = "sk-demo-…"` 硬编码密钥（conf 0.95）；未装 ruff 时 lint 自动跳过 |

`verdict` 规则：存在 blocker → `fail`；仅 major → `warn`；否则 `pass`。`key_facts` 不再写死，只收「本次对象确实命中事实库 / 合同要件检查」的已核验事实；代码对象没有事实断言时为空数组。

> 上表是**纯规则基线**。启用大模型后，语义审可能补充新的 issue（同样要过 `rule_id` 白名单与 `quote` 逐字校验），条数与裁定可能变化。

---

## 5. 完整性 / 防篡改（Demo 边界）

- `integrity` 块含 `target_hash / ruleset_hash / knowledge_hash / report_hash / logs_hash / prev_report_hash` 六类哈希。
- `ledger/chain.log` 每行追加一条，任何一行被改，下一行 `prev_hash` 对不上 → 一眼发现篡改。
- `report_hash` 计算前必须把嵌套在 `integrity` 内的 `report_hash` 置空（自指问题，已处理）。
- **边界**：SHA-256 只防无意改动和事后抵赖；防故意篡改需数字签名，属 v1.0 之后。

---

## 6. 设计要点与框架对应

- **规则落库**：所有规则在 `rules/*.yaml`，禁止散落 prompt（对应第 5、16 节坑 1）。
- **quote 逐字**：issue 的 `quote` 用 `sentence_at()` 逐字摘录，禁止改写（坑 2）。
- **confidence 不写满**：保留 0.6–0.9 分布，`< 0.7` 自动进 `to_confirm`（坑 3）。
- **只读不写回**：全程不修改 `targets/`，只出报告与建议（坑 4）。
- **日志进哈希链**：`logs_hash` 挂进 `integrity`，防止 Agent 事后改运行记录（坑 9）。
- **静态扫描真接入**：`S3` 对代码对象调 `tools/lint.py`，把 ruff/eslint 告警与硬编码密钥转成带**行号 + 逐字 quote** 的 STYLE-004 issue；工具未安装时在 `tools.jsonl` 记 `skipped`，不伪造结论。
- **交叉复核不是占位**：`fact×safety`、`compliance×style` 两对视角按 quote 去标点后的重合度（包含关系或二元组 Jaccard ≥ 0.6）判定「同一片段」；至少一方为 blocker/major 时进 `disputes`，双方 confidence 各降 0.05 并记决策日志。
- **key_facts 动态生成**：只写本次对象真正命中事实库断言 / 合同要件的那几条，不再把豆包结论套到所有对象上。
- **解释器可移植**：`server.py` 与 `start-console.bat` 按 `REVIEW_AGENT_PY → .venv → PATH` 顺序查找 Python，换机器不用改绝对路径。
- **模型输出不可信**：LLM 只能从本次 `active_rules` 里选 `rule_id`，`quote` 必须逐字命中原文，`severity` 由规则库决定；解析失败/超时/未配置一律降级，不伪造结论。
- **token 进成本日志**：`cost.json` 记录 `llm_tokens` / `llm_calls` / `llm_failures` / `llm_model`，按维度统计留在报告的 `llm.dimensions`。

---

## 7. 后续扩展（MVP → v1.0）

- v0.3：真·多 Agent 并行调度、Web 表单上传
- v0.4：图片 OCR 审查、PDF 合同审查
- v1.0：规则库版本管理、历史审查对比、一键导出整改单、数字签名存证

---

## 8. Web 控制台（四页）

为审查闭环配了一个**零依赖**的本地可视化控制台，按理想稿实现四个页面：**审查台 / 规则库 / 审计日志 / 校验 Ledger**。

```
review-agent/
├── server.py          # 后端：标准库 http.server，包装 review.py + 静态资源 + API
└── web/               # 前端：原生 HTML/CSS/JS
    ├── index.html
    ├── config.js      # 前端 API Base 配置
    ├── style.css
    └── app.js
```

**启动：**
```bash
# 方式一：命令行（可指定端口，缺省 8000）
python server.py --port 8000

# 方式二：双击 start-console.bat（纯 ASCII + CRLF，避免 cmd 代码页解析错位）
start-console.bat 8000              # 启动并打开浏览器
start-console.bat 8000 --no-browser # 只启动，不开浏览器
start-console.bat --check           # 只检查 Python / pyyaml / LLM 配置

# 浏览器打开
http://127.0.0.1:8000
```

> bat 的启动过程写在 `logs/console-boot.log`；端口被其他程序占用时会提示并在窗口里 `pause`，不会静默失败。
> 控制台端口与 DSH Web GUI（随机端口）无关，避免混淆。

**提供的 API（server.py 内部）：**
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/targets` | 列出 `targets/` 与 `targets/uploads/` 待审文件 |
| GET | `/api/reports` | 历史报告列表（含 verdict / stats / 目标文件名） |
| GET | `/api/report?id=R-xxx` | 读取单份报告 JSON |
| GET | `/api/integrity` | 校验 ledger 链 + 报告自洽，返回逐节点 hash 与状态 |
| GET | `/api/rules` | 规则库（含维度、严重度、启用状态） |
| GET | `/api/logs?id=R-xxx` | 读取单次审查的 task / decisions / tools / cost 四类日志 |
| GET | `/api/review/status?job=x` | 查询异步审查任务的 S1~S6 进度与报告 |
| GET | `/api/llm/status` | LLM 配置状态（key 只返回 masked 形式） |
| GET | `/api/llm/providers` | 常见服务商 / 本地模型预设 |
| POST | `/api/upload?name=x.txt` | 上传待审文件到 `targets/uploads/`（文本 ≤ 2 MB；PDF/Word/图片/ZIP ≤ 20 MB） |
| POST | `/api/purpose/upload?name=x` | 上传「审查目的」说明文件（PDF/Word/图片/ZIP/文本），摄取成文本暂存 `state/purpose_uploads/`，每份截取前 2000 字 |
| GET | `/api/purpose/files` | 列出暂存的目的文件（不含原文，只给预览） |
| DELETE | `/api/purpose/files?id=x` | 删除一个暂存的目的文件（含 `_originals/` 归档原件） |
| POST | `/api/review/start` | 启动异步审查（`{file, llm?, purpose?, purpose_files?}`），返回 job_id |
| POST | `/api/rules/toggle` | 启用/停用规则（`{rule_id, enabled}`） |
| POST | `/api/llm/config` | 保存前端配置到 `config/llm.local.json` |
| POST | `/api/llm/test` | 用草稿配置试连一次，不落盘 |
| POST | `/api/llm/reset` | 清除本地覆盖，回到 YAML / 环境变量 |
| POST | `/api/review {file}` | 同步运行 `review.py` 子进程，返回最新报告（兼容旧调用） |
| POST | `/api/reports/favorite` | 收藏 / 取消收藏一份历史报告（`{id, favorite:true\|false}` → `config/favorites.json`） |
| DELETE | `/api/report?id=R-xxx` | 删除历史报告（须 body 传 `confirm=R-xxx` 二次确认；文件移入 `outputs/.trash/`） |

> 接口契约（前端调用点 ↔ 后端路由 ↔ 字段）以 [`docs/API接口对照表.md`](docs/API接口对照表.md) 为单一事实来源；
> `tests/test_api_contract.py` 起真实 server.py 逐字段断言，前端调的每个接口都在后端有对应代码。

**四个页面：**

- **审查台**：拖拽/点击上传、已有对象下拉、文件 chip；运行审查后按 S1~S6 实时推进；失败/警告/通过横幅 + 逐条 issue（原文、规则、证据、置信度、建议）；右侧历史报告与完整性摘要。
- **规则库**：16 条规则按维度筛选，开关即时启停；状态存 `config/rules.state.json`，下一次审查只加载启用规则。
- **审计日志**：按报告切换，四个子页签展示任务步骤、决策、工具调用、成本汇总。
- **校验 Ledger**：链节点可视化，逐节点显示 report_hash 短值；断链或报告被改时节点/箭头标红并给出断点。
- 右上角 LLM 胶囊 + 未配置横幅：打开配置弹窗（保存 / 测试连接 / 恢复默认 / 清除 Key）。

**说明：** 控制台复用现有 `review.py` 与 `integrity.py`，本身不新增审查逻辑，只做编排与可视化；审查产物仍落到 `outputs/` `logs/` `ledger/`。

---

## 9. v0.2.1 修复记录

| # | v0.2 的问题 | v0.2.1 的修复 | 涉及文件 |
|---|---|---|---|
| 1 | 框架源文档缺失，README 引用不到 | 归档 `review-agent-framework.md` 到仓库根目录 | `review-agent-framework.md` |
| 2 | 解释器路径写死为某台机器的 `.workbuddy` 路径，换机即挂 | `server.py` 用 `resolve_python()`；bat 按 `REVIEW_AGENT_PY → .venv → PATH` 查找，缺 pyyaml 自动建 venv | `server.py`、`start-console.bat` |
| 3 | `lint.py` 从未被调用，STYLE-004 只声明不执行；`static_scan` 恒为 skipped | `lint.py` 输出结构化 findings；S3 对代码对象真跑 ruff/eslint，任意文本都扫硬编码密钥，生成行列级 issue | `review.py`、`tools/lint.py`、`tools/regex_check.py` |
| 4 | `cross_review()` 只比较完全相同 quote，基本不会触发争议 | 按去标点重合度判定同一片段，`fact×safety`、`compliance×style` 冲突进 `disputes`，并下调双方 confidence | `review.py` |
| 5 | `key_facts` 写死豆包结论，合同/代码对象也被套用 | 改为从事实库命中与合同要件检查动态汇总 | `review.py` |
| 6 | `task.json` 写完后才记 S6 步骤，日志缺最后一步 | S6 步骤在写日志前落盘 | `review.py` |
| 7 | 三份样例只覆盖文本/合同，代码审查无法验证 | 新增 `targets/code_001.py`，README 样例表补一行 | `targets/code_001.py`、`README.md` |
| 8 | STYLE-001~003 与合同缺失条款的 `quote` 是改写文本（如「含 emoji 字符」「合同正文」），违反逐字要求 | STYLE-001~003 改为 `sentence_at()` 逐字摘句；缺失要件以文档标题为锚点，`location` 标注「缺失要件，无对应原文」 | `review.py` |

---

## 10. 大模型 API 接入（v0.2.2）

### 10.1 支持范围

任意 **OpenAI 兼容**的 `POST {base_url}/chat/completions` 端点：

| 服务 | base_url 示例 | model 示例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| Ollama（本地） | `http://127.0.0.1:11434/v1` | `deepseek-r1:8b`（思考型，需 max_tokens ≥ 4096）|
| 任意网关 | `https://your-gateway/v1` | 按网关文档 |

不引入 `openai` SDK，客户端只用标准库 `urllib`，保持项目零依赖。

### 10.2 配置方式

编辑 [`config/llm.yaml`](config/llm.yaml)，或用环境变量覆盖。优先级见 [§12.2](#122-配置优先级)：**前端保存 > 环境变量 > YAML > 默认值**。

| 环境变量 | 作用 | 默认 |
|---|---|---|
| `REVIEW_AGENT_LLM_API_KEY` | API Key（推荐只放环境变量） | 空 |
| `REVIEW_AGENT_LLM_BASE_URL` | 端点根地址 | `https://api.deepseek.com/v1` |
| `REVIEW_AGENT_LLM_MODEL` | 模型名 | `deepseek-chat` |
| `REVIEW_AGENT_LLM_ENABLED` | `1/0` 强制开关 | `auto` |
| `REVIEW_AGENT_LLM_TIMEOUT` | 单次超时（秒） | `40` |
| `REVIEW_AGENT_LLM_MAX_RETRIES` | 重试次数 | `2` |
| `REVIEW_AGENT_LLM_MAX_TOKENS` | 返回 token 上限 | `1200`（**思考型模型建议 ≥ `4096`**，见第 16 节）|
| `REVIEW_AGENT_LLM_MAX_INPUT_CHARS` | 原文截断长度 | `12000` |
| `REVIEW_AGENT_LLM_MAX_ISSUES` | 每维最多采纳条数 | `5` |

Key 解析顺序：`REVIEW_AGENT_LLM_API_KEY` > `config/llm.yaml` 的 `api_key` > 主机名匹配时的 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY`。**不会**把 OpenAI Key 发给非 OpenAI 主机。

> 双击 `start-console.bat` 时，命令行里临时 `set` 的变量不会带进去：请用系统环境变量（`setx` 后重开窗口），或把 Key 写进 `config/llm.yaml` 的 `api_key`（注意不要提交到公开仓库）。

### 10.3 审查链路

```
S3 分维审查
  ├─ fact/safety/style/compliance 规则审（确定性，始终执行）
  ├─ static_scan（代码 lint + 密钥）
  └─ semantic_review（仅当 LLM 可用）
       └─ 每个激活维度一次调用：该维规则清单 + 原文（超长截断）
            ↓
       rule_id 白名单校验 → quote 逐字命中校验 → 去重 → 生成 issue
       severity 由 rules/*.yaml 决定，模型无权改级
       tool = llm_api(<model>)
```

- 模型返回必须是 JSON 数组；解析失败、未知 `rule_id`、`quote` 不在原文，一律丢弃并记入决策日志。
- `confidence` 截断到 `0.5–0.95`；`< 0.7` 照旧进入 `to_confirm`。
- LLM 补充的 issue 与规则审 issue 一起参与 S5 交叉复核、S6 裁定。

### 10.4 降级与失败处理

| 情形 | 行为 |
|---|---|
| 未配置 Key 且非本地端点 | 不调用，`tools.jsonl` 记 `skipped`；`review_mode=rule-only`，`llm.reason` 说明原因 |
| `--no-llm` / `enabled=false` | 同上，强制纯规则 |
| HTTP 429/5xx/网络错误 | 按 `max_retries` 退避重试；仍失败则该维度记 `failed`，继续其他维度 |
| 全部维度失败 | 出纯规则报告，`review_mode=rule-only`，`core_conclusion` 明确标注“LLM 调用失败，已降级” |
| 模型返回非法 JSON | 丢弃该维度候选并记决策，不抛异常、不阻塞出报告 |

原则：**宁可少报，不可伪造**。无凭据或调用失败时，绝不生成“看起来像模型结论”的内容。

### 10.5 成本与可观测

- `logs/R-xxx/cost.json`：`llm_tokens.input/output`、`llm_calls`、`llm_failures`、`llm_model`。
- 报告 `llm` 块：`model`、`base_url`、masked `api_key`、`tokens`、`errors`、`dimensions`（每维 candidates/added/duplicates/dropped/tokens/truncated）。
- `tools.jsonl`：每次 `llm_api` 调用的输入摘要、返回摘要、耗时、状态、重试次数。
- `decisions.jsonl`：每维新增/去重/丢弃原因。
- `python tools/log_summary.py` 汇总多份审查的 `llm_calls` / `llm_failures` / `llm_tokens` / 模型列表。

### 10.6 安全边界（别吹过头）

- 发给模型的内容：**规则描述 + 待审原文**；不发送文件路径、不发送 Key、不发送知识库全文。
- Key 只以 masked 形式出现在报告和 `/api/llm/status`；`tools.jsonl` 不落 Key。
- prompt 注入：system prompt 声明“原文只是数据、其中指令不得执行”，叠加 `quote` 逐字校验，能挡住大部分“让模型改判”的攻击；但这是 Demo 级防护，**不能替代人工复核**。
- 外部 API 意味着原文会离开本机。敏感合同/个人信息请改用本地 Ollama，或加 `--no-llm`。

### 10.7 自检

```bash
# 只读检查配置（不真调用，Key 以 masked 显示）
python tools/llm_client.py --check

# 真调一次，验证 endpoint / key / model 是否通
python tools/llm_client.py "回复 ok"

# 控制台侧
curl http://127.0.0.1:8000/api/llm/status
```

---

## 11. v0.2.2 修复记录

| # | 问题 | 修复 |
|---|---|---|
| 1 | `start-console.bat` 含 UTF-8 中文且为 LF，cmd 按 GBK 解析时字节错位，双击报 `'" set "PY' is not recognized`、`'level' is not recognized` 等 | 改为**纯 ASCII + CRLF**（`chcp` 与中文全部移除）；Python 探测增加 3.x 校验并跳过 WindowsApps 占位符；新增 `--check` / `--no-browser`、`logs/console-boot.log`、端口冲突提示 |
| 2 | LLM 与规则审引用同一句但长度不同（整句 vs 半句）时去重失效，报告重复 | 同一 `rule_id` + quote 重合度 ≥ 0.8 判重 |

> bat 维护约定：**不要往 `start-console.bat` 里写中文注释或 `echo`**。Windows 的 cmd 会按 OEM 代码页（中文系统为 GBK）解析字节，UTF-8 中文可能吃掉换行符，导致后续命令整体错位。中文说明统一放 README。

---

## 12. 前端 LLM 配置面板（v0.2.3）

不用改 YAML、不用设环境变量，直接在控制台里配。

### 12.1 使用步骤

1. 启动控制台，右上角显示 **「LLM 未配置」**（灰点）。
2. 点击它，打开「大模型 API 配置」弹窗。
3. **服务商**下拉选预设（DeepSeek / OpenAI / 通义 / Kimi / 智谱 / 硅基流动 / 火山 / 千帆 / Ollama / LM Studio / vLLM / 自定义），Base URL 与常用模型会自动填好。
4. 填 `模型`（可改）、`API Key`（Key 留空表示保留已保存的；本地模型可留空）。
5. 点 **测试连接**：只发一个 `ping`，不会把待审原文发出去。
6. 点 **保存**：写入 `config/llm.local.json`，右上角变绿 **「LLM · 模型名」**。
7. 点 **恢复默认**：删除本地覆盖，回到 `config/llm.yaml` / 环境变量。

> 保存后在控制台点「运行审查」即生效，无需重启服务；`review.py` 子进程每次都会重新读取配置。

### 12.2 配置优先级

```
--no-llm（强制关闭）
  > config/llm.local.json（前端保存）
    > 环境变量
      > config/llm.yaml
        > 内置默认值
```

`--llm` 会强制启用，但仍然要求有 Key 或本地端点。

### 12.3 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/llm/status` | 返回 `available / model / base_url / api_key(masked) / api_key_set / source / local_override` |
| GET | `/api/llm/providers` | 返回服务商预设（含本地模型、常用模型、是否需 Key） |
| POST | `/api/llm/config` | body：`{base_url, model, api_key?, clear_key?, enabled?, timeout?, max_tokens?}`；只写本地覆盖，不改 YAML |
| POST | `/api/llm/test` | 同上草稿字段；`api_key` 为空时沿用已保存 Key；不落盘 |
| POST | `/api/llm/reset` | 删除 `config/llm.local.json` |

校验：Base URL 必须 `http(s)://`；`timeout` 1–300；`max_tokens` 16–8192；`model` 必填。

### 12.4 前端 API Base

如果前端不跟 `server.py` 同源部署，改 [`web/config.js`](web/config.js)：

```js
window.REVIEW_AGENT_CONFIG = {
  apiBase: "http://127.0.0.1:8000",  // 留空 = 同源
};
```

跨域时后端需要额外允许 CORS；本项目默认只监听 `127.0.0.1`，建议同源部署。

### 12.5 安全说明

- `config/llm.local.json` **明文保存 Key**（权限尽量设为 600），已加入 `.gitignore`，不要提交。
- `GET /api/llm/status` 和审查报告里的 `llm.api_key` 都只返回 masked（如 `sk-t…7890`），不回传完整 Key。
- 控制台默认只绑定 `127.0.0.1`；不要把端口直接暴露到公网。
- 生产环境请用环境变量或密钥管理服务，前端面板只适合本地 Demo。

---

## 13. 控制台上传待审文件（v0.2.4）

### 13.1 使用

1. 控制台「待审对象」旁点 **上传文件**，或把文件拖到下方虚线区域。
2. 上传成功后自动加入下拉框并选中，点「运行审查」即可。
3. 文件落到 `targets/uploads/`，原文件名保留；重名自动追加时间戳。

支持：`.txt .md .markdown .py .js .ts .jsx .tsx .json .yaml .yml .html .css .csv .log .xml`，**仅 UTF-8 文本，≤ 2 MB**。

### 13.2 接口

```http
POST /api/upload?name=answer.txt
Content-Type: application/octet-stream

<文件原始字节>
```

响应：

```json
{"ok": true, "file": {"name": "answer.txt", "rel": "targets/uploads/answer.txt", "size": 107}}
```

失败时返回 `{"ok": false, "error": "..."}`（类型不支持、非 UTF-8、空文件、超 2 MB、二进制等）。

### 13.3 安全边界

- 文件名只取 basename，剔除 `\ / : * ? " < > |` 与控制字符，防目录穿越。
- 扩展名白名单 + UTF-8 严格解码 + 拒绝 NUL 字节，避免二进制/脚本伪装。
- 上传只新增待审对象，审查过程仍然不修改它（只读原则不变）。
- 上传目录已加入 `.gitignore`，不会把用户文件提交进仓库。
- 控制台只监听 `127.0.0.1`，上传接口不对外网开放。

---

## 14. v0.2.5 四页控制台与配套后端

按 `ideal-console.html` 重建，后端新增能力：

| 能力 | 实现 |
|---|---|
| 规则启停 | `tools/rules_store.py` + `config/rules.state.json`；`review.py` 只加载启用规则，S2 的 `active_rules` 同步收缩 |
| 规则库 API | `GET /api/rules`、`POST /api/rules/toggle`，按 事实/安全/合规/风格 排序 |
| 异步审查 | `POST /api/review/start` + `GET /api/review/status?job=`；后台线程跑 `review.py`，S1~S6 进度写 `logs/.jobs/<job>.jsonl` |
| 进度事件 | `review.py --progress <file>`，`Session.step()` 每步追加一行 JSON |
| 审计日志 | `GET /api/logs?id=` 返回 `task / decisions / tools / cost`，前端四子页签渲染 |
| Ledger 可视化 | `/api/integrity` 增加 `report_hash_short`、逐节点 `ok`、`broken_at`、报告 verdict |
| 历史统计 | `/api/reports` 增加 `stats`、`target_name`、`review_mode`，右侧历史行展示 blocker/major/minor |

**注意：**

- 规则开关是**运行时状态**，不修改 `rules/*.yaml`；删掉 `config/rules.state.json` 即恢复全部启用。
- 异步任务同一时间只允许一个，避免并发写 `outputs/` 造成 review_id 冲突。
- 旧的同步 `POST /api/review` 保留，命令行/脚本不受影响。
- 控制台「运行审查」在 LLM 未配置时按设计禁用；「用示例文件试跑」会以纯规则模式跑通闭环。

---

## 15. 常见服务商预设与本地模型（v0.2.6）

### 15.1 预设列表

配置弹窗的「服务商」下拉来自 `GET /api/llm/providers`（定义在 `tools/llm_client.py` 的 `PROVIDERS`）：

| id | 服务商 | 默认 Base URL | 默认模型 | 需要 Key |
|---|---|---|---|---|
| `deepseek` | DeepSeek 官方 | `https://api.deepseek.com/v1` | `deepseek-chat` | 是 |
| `openai` | OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | 是 |
| `dashscope` | 阿里云百炼 / 通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | 是 |
| `moonshot` | Moonshot / Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` | 是 |
| `zhipu` | 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash` | 是 |
| `siliconflow` | 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` | 是 |
| `volcengine` | 火山方舟（豆包） | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-1-5-pro-32k-250115` | 是 |
| `qianfan` | 百度千帆 | `https://qianfan.baidubce.com/v2` | `ernie-4.0-8k` | 是 |
| `ollama` | Ollama（本地） | `http://127.0.0.1:11434/v1` | `deepseek-r1:8b`（思考型，需 max_tokens ≥ 4096）| 否 |
| `lmstudio` | LM Studio（本地） | `http://127.0.0.1:1234/v1` | `local-model` | 否 |
| `vllm` | vLLM（本地） | `http://127.0.0.1:8000/v1` | `Qwen/Qwen2.5-7B-Instruct` | 否 |
| `custom` | 自定义 OpenAI 兼容 | 手填 | 手填 | 可选 |

模型输入框带 `datalist`，可在预设模型之间切换，也可以直接输入任意模型名。

### 15.2 本地模型

**Ollama**

```bash
ollama serve
ollama pull qwen2.5:7b
```

控制台选「Ollama（本地）」→ 模型填**你本地实际装了的模型名**（如 `deepseek-r1:8b`，用 `ollama list` 查看）→ 测试连接 → 保存。无需 Key。

> 思考型模型（`deepseek-r1` / `qwen3` 系）会先生成思考链再输出正文，**`max_tokens` 必须 ≥ 1024**（审查场景建议 4096），否则正文恒为空。详见第 16 节。

**LM Studio**

在 LM Studio 打开 `Local Server`（OpenAI Compatible），端口默认 `1234`，选「LM Studio（本地）」即可。

**vLLM**

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct --served-model-name qwen2.5-7b --port 8000
```

控制台选「vLLM（本地）」，模型填 `qwen2.5-7b`。

**局域网模型**

`127.0.0.1 / localhost / *.local / host.docker.internal / 10.x / 172.16-31.x / 192.168.x` 都会被识别为本地端点，**允许不填 Key**。如果本地服务自己要求鉴权，照常填 Key 即可。

### 15.3 Key 切换保护

- 从 A 服务商切到 B 服务商时，旧 Key 不会被静默发给新 host：
  - 前端要求重新填写 Key；
  - 切到本地模型时自动勾选清除旧 Key；
  - 后端也做同样校验，返回「切换服务商后请重新填写 API Key」。
- 同一 host 下只改模型/超时，会保留已保存的 Key。

---

## 16. 本地大模型真实测评（v0.2.8）

### 16.1 一句话结论

本地模型（Ollama `deepseek-r1:8b`）**已被审查链路真实调用**：全量审查 `review_mode=rule+llm`、4 个维度各调用一次、零失败、LLM 实际补充了规则审没抓到的 issue。完整数据见 [`docs/本地模型实测报告.md`](docs/本地模型实测报告.md)。

### 16.2 测评流程（`tools/llm_bench.py`，五阶段）

| 阶段 | 做什么 | 回答什么问题 |
|---|---|---|
| S1 环境 | 读项目 LLM 配置 | 链路认不认这个模型？ |
| S2 预算探针 | 直连端点扫 `max_tokens` 8→2048 | 为什么"连上了却没输出"？ |
| S3 项目封装 | 走 `llm_client.LLMClient.chat()` | 项目封装能拿到正文吗？ |
| S4 单维审查 | 真实调 `llm_review.review_dimension()` | 单维度能产出合格 issue 吗？ |
| S5 全量审查 | 调 `review.run_one(use_llm=True)` | 整条链路出 `rule+llm` 报告吗？ |

```bash
python tools/llm_bench.py --model deepseek-r1:8b --target targets/answer_001.txt --max-tokens 4096 --timeout 180
python tools/llm_bench.py --skip-budget      # 快速回归
```

产物：`outputs/llm-bench-<ts>.json` + `outputs/llm-bench-<ts>.md`。

### 16.3 关键实测数据

| 结论 | 数据 |
|---|---|
| 思考链长度 | deepseek-r1:8b 约 **525 token**（`message.reasoning` 字段） |
| 正文出现的阈值 | `max_tokens` ≥ **1024**（≤256 时 `content` 恒空、`finish_reason=length`） |
| 审查可用预算 | **4096**（2000 时 JSON 被截断，产出 0 条 issue；4096 时产出 2 条） |
| 全量审查 | `R-20261023-005`，`rule+llm`，calls=4，failures=0，token in 1786 / out 3084，77.7s |
| LLM 实际增益 | fact 维新增 1 条 issue；另有 1 条与规则审重复被去重丢弃 |

### 16.4 本次修复

| # | 问题 | 修复 | 涉及文件 |
|---|---|---|---|
| 1 | `/api/llm/test` 硬编码 `max_tokens=8`、超时压到 20s，思考型模型必然"连接成功但回复空白" | 改为 `max_tokens=256`、超时上限 60s；`reply` 为空时返回 `note` 说明原因 | `server.py` |
| 2 | 审查侧 `max_tokens=2000` 对思考型模型不够，JSON 被截断 → 产出 0 条 issue | 本地配置调至 4096，并在实测报告中给出按模型类型的建议值 | `config/llm.local.json` |
| 3 | 本地模型名填错（`qwen2.5:7b` 本地未安装）导致永远走纯规则审 | 改用实际存在的 `deepseek-r1:8b` | `config/llm.local.json` |

---

## 17. 历史报告管理与收藏（v0.6.3）

### 17.1 删除历史报告（二次确认）

右栏历史列表每行悬停出现 🗑 按钮 → 弹出确认框说明影响 → 确认后删除。

**两道防线**：

| 层 | 机制 | 绕过后果 |
|---|---|---|
| UI | `#confirmModal` 弹窗 + 取消/确认按钮 | 无 |
| 服务端 | `DELETE /api/report?id=` 必须带 body `{"confirm":"<报告ID>"}` | 返回 `{ok:false, need_confirm:true}`，拒绝删除 |

**删除对审计链的影响**：不删链、不重算 hash——`ledger/chain.log` 对应行**原位打墓碑**
（加 `op="delete"`、`deleted_at`、`deleted_hash`），`seq` / `report_hash` / `prev_hash` 一律不动。
因此**链条衔接分毫不变**，`tools/integrity.py verify` 依旧通过，而"这份报告被删过"留在链上可审计
（`/api/integrity` 的 entries 中 `deleted=true`）。

删除时顺手把该报告从收藏表里摘掉——否则收藏视图里会留一个永远点不开的幽灵条目。

文件不硬删：报告 JSON/Markdown 与 `logs/<id>/` 移入 `outputs/.trash/`，误删可自行移回。

### 17.2 排序：只按时间，最新在最上面

```
GET /api/reports → 读取每份报告的 timestamp
                 → sort(key=(timestamp, review_id), reverse=True)
```

- **排序键是报告内的 `timestamp`（生成时刻），不是文件 `mtime`**。拷贝 / 解压 / 批量重写都会改
  mtime，顺序会莫名漂移；timestamp 是报告自己写死的。
- 同秒连跑两份时 timestamp 相同，用 `review_id` 兜底（编号更大的是后生成的），保证顺序稳定。
- 老报告没有 `timestamp` 字段时退回 mtime，但**先格式化成同一口径**再参与排序。

> **拖拽排序已整套移除**（v0.6.3）。两套排序必然打架："我拖出来的顺序"和"最新在最上面"
> 不可能同时成立；用户明确只要时间序，于是把 `draggable` / `⋮⋮` 手柄 / `moveReportInList` /
> `persistReportOrder` / `POST /api/reports/order` 从前后端一起删掉。
> `config/report_order.json` 不再被读取（文件留着不删，里面只是旧快照）。
> 测试用"禁止残留"锁死：`draggable`、`drag-handle`、`drop-before`、`/api/reports/order`
> 任何一项回到代码里都会失败——**半删的拖拽最糟**，用户会以为还能拖。
>
> 历史坑：旧实现把不在 `report_order.json` 里的新报告补到**末尾**，
> 于是每份新报告都被压到 140 条列表最底部，右栏只露顶部几条——表现就是"历史报告不更新"
> （实测 `R-20261026-007/010/011` 排在 137 位之后）。

### 17.3 收藏（⭐）

每行右侧是星标按钮：`☆` 未收藏 / `★` 已收藏（金色，**常显不靠 hover**）。
点星标只改收藏状态，**不会顺带打开这份报告**（行内按钮先于整行点击被拦截）。

标题右侧 `★ 收藏（N）` 一键切到收藏视图：列表只显示收藏，标题变 `收藏（N）`，
按钮变 `← 全部（140）`（不重复同一个计数），再点回到全部。筛选是**纯前端**的——
收藏状态随 `GET /api/reports` 一起返回，切换不发请求。

```
POST /api/reports/favorite  {"id":"R-xxx","favorite":true|false}
  → config/favorites.json 落盘 → 返回 {ok, id, favorite, favorites[]}
```

- `favorite` **必须是布尔值**：不接受 `"true"` / `1` / `toggle`，免得前后端状态不一致时静默写反。
- 只接受格式合法（`R-\d{8}-\d{3}`）且**文件真实存在**的报告 ID。
- 收藏文件损坏 / 缺失一律当"没有收藏"，不能把主路径（历史列表）拖垮。
- 前端状态**以服务端响应为准**，不在本地先点亮星星再回滚——写盘失败时点亮的星星会骗人。
- 重渲染列表时手动保留 `scrollTop`；收藏请求在飞时自动刷新直接跳过（按钮会被换掉，
  响应回调打在已脱离文档的节点上）。

### 17.4 列表不是实时的：刷新

页面不会自己知道**命令行 / 另一个窗口**跑完了审查，所以有三个刷新入口：

| 触发 | 行为 |
|---|---|
| 标题右侧「⟳ 刷新」 | 强制重拉，提示「历史报告已更新（+N 条）」或「已是最新」 |
| 窗口重新获得焦点 / 页面从后台切回 | 自动重拉，**8s 节流**（服务端每次要读全部报告 JSON，不能轮询） |
| 审查完成 | 原有链路，`pollJob` 里 already 会 `loadReports()` |

若当前正在看的报告已被别处删除，刷新后明确提示"当前报告已不在列表中"，不让人对着已删内容继续判断。

### 17.5 新增文件

| 路径 | 作用 |
|---|---|
| `config/favorites.json` | 被收藏的报告 ID（`{"favorites": [...]}`，顺序无意义） |
| `outputs/.trash/` | 已删报告与日志的回收区（可自行恢复） |
| `config/report_order.json` | **已废弃**：旧拖拽顺序快照，不再读取 |

### 17.6 顺带修掉的真实 Bug

`outputs/` 下曾出现 `llm-bench-*.json`（测评产物），被 `glob("*.json")` 当成历史报告扫进
`/api/reports`（列表里出现 `id: null`）并让 `/api/integrity` 的 `ok` 变成 `false`。
现约定：**历史报告只扫 `R-*.json`**，测评产物改放 `outputs/bench/`。

---

## 18. 二进制文档上传：PDF / Word / 图片 / 压缩包（v0.3.0）

### 18.1 解决的核心问题

审查引擎原本硬假设输入是纯文本（`review.py` 直接 `read_text(encoding="utf-8")`），
且上传接口「只收 UTF-8、含 NUL 即拒」。本节引入**文档摄取层**：

```
上传二进制 → 原件归档 _originals/ → tools/ingest.py 抽取文本
           → 派生文本进 uploads/ 成为待审对象 → 复用现有文本审查链路
```

好处：**quote 逐字校验、规则匹配、LLM 分维审全部原样生效**，不需要为每种格式
重写一套审查逻辑；同时原件始终保留，抽取结果可与原件对照。

### 18.2 支持矩阵

| 类型 | 扩展名 | 处理方式 | 结果 |
|---|---|---|---|
| PDF | `.pdf` | 有 `pypdf` 用 pypdf，否则内置提取器（zlib + 文本算子） | 派生 `<名>.pdf.txt` |
| Word | `.docx` `.docm` | zipfile 读 `word/document.xml`，解析 `w:t`（含页眉页脚） | 派生 `<名>.docx.txt` |
| 旧版 Word | `.doc` `.rtf` | **拒绝** | 提示另存为 .docx |
| 图片 | `.png .jpg .jpeg .gif .bmp .webp .tif .tiff .heic` | OCR 转写为文本后审查（见 18.7） | 派生 `<名>.image.txt` |
| 压缩包 | `.zip` | 递归展开（成员按各自类型继续处理，保留目录层级） | 每个成员一个待审对象 |
| 文本/代码 | 20 种 | 原样保存 | 直接审查 |

体积上限：文本 **2 MB**，二进制 **20 MB**（前后端一致）。

### 18.3 安全防护

| 风险 | 防护 |
|---|---|
| ZIP 路径穿越（zip slip） | 成员名含 `..` 或绝对路径 → **整包拒绝** |
| ZIP 压缩炸弹 | 解压总量 ≤ 50 MB、成员数 ≤ 200、嵌套深度 ≤ 2 |
| PDF zlib 炸弹 | 单个内容流解压限 8 MB |
| 文本膨胀 | 提取文本上限 200k 字符，超出截断并告警 |
| 派生物污染待审列表 | `_originals/` `_meta/` 是子目录，不进列表 |

### 18.4 诚实标注原则

- **扫描版 PDF**（无文本层）：明确告警「可能需要 OCR 或人工复核」，**不伪造**已审查的结论。
- **图片**：报告的 `limitations` 里写明「未做内容审查，需人工复核或换用视觉模型」。
- 派生文本审查时，报告 `coverage.source` 记录来源（`kind` / `source_name` / `extractor` /
  `pages` / `warnings`），`limitations` 补一条「待审文本由 PDF「a.pdf」抽取生成，
  仅覆盖可提取的文本层；原文件的版式、表格线、图表与内嵌图片未纳入审查」。

### 18.5 可选增强

装 `pypdf` 可显著提升 PDF 提取质量（复杂排版、字体编码、表格）：

```bash
pip install pypdf     # 可选，不装则用内置提取器兜底
python tools/ingest.py 某文件.pdf      # 单独试跑摄取
```

### 18.6 若要真正审查图片内容

当前零依赖方案不做 OCR，也不接视觉模型（`llm_client` 仅支持纯文本 content）。
要支持需两步：① `llm_client.chat()` 接受多模态 content 数组（`image_url` + base64 data URL）；
② `llm_review.build_messages()` 支持附加图片 part。这属于独立特性，本版未包含。

### 18.7 OCR：图片文字转写（v0.3.1）

`tools/ocr.py` 是**多引擎适配层**，按本机条件自动探测与降级：

| 引擎 | 触发条件 | 说明 |
|---|---|---|
| `vision` | `config/ocr.json` 配了 `vision.base_url` + `vision.model` | 视觉大模型（OpenAI 兼容多模态），效果最好、可远程 |
| `winrt` | Windows 10/11 + PowerShell 5.1 | **内置 OCR，零安装**，支持 zh-Hans-CN / en-US |
| `rapidocr` | `pip install rapidocr-onnxruntime` | 纯 ONNX，约 15 MB，无需 torch，跨平台 |
| `tesseract` | 装了 tesseract 二进制 + `chi_sim` 语言包 | 命令行调用 |

```bash
python tools/ocr.py --engines          # 看本机可用引擎
python tools/ocr.py 图片.png            # 单独试跑
```

**图片上传现在的完整流程**：`上传图片 → OCR 转写 → 派生 <名>.image.txt → 进待审列表`，
报告 `limitations` 会注明「由图片「x.png」经 winrt 引擎 OCR 转写生成，识别文本可能存在
识别误差，结论请以原图为准」。所有引擎都不可用时**明确标注未做内容审查**，绝不返回空文本冒充已识别。

**用视觉大模型**（可选，需自己 pull 一个视觉模型）：

```jsonc
// config/ocr.json
{
  "engine": "auto",
  "vision": {
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "qwen2.5vl:3b",     // 先 ollama pull qwen2.5vl:3b
    "api_key": "",
    "timeout": 120
  }
}
```

**两个踩坑点**（已在项目内固化）：

1. `ocr_winrt.ps1` 必须**纯 ASCII + UTF-8 BOM**。PowerShell 5.1 会把无 BOM 的 UTF-8
   脚本按 GBK 解析 → 中文注释乱码 → 整段语法错误。契约测试会卡住这条。
2. PowerShell 变量名**不区分大小写**：脚本里写 `$out = ...` 会覆盖参数 `-Out`。
   脚本内部一律用 `$selfImage` 之类不同名。

---

## 20. 方向感：从「规则执行器」到「独立审查者」（v0.4.0）

### 20.1 问题诊断

用一份埋了约 20 处逻辑矛盾的会议纪要实测（`R-20261023-008`），结果只报出 5 条，
且 3 条是「百分比未标注口径」这种**形式检查**、2 条是口语化用词——**真正的矛盾一条没抓到**。
根因有四个，都不是"模型不够聪明"：

| # | 根因 | 实测证据 |
|---|---|---|
| 1 | **规则闭集**：prompt 写死「只报确实违反上述 rule_id 的问题」，LLM 被降级成规则执行器 | compliance 维提了 6 条候选、**全部被丢弃**，丢弃原因是「quote 非原文（本维度规则清单：- COMP-001…）」——**它把规则清单当原文引用了** |
| 2 | **缺逻辑维度**：16 条规则里没有「内部一致性/论证」这一类 | 数值互斥、时间线冲突、结论与正文矛盾，规则库一条都没有 |
| 3 | **缺审查任务书**：不知道要判定什么、标准是什么、后果多大 | prompt 里只有规则清单，没有用途/标准/后果等级 |
| 4 | **只报不说边界**：报告只说「发现 N 个问题」，不说「没查什么」 | `verdict=warn` + 「需整改后使用」→ 虚假的安全感 |

### 20.2 改造

| 改造 | 位置 | 作用 |
|---|---|---|
| **新增 logic 维度**（8 条规则） | `rules/logic.yaml` | 数值互斥 / 时间线冲突 / 算术不符 / 因果不成立 / 结论与正文冲突 / 样本代表性 / 常识约束 / 人物属性矛盾 |
| **开放发现（OPEN-xxx）** | `tools/llm_review.py` | 规则清单改为「重点提示，**非封闭限制**」；清单外的问题用 `OPEN-XXX` 标识即可进入报告（标 `open_discovery`、severity 取系统默认档）。**quote 逐字校验这条安全线不放宽** |
| **审查任务书** | `config/charter.json` + `review.py:build_charter()` | 声明用途 / 受众 / 后果等级 / 严格度 / 重点关注 / 特别说明，注入每个维度的 prompt |
| **未覆盖声明** | 报告 `coverage.uncovered` | 明确列出「本次没查什么」：未做外部事实核验、未验证链接、未审图片图表、未做法律效力判断、模型存在误差 |
| **截断显式化** | `llm_review` / 报告 `limitations` | 模型输出被 `max_tokens` 截断时（`finish_reason=length`，**包括已救回部分条目的情况**），该维度标注为**「未完成审查」**并进 limitations——**绝不伪装成「没发现问题」** |
| **文本覆盖率** | 报告 `coverage.chars_*` + 前端裁定横幅 | 输入侧截断同样显式化：报告写明「已审 X / Y 字符（Z%），其后 N 字符完全未进入审查」，并排在 `limitations` **最前**。裁定由 `PASS` 改为**「未发现 — 不等于通过」**，因为本引擎存在已知漏报，"通过"是它给不起的结论 |

### 20.3 一条硬结论：本地推理模型做长文档审查会耗尽思考预算

同一份文本（deepseek-r1:8b）实测：

| 输入 token | 输出 token | 正文 | 结论 |
|---|---|---|---|
| 9 | 178 | ✅ 有 | 简单任务正常 |
| 1091 | 4096（打满） | ❌ 空 | `finish_reason=length` |
| 1866 | 4096（打满） | ❌ 空 | 耗时 443s |
| 2171 | 4096（打满） | ❌ 空 | 耗时 443s |

**思考链长度随输入超线性增长**。`think:false` 在 qwen3 蒸馏版 R1 上不生效（实测仍生成 400+ token 思考链）。
所以这是**模型选型问题，不是代码问题**：

- **换非思考型模型**（推荐）：`ollama pull qwen2.5:7b` 或 `llama3.1:8b`——直接输出 JSON，秒级完成；
- 或用云端模型（`deepseek-chat` 等）；
- 或将来实现**长文分块审查**（按段落切块 + 跨块一致性汇总），这是解决长文档的根本办法。

### 20.4 规则库现状

| 维度 | 条数 | 说明 |
|---|---|---|
| fact | 4 | 数字/日期/来源可复现、口径、因果依据 |
| **logic** | **9** | 内部一致性与论证质量（含 1 条自动晋升规则） |
| safety | 4 | 违法指引、隐私、prompt 注入 |
| compliance | 4 | 合同要件、披露口径 |
| style | 4 | emoji、口语化、情绪化、硬编码密钥 |

另有安全审 Agent 专属的 14 条 `SEC-*`（不在 `rules/`，只在 `security-audit` 生效）。

契约测试里的规则数断言已改为**动态计算**，以后加规则不会让测试失效。

#### 执行方式：别把「给模型的提示」当成「代码在跑」

`method` 字段是标签，不是执行器。所以每条规则的执行方式被显式登记在
`tools/rule_exec.py`，并出现在**报告、接口、控制台规则表**三处：

| kind | 含义 | 当前条数 |
|---|---|---|
| `deterministic` | 有专用执行器（事实库 / 算术 / 正则 / checklist / lint / 窄口径逻辑检查） | 23 |
| `declarative` | 规则自带 `pattern`，由 `pattern_rules` 通用引擎执行 | 1 |
| `prompt_only` | **只作为分维 prompt 的提示，完全靠模型判断** | 1（`LOGIC-003` 因果归因） |

报告新增 `rule_execution{deterministic,declarative,prompt_only,executors}`，
并在 `limitations` 明写「以下 N 条规则没有确定性执行器……跨次可能漂移」；
`GET /api/rules` 每条带 `capability{kind,executor,label}` 与 `execution` 汇总。

**逻辑维度的 4 条规则补上了确定性检查**（`tools/logic_checks.py`，只做能明确判定的子集）：

| 规则 | 判定条件 | 置信度 |
|---|---|---|
| `LOGIC-004` 年龄与工龄矛盾 | `N 岁` + `M 年工龄`，N−M < 14 岁 | 0.88 |
| `LOGIC-005` 样本代表性 | 同句「抽样 + 样本量」在前、「所有/全体」在后 | 0.72 |
| `LOGIC-006` 结论与正文冲突 | 结论用「一致同意/全部通过」+ 正文有「待确认/保留意见」 | 0.74 |
| `LOGIC-008` 绝对化表述 | `100%/零缺陷` + 同句（排除自身后）出现竞品/投诉等反证 | 0.76 |

三条防误报的口径：**目标/承诺语境不算事实**（「目标 100%」不报）、
**否定语境不算风险**（「不存在风险」不报）、**排除绝对化表述自身**
（否则「没有对手」会被当成反证词，构成循环论证）。

### 20.5 质量评测：别只看「命中几条」（`tools/eval_review.py`）

压力测试只统计了「14 条里命中几条」，既没有 precision，也没有重复运行——
单次结果无法区分「能力稳定」与「这次走运」。评测工具补上这两块：

```bash
python tools/eval_review.py --runs 3              # 默认对抗样本，跑 3 次
python tools/eval_review.py --runs 3 --no-llm     # 纯规则基线
```

- **标注**：`targets/<样本>.labels.json`，每条错误是「AND-of-OR 正则」，匹配
  `quote + evidence + suggestion + rule_text`；
- **必须排除风格维度**：风格规则会整句引用问题句，看起来像命中、实际没识别出矛盾。
  若不做 `exclude_dimensions: ["style"]`，同一份报告会从 7/14 虚高到 8/14；
- **稳定性**：多次运行取交集，区分「每次都命中」「时有时无」「从未命中」；
- **隔离**：每次运行把 `OUTPUTS/LOGS/LEDGER` 指向临时目录，不污染真实历史报告与台账。

实测（对抗样本 1,780 字符，`deepseek-chat`，3 次）：

| 指标 | 结果 |
|---|---|
| 召回 | 64% / 79% / 64%（平均 **69%**） |
| 稳定命中 | 8 条（E01/E02/E07/E09/E10/E12/E13/E14） |
| 时有时无 | 3 条（E03/E04/E05） |
| 从未命中 | 3 条（E06/E08/E11） |
| 标注集合两两 Jaccard | **80~82%** |
| 全量 issue 集合两两 Jaccard | **48~56%** |

两个值得注意的结论：

1. **标注覆盖相对稳定（80%），但清单内容只有一半稳定（48~56%）**——
   意味着「大方向每次都能看到，具体条目每次换一批」。把这份清单当逐条核对表用是不可靠的；
2. 机读标签在 `R-20261023-014` 上复原出 **7/14**，与压力报告的人工统计完全一致——
   这说明标签判定标准是对的，后续召回数字可以和人工结果直接比较。

### 20.6 确定性算术层：把「算一下就知道」的题从模型手里拿走（`tools/arith_check.py`）

压力测试稳定漏报的 6 条里，有 4 条是纯算术：日期-星期、日期差、加总、占比。
模型不是"不够聪明"，而是**我们让它做它结构上做不稳的事**——它会把
`360+210+480` 算成 1080，也会说 `2026-10-18` 是周六。搬到代码里就变成 100% 可复现。

| 检查 | 依据 | 规则 | 置信度 |
|---|---|---|---|
| 分项加总 | `360+210+480=1050` vs 文中 1080 | LOGIC-001 | 0.95 |
| 日期-星期 | 查日历（`2026-10-18` 实为周日） | LOGIC-002 | 0.97 |
| 起止日期 vs 历时 | `7/1→10/15` 共 106 天 ≈ 3.5 个月 vs 文中 4.5 | LOGIC-002 | 0.93 |
| 占比 vs 人数/总数 | `50/220 = 22.7%` vs 文中 25% | LOGIC-001 | 0.82 |

**误报控制（关键）**：这类规则一旦误报，会直接污染本来信噪比就只有约 1/4 的清单。
所以只在关系明确时开火：

- 合计必须有「三项/四项」这类**计数词**，否则不判断（`合计 900 万` 这种无法确定被加数）；
- 百分数不当金额参与求和（`费用率 45%` 不会被加进去）；
- 日期-星期**年份不确定时直接跳过**，绝不猜；
- 占比要求全篇只有**唯一**的「员工总数」，有多个口径就放弃；
- 容差覆盖正常四舍五入（金额 0.2%、占比 2 个百分点、月数 0.75）。

**实测（同一份 1,780 字符对抗样本）**：

| 指标 | 改造前 | 改造后 |
|---|---|---|
| 召回（3 次） | 64 / 79 / 64%（均值 69%） | 71 / 79 / 71%（均值 **73.7%**） |
| 稳定命中 | 8 条 | **10 条** |
| 时有时无 | 3 条 | **1 条**（仅 E05 归因冲突） |
| 标注集合 Jaccard | 80~82% | **91~100%** |
| 纯规则（无 LLM）召回 | ~3/14 | **6/14（43%）**，零 token 成本 |

自洽数据上的误报验证：`stress_mid_001.txt` 含 23 处「三项合计」，**22 处核对通过、
仅 1 处真错误命中**；契约测试锁住了这条（`test_self_consistent_sample_has_no_arith_false_positive`）。

**这一步也顺带暴露了压力报告的一个方法论错误**：报告称「mid 档全文可见仍漏 T1-T3，
说明漏报源于跨段推理能力」。但当前 `stress_mid_001.txt` 里**根本没有 T1-T3 这三句**
（它们只存在于 `stress_long_001.txt`）。所以那条结论不成立——样本与报告不一致，结论无法复现。

### 20.7 可用性迭代：同类聚合、提示词收敛、语义审缓存（v0.4.7）

整改之后清单仍然"能对但读不动"（长文 161 条）。三项改动：

| 改动 | 做法 | 效果（40,990 字长文） |
|---|---|---|
| **同类聚合** | 只对**提示类**规则（`FACT-004`/`COMP-004`/`STYLE-002/003`）聚合成一条：保留示例 + 总处数 + `aggregated_ids`，其余进 `suppressed` | 161 → **62 条** |
| **提示词收敛** | 加三条硬要求：宁少勿滥（拿不准就不报）、同类只报一处、同句同矛盾只报一条 | 模型自否 189 → **54**；短样本清单 33–37 → 29–32 |
| **语义审缓存** | key = `prompt版本 + 维度 + 模型 + 原文块 + 规则集`，只缓存未被截断的结果 | 同文件重审：30.6s / 9.6k token → **0.1s / 0 token** |

**逻辑类错误绝不聚合**——同一条规则下的每一处都可能是独立真错误，
聚合会把真问题藏起来（契约测试锁了这条）。

**短样本 3 次对照**：召回仍 100%（14/14），标注命中率 **77–79% → 79–87%**。

### 20.8 盲测协议（校准真实召回）

自己的考卷考 100 分不等于真实水平。真实召回必须用**没被设计过**的文档测：

```bash
python tools/eval_review.py --sample targets/real_contract_01.md --init-labels 12
# 人工盲标 desc / match（不看任何模型输出）
python tools/eval_review.py --sample targets/real_contract_01.md --runs 3
```

完整流程、标签编写规则、结果解读见 [`docs/盲测协议.md`](docs/盲测协议.md)。

### 20.9 完整整改记录

P1–P5 与额外 6 项缺陷的逐条修复、前后数据对照、以及仍未解决的 6 项风险，
见 [`docs/修复报告-压力测试问题整改.md`](docs/修复报告-压力测试问题整改.md)。
核心结果：**标注召回 69% → 100%（3 次全中）**、**长文覆盖率 30% → 100%**、回归 115 项全绿。

---

## 21. LLM 配置记忆与超时（v0.4.1）

### 21.1 「上次用的模型」会被记住，可一键切回

| 能力 | 说明 |
|---|---|
| 持久化 | 当前配置存 `config/llm.local.json`（优先级最高，重启后仍在） |
| **配置记忆** | 新增 `config/llm_history.json`，保留**最近 12 条**用过的「端点 + 模型」 |
| 记录时机 | **保存成功**和**测试连接成功**都会记一笔——测试过但没保存的模型也能找回来 |
| 一键切回 | LLM 弹窗里的「最近使用」列表，点击即填入表单（点「保存」后生效） |
| 密钥安全 | 记忆里**只存端点与模型**，不存 API Key 明文（只记 `api_key_set` 布尔值） |
| 换端点保护 | 切到不同 host 时不沿用旧 Key，需重新填写 |

新增接口：`GET /api/llm/history`、`POST /api/llm/history/restore {base_url, model}`。

### 21.2 修掉「测试连通性成功了却提示超时」

根因是三处叠加，都已修：

| # | 问题 | 修复 |
|---|---|---|
| 1 | `timeout` 校验上限 300，而思考型模型需要 450 → **点「保存」直接被拒**（提示一闪而过，很容易错过） | 上限放宽到 **600**（前端输入框同步） |
| 2 | `/api/llm/test` **忽略你在表单里填的超时**，硬编码 `min(已保存值, 60)` → 思考型模型必然超时 | **草稿优先**：用你刚填的 timeout / max_tokens，上限 600s |
| 3 | 连通性探针只给 256 token，而思考链就要 525 token → 端点明明通着，回复却是空的 | 探针预算提到 **≥1024**，思考型模型也能拿到正文 |

超时错误现在会明确说明原因与建议：

> 请求超时（300s 内未返回）——思考型模型思考链可能很长，请调大「超时（秒）」后重试

测试成功后的状态行也会显示当前生效的超时上限，避免把「延迟 4287ms」误读成超时：

```
连接成功 · deepseek-r1:8b · 延迟 4287ms · 超时上限 300s · 回复「可用」
```

### 21.3 关键教训：`max_tokens` 必须够写完 JSON

同一份会议纪要（含约 20 处矛盾），三次审查的实测对照：

| 报告 | 配置 | issues | 发生了什么 |
|---|---|---|---|
| `R-20261023-008` | 本地 deepseek-r1:8b（改造前） | 5 | LLM 4 维调用中 3 维失败；compliance 维 6 条候选因「引用了规则清单」被全丢 |
| `R-20261023-011` | 云端 deepseek-chat，`max_tokens=2048` | 5 | **模型答对了**（content 3810 字符，正确抓到算术矛盾），但 JSON 写到一半被截断 → 解析失败 → **整条丢弃** |
| `R-20261023-012` | 同上 + `max_tokens=8192` + 截断容错 | **23** | 0 截断；**11 条规则外独立发现**；抓出全部人工标注的矛盾 |

所以做了两件事：

1. **默认 `max_tokens` 提到 4096**（`config/llm.yaml`），本地覆盖也建议 8192——
   结构化 JSON 输出（每条含 quote+evidence+suggestion）很容易超过 2000 token；
2. **截断容错解析**（`tools/llm_review.py:_salvage_objects`）：输出被截断时，
   **救回已经写完的完整对象**，而不是因为最后一个对象没写完就整条扔掉。
   「模型答对了却因截断全丢」是最可惜的失败模式。

---

## 22. 规则集版本：区分「升级」与「篡改」（v0.4.2）

### 22.1 触发的问题

新增 `rules/logic.yaml` 后，控制台右上角出现红色告警：

```
✗ 审计链或报告校验异常
存在断链或报告自洽失败
```

**但 ledger 链其实完好无损**（17 条记录、prev_hash 逐行衔接无误）。真正原因是：
`rules/` 目录的 hash 变了 → 14:11 之前生成的 8 份报告记录的 `rules_dir_hash` 全部失配
→ 被判为「报告自洽失败」→ 全红。

**这是设计缺陷**：升级规则库是正常维护动作，却和「报告被人偷改」共用同一个告警，
用户完全无法分辨。

### 22.2 修法

| 机制 | 说明 |
|---|---|
| **规则集版本号** | `config/rules_version.txt`（如 `v1-2026-10-23`）。**改 `rules/` 时必须同步升版本号** |
| 报告记录版本 | 每份报告的 `integrity.rules_version` 记录生成时的版本号（ledger 节点也记） |
| 判定分级 | hash 不匹配 **且** 版本号变了 → **stale**（规则演进，黄色提示，不算失败）<br>hash 不匹配 **且** 版本号没变 → **篡改**（红色，必须告警） |
| 前端分级 | 绿=链完整且无旧报告；黄=有 stale 报告，提示「规则升级后历史结论可能需重审」；红=链断或报告被改 |

> 版本文件必须放在 `rules/` **之外**，否则它自己就会改变 `rules/` 的 hash。

**升级规则库的正确姿势**：

```bash
vim rules/xxx.yaml                 # 改规则
echo "v2-2026-10-30" > config/rules_version.txt   # 同步升版本号
```

忘记升版本号 → 旧报告会判为**篡改**（这是刻意的：绕过版本机制偷改规则库必须被发现）。

---

## 23. 原文标注视图（v0.4.3）

### 23.1 问题

原来只有「问题清单」，看不出**原文的哪一块有问题**——得自己逐条比对原文才能定位。

### 23.2 现在的报告页结构

```
裁定横幅 → 原文标注 → 问题汇总（按类型归类）→ 问题明细 → 本次审查边界
```

### 23.3 原文标注

| 要素 | 规则 |
|---|---|
| 原文快照 | 报告新增 `target_text` 字段——**与 `issue.quote` 的校验基准完全一致**，不受原文件之后被改动/删除影响 |
| **颜色 = 问题类型** | 事实(琥珀) / 逻辑(紫) / 安全(红) / 合规(蓝) / 风格(绿) 五色 |
| **下划线 = 严重度** | blocker 4px 实线 / major 3px / minor 2px / nit 1px 虚线 |
| **角标 = 第几条** | 高亮片段末尾带一个深色小角标，如 `逻辑3`、`事实7`——**表示这是该类型的第几个问题** |
| 规则清单外的发现 | 文字**加粗**标示（这些是模型独立发现、没有规则背书） |
| 悬停 | 显示编号 + 问题 ID + 维度 + 规则 + 严重度 + 置信度 + 判断依据 |
| **重叠片段合并** | 同一段原文常被多个维度同时命中（如「既是事实不符、又是逻辑冲突」）——**合并而非丢弃**，角标并列显示，tooltip 列出该处全部问题 |
| **双向定位** | 汇总表每行可点击 → 原文滚动到该处标注并**闪烁高亮**；**点原文高亮 → 回跳并高亮汇总表对应行**。同一 quote 多处出现时反复点击循环跳下一处。旧报告无原文快照时明确提示"请重新审查"，不静默失效 |
| **单类型跳转** | 一句话被多个类型命中时：**点角标**只看该条（角标加粗描边）；**点整段**弹出类型选择器（`逻辑1 / 事实3 …`，Esc 或点外部关闭，首项自动聚焦），选中后只高亮汇总表对应那一行，不再一次性全高亮 |
| **压暗聚焦** | 定位时把**其余高亮降到 26% 透明度 + 去饱和**，只让目标句满亮度并加描边——满屏都是高亮时，"压暗背景"比"加强目标"更容易找到。回跳汇总表时其余行同样压暗到 35% |
| **落点精度** | 用 `getBoundingClientRect` 差值计算（**不能用 `offsetTop`**：`.anno-text` 无 `position`，`offsetParent` 是外层元素，会重复叠加整页偏移，实测偏 900px）。平滑滚动后**轮询闭环补差**直到残差 ≤2px，兜底 4 次后强制对齐 |

> **编号规则**：按维度分组（逻辑 → 事实 → 安全 → 合规 → 风格），组内按 **严重度 → 置信度** 排序后编号。
> 「原文角标」与「下方汇总表的编号列」**共用同一套编号**，可以互相定位：
> 原文里看到 `逻辑3`，到汇总表里找编号为「逻辑3」的那一行即可。
> 同段原文被多维命中时会出现多个角标（如 `逻辑1 事实1`），表示该处同时属于多类问题。

> 踩过的坑：第一版用「去重叠」策略，导致 25 条问题里 **10 条在原文里没有高亮**（被更长的同段 quote 挤掉），
> 等于漏报。改为合并后 **25/25 全部可见**（15 处高亮，其中 9 处是多维合并）。

### 23.4 问题汇总

按**类型**分组，组内按 **严重度 → 置信度**排序，每条含：严重度标签 / 规则 ID（规则外发现打「规则外」标签）/
位置 / 判断依据摘要。类型顺序：逻辑 → 事实 → 安全 → 合规 → 风格（问题影响面从大到小）。

**「问题摘要」默认全文直出：不截断、不折叠、不留按钮。** 一条问题的结论往往在最后半句
（"…未说明修改的范围与验证方式，结论强度与证据强度不匹配"），任何形式的一次点击都是多一道门槛。
代价是表格更高——刻意的取舍：可读性优先于紧凑。

> 演进过三版，每一版都是上一版的失败教训：
> ① `evidence.slice(0, 110)` 硬截断——后半句直接砍掉、连省略号都没有，读者以为这条就说到这儿；
> ② 折 3 行 +「展开全文（N 字）」按钮——还是得点一次才看得全，且窄栏下 3 行只剩二三十字；
> ③ 现在直接给全。
> 截断现场（R-20261026-011）：`OPEN-QUANT`「…而求职意向为『AI Age」、`OPEN-CONFLICT`「…一边」都断在句子中间。

实测（会议纪要样本）：25 条问题 → 事实 8 / 逻辑 5 / 安全 4 / 合规 5 / 风格 3，其中 **8 条为规则清单外发现**；
编号分配为 逻辑1~5 / 事实1~8 / 安全1~4 / 合规1~5 / 风格1~3，**25/25 全部有唯一编号**。

---

## 24. 多 Agent 框架：判定标准成为数据（v0.5.0）

最初设定是「各种项目上都用 Agent」——安全有安全审 Agent，文档有文档审 Agent。
但到 v0.4.9 时整个项目只有一个 Agent：维度映射、五个维度函数的调用顺序、
规则来源、任务书全部写死在 `review.py` 里，加一个场景就要改主流程。

现在 `Agent` 被定义成数据（`agents/<id>/manifest.json`）：

```json
{
  "id": "security-audit",
  "auto": false,
  "rules": ["rules/safety.yaml", "agents/security-audit/rules"],
  "dimensions": [{"id": "injection", "label": "注入", "handler": "pack:injection"}],
  "dims_by_type": {"code": ["secret", "injection", "config", "dependency", "safety"]},
  "checks": [],
  "boundaries": ["只做静态写法匹配，不做数据流跟踪"]
}
```

主流程退化为「按 manifest 调度」：维度处理器统一为 `(session, text, rules, ctx)`，
`builtin:*` 复用已有实现，`pack:*` 指向 `agents/<id>/checks.py` 的 `CHECKERS`。

### 两个 Agent 的差别（同一份文件，两套标准）

| | doc-review | security-audit |
|---|---|---|
| 判什么 | 文字是否准确/合规/得体 | 写法是否可被利用 |
| 维度 | 事实/安全/风格/合规/逻辑 | 凭据/注入/配置/依赖/内容红线 |
| 规则 | 24 条全局规则 | 18 条（含 14 条 SEC-*） |
| 自动路由 | 是（默认） | **否**，必须 `--agent`（换标准不能悄悄发生） |

安全审的判定条件全部写在 YAML（`pattern/require/guard/unless/confidence`），
`checks.py` 只做通用匹配；加一条规则不用改代码。

### 实测

| 样本 | 结果 |
|---|---|
| `targets/security_demo/vuln_app.py`（植入 10 处） | **10/10 命中**，verdict=fail |
| `targets/security_demo/safe_app.py`（全安全写法） | **0 条**（参数化查询/shell=False/safe_load/环境变量凭据） |
| `targets/security_demo/requirements.txt` | 3 条（git+ 直连 major、2 条未锁版本 minor） |

### 可追溯

报告新增 `agent{id,version,selected_by,dimensions,checked_dimensions,checks,boundaries}`，
integrity 新增 `agent_dir_hash / agent_version`：**改 manifest 或自带检查器会被发现**，
版本号变了算演进（stale），版本号没变却改了 hash 算篡改。

同时补了 `config/knowledge_version.txt`——此前改 `knowledge/facts.json` 没有版本号，
让 **87 份历史报告全部 FAIL**；现在与 `rules_version` 同一套逻辑：版本变 → stale。

完整设计、新增 Agent 的 5 个步骤、以及这套抽象的边界，见
[`docs/多Agent框架.md`](docs/多Agent框架.md)。

---

## 25. 自学习闭环：提案 → 门禁 → 受控晋升（v0.6.0）

「审查后如果有新的审查方向，能加进规则库」——方向对，但**不能是「审查完自动写规则」**。
一旦被审文档能影响规则，被审对象就获得了修改裁判尺度的能力（第二轮 c04 已经证明
文档里的注入句式能操控审查语义）。所以做成了：

```
人工标注（唯一可信信号）→ 挖掘聚类（数字泛化）→ 全语料门禁 → 预演 → 晋升/回滚
```

**四条结构约束**

1. 候选在 `state/candidates/`，**不在 `rules/`**（`rules/` 是被哈希覆盖的受审目录）；
2. `review.py` 里没有任何写 `rules/` 的代码；
3. **HTTP 面上没有写规则的能力**：`/api/annotations` 只写标注，`/api/learning` 只读；
   写库只有 `tools/rule_promote.py` 一条路径（契约测试锁住）；
4. 只增不改不删，`rule_id` 已存在即拒绝。

**门禁指标**（全语料 43 份，带候选 vs 不带）

| 指标 | 门槛 | 为什么 |
|---|---|---|
| `hit_doc_count` | ≥2 | 单文档学出来的规则必然过拟合 |
| `offtarget_count` | **0** | 误报的代价是"人不再看清单"，一条就拒绝 |
| `suppressed_others` | ≤0 | 新规则不该改变别的判定的可见性（`COMP-004` 的既定去重除外） |

**实测（`targets/learning_demo/`，两类真实漏检）**

| 候选 | 结果 |
|---|---|
| 「同比下降 -3.2%」类 | **通过** → 曾晋升为 `AUTO-LOGIC-BE1B78`，**第四轮判定为误报放大器后已回滚** |
| 「占比合计 110%」类 | **被拒**：它把数字自洽的对照组（合计 100%）也报了 → 只学了措辞没学语义 |

**审批分级**：`safety/compliance/fact` 只能人工确认（`--confirm-manual`）；
`logic/style` 可走 `--auto`，但每周 ≤3 条、严重度上限 minor。

**治理**：每次写库都 快照 → 只新增 → `rules_version` 自增 → 追加审计行；
`rollback` 可一键回退。不升版本就改 `rules/` 会被判为篡改（红），升了版本历史报告转 stale（黄）。

> 这套机制当场抓到我自己一次违规：改完 Agent 目录忘了升 manifest 版本，
> 已有报告立刻被判「疑似判定标准被改」。

**它不是机器学习**：没有模型、不学语义，只做「人工标注 → 措辞模板 → 门禁过滤」的
监督式规则归纳。局限与风险写在 [`docs/自学习闭环.md`](docs/自学习闭环.md) 第 8 节。

### 第四轮对抗测试后的加固（v0.6.2）

| 修补 | 说明 |
|---|---|
| 门禁改**句级** | 新增 `internal_offtarget`：命中必须对上标注指认的那句，否则同文档里的正常句就能绕过 0 误报 |
| 晋升**重算** | `promote` 不再信任候选 JSON 里的 `gate.passed`/`shadow_at`，一律重跑；候选带 `content_hash` 防「过门禁后偷换 pattern」 |
| shadow 上限成闸门 | 新增条数超限直接拒绝（原来只打印 ⚠） |
| 标注作者白名单 | 只有 `identity.trusted_authors` 里的作者能作为学习种子；HTTP 面可选 token |
| 漂移复评 | `rule_promote.py audit`：按当前语料复评已晋升规则，不通过自动回滚 |
| 评估串行化 | `state/.evaluate.lock`，并发评估直接拒绝，避免互相覆盖输出目录 |

演示用的 `AUTO-LOGIC-BE1B78` **已回滚**：它把「同比下降 -3.2%」这类正常财务陈述报成问题，
证明措辞级规则不该进产品规则库。

---

## 26. 第六轮：规则库盲区与冗余门禁（v0.6.3）

第六轮对抗测试构造了 11 类「未在规则库之中」的文本，实证结果是
**0/11 被规则层发现，另有 7 份样本被 COMP-004 刷了一条与内容无关的噪声**。

修复的重点不是补 11 个句子，是补 11 个**判定面**：

| 类别 | 修法（结构，不是词表） |
|---|---|
| B1 抽样推全域 | 抽样词 + 全域范围 + **结论动词** + 样本量先于范围；已限定范围的结论不报 |
| B2 从业 N 年 | 工龄写法改多模式并列 |
| B3 毫无问题/绝对领先 | 新增「不可证伪的绝对化」分支，不依赖同句反证 |
| B4 覆盖率 150% | 占比类指标 > 100 判越界；指标与数字间有增长/同比则放过 |
| B5 银行卡号 | **Luhn 校验**主通道 + 四段式分组降级通道 |
| B6 住宅地址 | 市区县+路街+门牌 **且** 带单元/室 双条件 |
| B7 刷单返利 | 违法操作扩一整类（刷单/跑分/代开虚开发票/伪造公章/洗钱） |
| B8 系统设定已被覆盖 | 按「授权主张 + 免除约束」语义结构识别 |
| B9 大小为写金额不一致 | `cn2num()` 换算大写与括号内数字比对 |
| B10 截止/截至 | 只认「截止 + 时间点」，不碰「截止日期/截止时间」 |

**P0 是 COMP-004 的判定面**：从「句子里有数字」改成
「指标类数字 × 外部数据语境」，并排除金额/账号/门牌/年龄语境。
全语料 COMP-004 从 18 条降到 0 条，正例（外部统计无来源）仍能命中。
理由很简单：**"有数字"不等于"披露了数据"**，而内部经营数字本身
就是原始来源，要求它给自己标口径只会制造噪声。

### 冗余门禁：补了执行器就不再走学习闭环

补完 `LOGIC-004` 之后，原本能过门禁的候选 `AUTO-LOGIC-B61926` 指标依然全绿，
但它命中的两处**已经由 LOGIC-004 报出**——门禁原来只问"会不会误报"，
没问"有没有必要"。新增第五道门 `redundant_count ≤ 0`，拒绝理由写明
"新增这条不增加检出能力，只是重复报同一处问题"。

### 验证

```
tests/v6_probe.py         盲区样本 a/b 双份 22/22（修复前 12/22）
tests/v6_scan.py          全语料 34 → 16 条命中，COMP-004 18 → 0
tests/test_blindspots_v6  28 项（19 正例 + 9 负例锁误报）
python -m unittest        254 项全绿
integrity verify          0 FAIL，137 STALE（规则集升版，非篡改）
```

完整记录见 `docs/修复报告-第六轮.md`。

### 仍然做不到的

事实库外实体（仅 2 条断言）、跨块长距离矛盾、因果归因（LOGIC-003）。
这三项**显式登记为盲区**，在报告 `coverage.uncovered[]` 里如实列出——
宁可承认"没查"，不制造"查过了"的错觉。


---

## 27. 审查目的：一句话 → 目标 → 证据（v0.7.0）

之前只能"全维度泛扫"：审完给一张清单，由你自己判断哪些跟你的目的有关。
现在可以在审查台左栏写一句**目的**：

> 这是给客户的报价邮件，帮我找有没有承诺过头的地方

报告会多出一块「审查目的与目标证据」：识别出的意图 → 派生的审查目标
（**找到 / 未找到**都列）→ 逐字证据；点证据能在原文里定位并高亮。

### 为什么不是"把目的塞进 prompt 让模型自己找"

| 如果交给模型 | 后果 |
|---|---|
| 同一句目的两次派生出不同目标 | 报告无法复核，也没法跟历史比 |
| 模型"引用"了一句原文，原文里没有 | 你要自己回原文核，等于没省事 |
| 没找到证据时输出一段空话 | 你会以为"这个目的没问题"，其实只是没找 |

所以拆成三层，**只有最后一层用模型**：

```
parse()  目的 → 意图 + 目的词 + 目标     确定性（config/purpose.yaml 驱动）
analyze() 目标 → 证据[]（逐字 quote）     确定性（规则命中 / 探针 / 关键词）
judge()   证据 → relation + rationale     可选，模型；只能改判定，不能改 quote
```

模型收到的是**已经定位好的证据清单**，只回 `{id, relation, rationale}`。
它不返回 quote，所以"引用原文"这件事天然不会幻觉；编号对不上就丢弃，
调用失败就回落 `judge.mode = rule`，确定性证据一个不丢。

### 三个刻意的设计

1. **「未找到」也是一个结论**。每个目标都有 `status: found | not_found`，
   并同时写进 `limitations`。只列找到的，会让人以为没列出来的地方都干净。
2. **目的不改变 verdict**。它不提高也不降低严重度——否则换个说法问就能改变结论。
3. **同一句可以服务多个目标**。去重按句合并，但目标归属是列表；
   否则"承诺 15 日交付且质量绝对没问题"会让其中一个目标误报 `not_found`。

### 用法

```bash
python review.py targets/contract_001.md --no-llm \
  --purpose "帮我核对合同金额、大小写与付款条款是否一致"
```

控制台里 `purpose` 存 localStorage，随 `/api/review/start` 提交；
预设了 5 个常用方向（承诺风险 / 金额核对 / 数据来源 / 合同要件 / 对外发布）。

**目的也可以来自文件**：审查目的面板点「上传目的文件」，支持 PDF/Word/图片/ZIP/文本
（与待审对象同一条摄取链，图片走 OCR）。提取的文字暂存 `state/purpose_uploads/`——
**不进 `targets/`**，不会污染待审列表；每份截取前 2000 字、一次最多 5 份，
截断与超限都写进报告的 `purpose.limitations`，不静默丢内容。
运行审查时前端只传文件 id，服务端把手写目的与文件文本组装成一段目的，
经 `--purpose-max <实际字数>` 告知解析上限（手写目的的 300 字预算没有被动过）。

### 验证

```
tests/test_purpose.py                     40 项（解析确定性 / quote 逐字 / 未找到显式 / 模型不改引用 / max_chars）
tests/test_api_contract.py::PurposeApiTests  4 项（透传 / 300 字截断 / 空目的为 null / 前端挂载）
tests/test_api_contract.py::PurposeUploadTests  12 项（多格式摄取 / 截断告警 / 不进待审列表 / 删除 / 随审查进报告）
python -m unittest                        322 项全绿
```

设计说明见 `docs/审查目的.md`；词典在 `config/purpose.yaml`（**改词典必须同步 `version`**）。

### 第七轮：向目的功能投暴力测试（v0.7.1）

一份约 100 个用例的暴力/对抗脚本（`tests/bruteforce_purpose.py`）砸下来，
结果 PASS 33 / FAIL 6 / OBS 11。修完是 **PASS 41 / FAIL 0 / OBS 4**，单测 289 → 302。

| 缺陷 | 根因 | 修法 |
|---|---|---|
| markdown 结构注入 | `_md_safe` 转义字符但**不动换行**，而 md 的标题/表格靠行首生效 | 新增 `_md_flat()`：空白压成一行再转义；文字仍可见，只是失效 |
| 证据偏移 `-1` | `text.find()` 失败返回 -1，`int(-1 or 0)` 仍是 -1 | **取消"调用方传偏移"**：一律由 quote 反查；查不到就剔除并写进 limitations |
| `parse(123)` 崩 | 首行直接 `.strip()` | `str()` 兜底 |
| 清洗只在 HTTP 层 | CLI/`run_one` 直通，`\x00` 进报告 | 收敛成 `purpose.clean()`，三入口共用 |
| judge 注入面 | 目的/引用裸拼进 prompt | `<<<PURPOSE` / `<<<EVIDENCE` 不可信区块 + 双重禁令 |
| 探针盲区（6 处） | 词表 + 正则太窄 | 修 4 处（日内/单位空格/中文数字百分数/折倍），全角空格用**匹配视图 + 位置映射**解决；繁体留作边界 |

关键判断：`purpose.evidence` 现在是**硬承诺**——每条的 quote 都能在 `target_text`
的 `start` 位置逐字读回。写一个假的 0 偏移比写 -1 更糟：它会安静地指错地方。

完整记录见 `docs/修复报告-目的功能加固.md`。

---

## 28. 审查情境：不同用途用不同规则（v0.7.2）

同一份材料，用途不同，**该问的问题**也不同。

简历初筛时，HR 不会去核实「四六级证书是不是真的」——要核实只能索要原件、
查学信网，批量初筛没有这个条件。把这类"未提供佐证"报出来，结果就是：
每份简历都挂一条 `FACT-002`，真正能改的问题（学历年限算错、承诺过头、
隐私信息外泄）反而被淹了。

所以引入**审查情境（scenario）**：选一个用途，就关掉一整类"问了也没法行动"的问题。

### 28.1 现有情境

| id | 名称 | 关闭的规则 | 仍然检查 |
|---|---|---|---|
| `general` | 通用文档审查（默认） | 无 | 五维全开 |
| `resume-screening` | 简历批量初筛 | `FACT-001`（具体标识必须可复现）、`FACT-002`（引用必须可定位） | 逻辑矛盾、合规与隐私风险、岗位匹配、表述风格 |

配置在 `config/scenarios.yaml`（**版本化**，改动要升 `version`，报告会记下它）。
`tools/scenario.py` 负责加载、校验、过滤。

### 28.2 三条硬规则

1. **只决定"问不问"，不改判定标准，也不改严重度。**
   关掉的是「这一类问题本次不问」，不是「这类问题从轻」。严重度仍由规则库决定，
   模型无权改级——这条从 v0.1 起就没松过。
2. **关了什么必须写进报告。** 报告带 `scenario.disabled_rules`（含每条规则的原意
   `disabled_detail`），`limitations` 里写明"未报出资质问题 ≠ 资质已核实，
   需要时请单独索要证明材料"。不这么写，等于把「未发现」偷换成「通过」。
3. **配置错了要报错，不静默回退。** 未知情境 → 启动即失败（不先起任务再失败）；
   把所有规则都关光 → 报错；配置里写了规则库中不存在的 ID → 不算错，
   但会把 `FACT-999` 之类的 ID 列进 limitations，避免"以为关了其实没关"。

### 28.3 实测：同一份简历，两种情境

同一个文件、同样带目的、同样走 LLM 语义审：

| | 通用情境 `R-20261026-011` | 简历初筛 `R-20261027-001` |
|---|---|---|
| 异议条数 | 6 条 | 3 条 |
| 裁定 | warn | 未发现 — 不等于通过 |
| 资质类 | `FACT-001` 项目地址不可达、`FACT-002` 学历无佐证、`FACT-002` CET-4 无佐证 | 全部不再出现 |
| 仍然报出 | 学历年限推算、项目参与度不可量化、工作边界表述冲突 | 同左（一条没少） |
| 参与规则数 | 25 | 23 |

那条 CET-4 就是典型：通用情境下它是"无出处的资质断言"，初筛情境下它不该进清单——
HR 拿到这条只能去要证书，而这在批量初筛里不会发生。

### 28.4 用法

```bash
python review.py targets/xxx.txt --scenario resume-screening --no-llm
python review.py --list-scenarios          # 看有哪些、各关了什么
```

控制台左栏「审查情境（用哪套规则）」下拉；`POST /api/review/start` 带
`{"scenario": "resume-screening"}`；`GET /api/scenarios` 取清单。
报告状态条会挂一个蓝色 chip：`情境：简历批量初筛 · 关闭 2 条规则`，
悬停能看到每条被关规则的原意。

新增情境 = 在 `config/scenarios.yaml` 加一段，**不改代码**。
但请想清楚两件事：关掉的规则是不是真的"这一用途下无法行动"，
以及 `extra_limitations` 有没有把"没问"说清楚。

---

## 29. 宽屏下「双向定位」失效：滚错了容器（v0.7.2）

**症状**：点原文里被标注的句子，汇总表对应行**确实高亮了**（`is-active` /
`is-row-flash` 都加上了，提示也显示"已定位"），但页面**一动不动**。
反方向——点汇总表的「定位 →」跳原文——却正常。

**根因**：宽屏（≥1101px）下 `#page-review .grid-review > .card` 是

```css
max-height: calc(100vh - nav - gap);
overflow-y: auto;
overscroll-behavior: contain;
```

的**独立滚动容器**。于是整页 `document.documentElement.scrollHeight === innerHeight`
——`window.scrollTo()` 一点都滚不动。

而旧实现里：

- 「汇总表 → 原文」滚的是 `#annotatedText` **内部**（`box.scrollTo`）→ 有效
- 「原文 → 汇总表」滚的是 `window` → **完全无效**

这就是"一个方向能用、另一个方向不能用"的原因，也是最容易误判成"前端坏了"的地方。

**修法**：新增 `scrollHost(el, skipEl)` 找**最近一个真的能纵向滚动的祖先**
（用 `getComputedStyle(p).overflowY` + `scrollHeight > clientHeight + 1` 双重判断，
因为只设 `overflow-x: auto` 时另一轴的 `visible` 会计算成 `auto`），
再由 `scrollHostTo()` 滚动它并**校验落点**：

- 落点基准最初写成「容器可视区的顶部」（`hr.top + pad`），不是绝对的 12px
  ——实测用绝对值会把目标行推到容器外面（落点 13px，而卡片顶部在 82px）；
  后来改为**可视区中线**，见 §30；
- 平滑滚动是异步的，滚完 200ms 量一次，差 >2px 就直接写死（`auto`）。
  哪怕没有动画，也不能停在半路。

教训：**同一份页面上同时存在两种滚动容器时，"能用"和"不能用"可能只差一个方向**。
测试锁死：`test_reverse_jump_scrolls_inner_container` 禁止退回"只滚 window"的写法。

---

## 30. 跳转落点：从「贴着模块顶部」改成「屏幕正中间」（v0.7.3）

**症状**（截图反馈）：点「定位 →」跳原文后，被标注的句子**贴在卡片最上沿**，
长句的第一行还会被卡片标题压掉一半，视线得自己往下挪才能读到；
反方向（点原文 → 汇总表）同样顶在表格顶端。

**根因**：落点算的是「顶边对齐」——

```js
// 旧
const topSafe = hr.top + (isWin ? NAV_HEIGHT : 0) + 12;
scrollHostTo(host, base + (r.top - topSafe));
```

`topSafe` 的语义是"刚好不被遮住"，对"看得到"够用，对"看得清"不够：
目标贴在容器上沿时，读者要在大片文本里重新找锚点。而且原文框
（`#annotatedText`）在卡片里**本身就不居中**——上方有标题/图例、下方还挂别的区块，
框中线比屏幕中线高 50px 以上，所以"居中在框内"和"居中在屏幕上"是两件事。

**修法**：统一走 `centerScrollTop(rect, host)`

```js
// 目标中线对齐可视区中线；高度 ≥ 可视区 70% 的长片段才退化为顶部对齐
return base + ((rect.top + rect.height / 2) - (b.top + viewH / 2));
```

三处配合：

1. `viewportBounds(host)` 统一给出可视区上下边界（window 时避让 sticky 导航的
   `NAV_HEIGHT`，容器时用 `getBoundingClientRect`）；
2. `revealInViewport()` / `ensureMarkInViewport()` 都改判「**中心偏离中线**」
   而不是「是否被裁切」——句子卡在上沿时技术上"可见"，但用户根本不知道该看哪儿；
   偏差在可视区 12%～15% 的舒适带内就不动，避免每次点击都轻微抖一下；
3. `jumpToIssue` 从 `revealInViewport($("#annotatedBlock"))`（顶上去原文整块）
   改成 `revealInViewport(mark, { skip: box })`——对准被标注的句子本身，
   并跳过 `#annotatedText`（它由 `centerMarkInBox` 负责）。

**残余差**：框内已居中、外层又滚不动时（宽屏整页 `scrollHeight === innerHeight`），
屏幕上看还是偏上。于是 `settleInBoxToViewportCenter()` 在落位**稳定之后**
（提前做会被下一轮 `go()` 拉回去，两者打架）把「标注中心 vs 屏幕中线」的差
补到框内滚动上。两条护栏防止"为了居中把句子推出框外"：

- 屏幕中线必须真的落在框内（`aim ∈ [24, clientHeight-24]`），框很矮又贴着卡片顶部时不硬推；
- 补差后的 `scrollTop` 必须仍在 `[0, scrollHeight - clientHeight]` 内，到头的就认了。

**验证**（Edge headless 注入探针，报告 `R-20261026-011`，6 条问题 × 2 种宽度）：

| 宽度 | 布局 | 首条偏移 | 末条偏移 | 6 条最大偏移 | 目标是否都在框内 |
|---|---|---|---|---|---|
| 1440 | 宽屏三栏 | −13px | −13px | 14px | 是 |
| 1000 | 单栏纵向 | +28px | +28px | 28px | 是 |

修复前同一坐标是「模块上沿对齐」——偏差 300px 量级。
窄屏的 +28px 是**刻意**的：那里对齐的是「导航条以下的正文区」中线，
屏幕几何中线再往上 28px 就被 sticky 导航占了。

测试锁死：`test_jump_target_lands_at_vertical_center`（公式、两处调用、
`skip: box`、长片段兜底、settle 的两条护栏），并禁止退回
`hr.top + (isWin ? NAV_HEIGHT : 0) + 12` 的顶部对齐写法。

---

## 31. 报告里出现「此处表述一致」的 issue：模型把"核对完没发现矛盾"当成了命中（v0.7.3）

**症状**（用户直接质问「这个逻辑存在的必要在哪」）：清单里一条 `LOGIC-002 / minor`，
evidence 通篇在讲"2024 级四年制推算是 2028 年毕业"，最后落在
**"此处两处表述一致……属时间线依据不足而非直接矛盾"**——一条写着"没问题"的问题。

**先回答"规则还要不要"**：要。`LOGIC-002`（时间线自相矛盾）的确定性部分
（`arith_check:weekday` 日期-星期不符、`arith_check:duration` 日期区间与「历时 N 个月」不符、
非法日期）是真会开火的硬判据，跟模型无关。出问题的不是规则，是**模型用这条规则的方式**：
它把"我把两端都核了一遍，结论是能对上"也输出成了一条 issue。
"依据不足"是审不到（该进 `coverage.uncovered`），不是审出问题。

**修法（两层，源头 + 兜底）**：

1. prompt 第 8 条补一句：比完两端结论是「表述一致 / 不构成矛盾 / 只是依据不足」的**不要输出**；
2. `validate()` 增加「自证无矛盾」判定——`NO_CONFLICT_RE` 命中 evidence **末句**、
且 `CONFLICT_DENY_RE` 不命中 → 进 `excluded`（报告「已排除」区），
不给静默丢弃。两条边界：
   - **只认末句**：结论通常落在最后一句，正文中间的"一致"往往只是铺垫
     （"两处表述一致，但与工时矛盾"——那才是要报的）；
   - **"一致"必须有反义护栏**："与岗位要求**不一致**"是真问题，
     `CONFLICT_DENY_RE` 里 `不一致|不相符|前后矛盾|与…矛盾` 一命中就保留。
     反义表只加不删——宁可多留一条噪声，也不能把真问题静默删掉。

**回放验证**（直接拿 `R-20261027-001` 里那条真实 evidence 塞回 `validate()`）：

| 输入 evidence | 修复前 | 修复后 |
|---|---|---|
| 真实样本（…"两处表述一致"…"而非直接矛盾"） | 1 条 minor 进清单 | 0 条，进「已排除」 |
| "与'阅读、调试'为主的定位存在强度**不一致**" | 1 条 | 1 条（保留） |
| "两处表述一致，**但与工作时长矛盾**" | 1 条 | 1 条（保留） |
| "原文**前后矛盾**：2024级入学却写 2026 年毕业" | 1 条 | 1 条（保留） |

测试锁死：`test_self_consistent_issue_is_retracted`（真实样本回放）、
`test_consistency_filter_never_eats_real_conflicts`（4 个反例必须留下）。

**注意**：已生成的报告是**快照**（带 hash 链、进 ledger），不会被新逻辑改写。
这条修复只对**之后**的审查生效；要看效果需要重跑一次该目标。

---

## 32. 改动登记不能只靠记性：把约定变成退出码（v0.7.4）

**背景**：`CHANGELOG.md` 建立后，登记这件事一直靠"我记得要写"。这轮用户第二次强调
「后续所有增删改都需要写入 md」——说明**约定本身不够**，约定要靠机制兜。

### 32.1 判定只一条规则，越简单越不会被绕过

```
本次改动里存在「值得登记的文件」→ 就必须有 CHANGELOG.md 的改动
```

不做的事（划清界限，避免噪声把人逼去 `--no-verify`）：

| 不做 | 为什么 |
|---|---|
| 判断账本**内容**对不对 | 那是 `ChangelogContractTests` 的活（四字段 + 倒序） |
| 检查日期是不是今天 | 跨天提交、周日补账都会误报 |
| 要求每个文件一条 | 一次改动一条即可，写细了会让人懒得写 |

**"值得登记"的边界**（`must_register()`）：

- 算：`.py .js .html .css .yaml .json .md .txt .ps1 .sh` + `.gitignore/.gitattributes/LICENSE`
  ——`.gitignore` 没有扩展名但改一行会让别人的仓库变样，必须登记；
- 不算：`outputs/ logs/ ledger/ state/ .workbuddy/ __pycache__/`（**跑出来的，不是写出来的**）、
  `config/{favorites,report_order,llm.local,llm_history,learning}.json`（运行时状态）、
  `_probe*/_dom*/_shot*`（验证探针）、`*.pyc/.log/.tmp`、图片。

### 32.2 两个入口

```bash
python tools/changelog_guard.py            # 看工作区（含未跟踪新文件）
python tools/changelog_guard.py --explain  # 逐文件打印"需要登记 / 不登记"
python tools/changelog_guard.py --staged   # 只看暂存区，pre-commit 用
```

退出码：**0 通过 / 1 漏登记 / 2 取不到 git（宽容放行）**。
2 不并进 1，是因为"不是 git 仓库"和"你忘了登记"是两件事，混在一起就会到处误拦。

### 32.3 为什么用 `core.hooksPath` 而不是 `.git/hooks/`

`.git/hooks/` 不进版本控制，换台机器、重新 clone 就失效——**守门自己先丢了**。
把钩子放在 `.githooks/` 并 `git config core.hooksPath .githooks`，
钩子跟着仓库走；配置只写进各自的 `.git/config`，不污染团队。

### 32.4 踩到的坑

`git status --porcelain` 默认把非 ASCII 文件名转成八进制
（`docs/API\346\216\245\345\217\243...`），既看不清，也让 `IGNORE_EXACT` 的精确匹配
全部失效。必须加 `-c core.quotepath=false`。本仓库 `docs/` 全是中文名，这个坑必踩。

### 32.5 测试为什么不 mock git

守门的价值全在"**真的会拦**"。`tests/test_changelog_guard.py` 里 4 项在
`tempfile` 建真仓库、真 `git init` + 真 commit 基线、真跑子进程：

| 场景 | 期望退出码 |
|---|---|
| 改 `app.py` 不写账本 | 1（且输出含文件名） |
| 改 `app.py` 并写账本 | 0 |
| 只新增 `outputs/R-1.json` | 0 |
| `git add app.py` 后 `--staged` | 1 |

踩过：临时仓库若不先 commit 基线，守门脚本自己（复制进去的 `tools/changelog_guard.py`）
会一直挂在待登记清单里——**行为是对的，但测不到想测的东西**。

**边界**：守门只保"有没有登记"，保不了"登记得真不真"。后者靠 code review 和
`ChangelogContractTests`。真要与人为善地绕过：`git commit --no-verify`，
但那时请把条目补上再来一次。

---

## 33. 格式要求不该住进规则库：FORMAT 规则的"标准从哪来"（v0.7.5）

### 33.1 用户问的那句话

> 「为何会写入规则库？我只是这次是这样的格式要求，那我下次是另一个，
> 而你都写入进去，最后我选择哪个标准呢？」

当时 `rules/format.yaml` 里每条规则都写着 `source: 课程报告格式要求 v1`，
数值全部硬编码在 `tools/docx_format_checks.py`：

| 常量 | 值 | 实际含义 |
|---|---|---|
| `OK_EAST` | 宋体/SimSun/新宋体… | **这一门课**允许的正文字体 |
| `SZ_XIAOSI` | 24 半磅 | **这一门课**要求的字号 |
| `FIRST_LINE_OK` | 200 字符 或 400~520 缇 | **这一门课**要求的缩进 |
| `LINE_OK` | 440 缇 + exact | **这一门课**要求的行距 |

问题不是"规则写错了"，而是**层级放错了**：

- `rules/*.yaml` 是**长期裁判尺度**——什么算问题，跨项目复用；
- 「正文宋体小四、固定 22 磅」是**某一份委托方的格式要求**——换个期刊/学校/学期就全变。

把后者写进前者，后果是：下次换标准只能改 Python；规则库会攒下多套互相矛盾的标准；
审的时候没人知道该选哪个。这跟"给所有简历都报一条 CET-4 无佐证"是同一类错误：
**把这一次的尺子，焊成了永久的标准**。

### 33.2 拆成两层

```
rules/format.yaml            只声明「要查哪些维度」（FORMAT-001~006），不含量值
      ↓ spec_keys 指明依赖哪几个键
config/format_specs.yaml     每个规格给出数值（字体/字号/缩进/行距/标题层级/图表编号）
      ↓ tools/format_spec.py 校验并解析
docx_format_checks.run_all(info, ridset, spec)
      ↓ spec 里没写的键 → 该项不检查
报告 format_spec{id,name,version,source,criteria{}}
```

加一份新标准 = 在 `config/format_specs.yaml` 加一段，**不改代码、不升规则版本**：

```yaml
  - id: journal-gbt7714-2026
    name: 某期刊投稿格式
    source: 《XX 学报》2026 年投稿须知第 3 节
    check:
      body_font_east: ["宋体", "SimSun"]
      body_size_halfpt: 21      # 五号 = 10.5pt
      first_line_chars: 200
      line_rule: auto           # 单倍行距：只要求"不是固定值"
      heading_max_level: 3
      figref: true
    disable_rules: [FORMAT-004] # 不判固定行距（该刊允许单倍）
```

### 33.3 默认是 `none`，不是"某套通用标准"

字体/字号/行距**没有通用的正确答案**，只有"委托方要求"。没有标准时判任何一项都是瞎猜，
而误报会让人放弃整份清单（这是本项目从第一条原则起的取舍）。

所以 `none` 只保留 `FORMAT-006`（图表编号与正文引用一致）——它不依赖具体版式数值。
其余五项**不参与**，并在报告里写明：

> 未提供格式规格：本报告**不判断**字体、字号、首行缩进、行距、标题层级
> （这些没有通用的正确答案，必须由委托方的格式要求决定）。
> 「未报出版式问题」不等于版式合规。

部分规格（只规定字号、不管行距）同样会写一条「本规格未包含 X 的判定，本次未检查
（不是「已合规」）」。**没有"沉默通过"这条路**。

### 33.4 实测：同一份文档，三套标准三种结论

目标：`targets/uploads/网安1班…智能招聘平台个人信息安全与算法偏差风险处置报告.docx`
（111 个正文段），纯规则审（`--no-llm`）：

| 所选规格 | 格式问题 | 具体命中 |
|---|---|---|
| 不选（默认 `none`） | **0 条** | 只有 limitations：「未提供格式规格…不等于版式合规」 |
| 课程报告格式要求 v1 | **4 条** | 字号（110 段 11pt）、缩进（10 段）、行距（111 段）、标题层级（无标题样式） |
| 另一份期刊（11pt/非固定行距/不限缩进） | **3 条** | 字号只剩 1 段五号、行距只剩显式 18pt 那 1 段、标题层级；**缩进完全不判** |

第三条最能说明问题：同一份文档，换标准后**同一批段落从"违规"变成"合规"**。
这不是判定不稳，而是判定**跟着标准走**——正是它该有的样子。

### 33.5 几条实现边界

| 决定 | 理由 |
|---|---|
| 规格只提供数值，不新增/删除"查什么"的能力 | 否则 spec 就变成了第二套规则库 |
| `check` 里未知键名 → **报错** | 键名写错 = 配了不生效，比没配更糟 |
| `first_line_twips_min/max` 必须成对 | 只配下限会退化成"半条判定" |
| `line_rule: auto` 时，无 spacing 属性的段算过 | 继承默认多半就是单倍；误报的代价更高 |
| 只对 `.docx/.docm` 处理 | 纯文本/代码套格式规格只会往 limitations 塞噪音 |
| 未知规格 id → **报错不起任务** | 静默回退 = 用户以为按投稿须知判的，实际按别的判的 |
| 报告记 `source` | 半年后要能回答「这 22 磅是从哪份文件来的」 |

测试：`tests/test_format_spec.py` **26 项**（配置校验 9 / 规格驱动判定 5 /
`run_one` 集成 3 / HTTP 接口 5 / 前端契约 3 / 其余），锁住"换标准结论真的会变"。

**顺带修掉的一处**：`FrontendParityTests` 提取前端接口的正则不含 `-`，
`/api/format-specs` 被截成 `/api/format`，误报"前端调了但后端没路由"。

---

## 34. 全系统暴力测试：不选靶，只查契约（v0.7.6）

前几轮暴力测试都是**已知功能**定向打（docx 格式 / 目的 / 对照矩阵）。定向测能回答
"这个功能对不对"，回答不了"系统整体还站不站得住"。这一轮换了问题：
**把系统当黑盒，只查五条与具体规则无关的契约。**

| 契约 | 反例（一旦破坏就是事故） |
|---|---|
| 任意输入都给结论，或明确报错 | 喂坏文件 → traceback / 500 |
| 绝不静默降级 | 配置写错 → 报告"正常"但少跑一整类检查 |
| 报告自洽 | quote 不在原文里、severity 越界、覆盖率算术对不上 |
| 链完整 | 并发写台账 → 两条同号记录，事后才发现 |
| 结论可复现 | 同一输入两次跑出不同清单 |

### 34.1 六个面怎么压

| 组 | 项数 | 压什么 |
|---|---|---|
| A 输入模糊 | 22 | 空文件 / 单字符 / 只有 BOM / 只有零宽字符 / 夹 NUL / 纯标点 / 12 万字符单行与多段 / emoji+扩展 B 区 / 怪文件名 / 5 类不可读目标 |
| B 报告不变式 | 6 | 140 份历史报告逐条体检（字段、severity 域、quote 逐字溯源、覆盖率算术、裁定一致、完整性链） |
| C 配置对抗 | 28 | scenario / format_spec / agent manifest / rules / 版本文件，各种写坏的方式 |
| D API 模糊 | 42 | 坏 JSON、错类型、穿越、超限 body、跨站头、未知路由、20 并发、滥用后存活 |
| E 并发与确定性 | 5 | 6 路并发同目标 + 同输入三跑 |
| F 摄取对抗 | 12 | bomb.docx、空/炸弹 PDF、PNG、旧格式、zip 成员超限/穿越/嵌套、超长截断 |

全部在**临时沙箱**里跑（`outputs/logs/ledger/targets` 整体重定向到 `tempfile`），
所以随便压都不会污染真实台账——这条是前提，不是细节：**测试一旦会写真实数据，
人就不敢跑第二次，等于没有测试。**

### 34.2 一次挖出 6 个缺陷，全是"生产时无感、审计时才炸"

| # | 缺陷 | 为什么难发现 |
|---|---|---|
| F1 | 并发写台账 → 两条 `seq` 相同的记录，链断 | 两份报告本身都自洽，只有 `verify_ledger` 报 `序号不连续 @line2` |
| F2 | 坏 XML 的 docx 抛 `ParseError` 冒到进程顶 | 正常 docx 永远走不到；只有恶意/损坏文件才触发 |
| F3 | `rules/*.yaml` 顶层写成对象 → 0 条规则、零提示 | 报告完全正常，只是整类检查没跑 |
| F4 | `rules_version` 首字符是 `\ufeff` | 肉眼不可见，只在跨工具比较时失准 |
| F5 | `PUT/PATCH` → 501 + HTML | 前端平时只发 GET/POST，永远碰不到 |
| F6 | 解压炸弹 docx 被告知「内容为空」 | "读不出来"与"本来就没字"看起来一样 |

共同点：**都不是"算错了"，而是"没说"或"说错了"。** 这也解释了为什么定向测试抓不到——
它们测的是"规则判得对不对"，而这六个缺陷都发生在规则之外。

### 34.3 台账锁的取舍（F1 的修法）

```
读最后一行取 prev_hash  →  算 report_hash（含 prev）  →  追加一行
└────────────────── 必须在同一把锁里 ──────────────────┘
```

三个决定：

- **锁粒度只覆盖这三步**，不包住报告写盘（那部分按 `review_id` 互不相干，锁久了纯拖慢）；
- **陈旧锁自动回收**（mtime > 60s 视为进程被强杀后留下的），否则一次 `kill -9` 会把后续
  所有审查堵死 30 秒后报超时；
- **等待超时报错要给出处置**（提示"确认没有审查在跑可手工删锁"），不只抛 `TimeoutError`。

### 34.4 判定口径与复跑

```bash
py -3 tests/bruteforce_global.py              # 全量，约 7s，无网络、无 LLM
py -3 tests/bruteforce_global.py --only D,E   # 只跑 API 与并发
py -3 -m unittest tests.test_bruteforce_regressions -v
```

- **PASS** = 符合"给结论或明确报错，绝不静默降级"；
- **FAIL** = 崩溃 / 静默降级 / 契约被破坏（含链断、ID 冲突、结论漂移）；
- **OBS** = 已知边界或设计取舍（如"同一句两个事实点各报一条"，合理但前端看着像重复）。

退出码 `0/1` 可直接进 CI 或 pre-push。

### 34.5 本轮实测

逐组跑的过程中共报出 9 条 FAIL：6 条是系统真缺陷（上表），3 条是测试脚本自身的问题
（临时目录在 ROOT 外触发 `relative_to`、`e.read()` 在 413 后自己抛、用新契约判旧 schema 报告）。
修复后 **113 PASS / 0 FAIL / 2 OBS**，单轮 7.3s，连续 3 次结果完全一致。
全量回归 **476 项 OK**（原 466 + 新增 10 项回归锁）。
报告全文见 `暴力测试·全系统·20261027.md`，明细见 `tests/bruteforce_global_report.json`。

## 35. 事实库：裁判手里得有法典（v0.7.7）

第 34 轮把工程设施补齐之后，有一处失衡很难看：哈希链、台账、自学习、控制台、
四类日志、pre-commit 守门、500 项回归都建好了，**事实审的全部弹药只有 2 条断言**
（豆包 MAU≈1 亿、豆包 2023 年推出）。

于是出现了最不该出现的退化：面对任何不涉及豆包的文本，事实审要么哑火，
要么"偷偷"退回模型的参数记忆去判断真假。**一旦靠模型自己想，"独立裁判"就破产了**
——它和通用 agent 之间的差别只剩一条哈希链，而哈希链只能证明"记录没被改"，
证明不了"记录是对的"。

### 35.1 第一步：把法典填进去（2 条 → 7 条）

> 注（v0.7.8）：红队复核后已扩到 **9 条**（新增试用期上限、华为量级），并新增 `max`
> 类型与 `watchlist`，见 §37.1；本节保留当时的 7 条版本作为过程记录。

`knowledge/facts.json` 按原有 schema（`entity` / `attr` / `type` / `expected` /
`tolerance` / `note` / `exclude_ctx`）扩充，覆盖审查对象里反复出现的法条口径：

| 实体 | 属性 | 期望 | 口径写在 note 里 |
|---|---|---|---|
| 豆包 | 月活 | 1 亿（容差 ±2） | 2024 年 App MAU 量级；「豆包大模型/API」是另一条产品线，`exclude_ctx` 排除 |
| 豆包 | 推出 | 2023 年 | 由字节跳动推出 |
| 个人信息保护法 | 通过/施行 | 2021 年 | 2021-08-20 通过、2021-11-01 施行 |
| 数据安全法 | 通过/施行 | 2021 年 | 2021-06-10 通过、2021-09-01 施行 |
| 网络安全法 | 施行 | 2026 年 | 2025 年修正案的修改决定自 2026-01-01 施行；注意「2025 年修正」不是施行年 |
| 网络数据安全管理条例 | 施行 | 2025 年 | 国务院令第 790 号，2024-09-30 公布、2025-01-01 施行 |
| 生成式人工智能服务管理暂行办法 | 公布/施行 | 2023 年 | 2023-07-13 公布、2023-08-15 施行 |

`note` 不是"结论"而是"凭据"：它必须写清公布/通过/施行日期，让半年后的人能复查
"这条断言是从哪来的"。`tests/test_fact_library.py::test_note_states_the_source_caliber`
把这条锁住——只写"年份应为 2021"没有可核验来源，测试直接失败。

同时补了 `knowledge/kb_laws.md`（人读版口径说明），并升 `config/knowledge_version.txt`
到 `1.0.2`。**改 `knowledge/` 必须升版本**，否则历史报告会被判成"篡改"（红）；
升了版本，历史报告只是 stale（黄），这是刻意设计。

### 35.2 第二步：日期归属改成"同句制"

旧实现取「实体前 40 字窗口里的第一个日期」。语料里三条法规挤在同一段时，
这个窗口会偷隔壁条目的日期：`网络数据安全管理条例` 命中后，取到的是
`个人信息保护法` 的 2021，于是正确文本被判错、错误文本反被判对。

现在只认**同一句内、离实体最近的那个日期**（`_sent_start` / `_sent_end` 切句，
`nearest` 取最近）；同句没有日期就**不判**，并在 decision 里写明"跨句猜年份是误报源"。
宁漏不误——这条原则在事实审上比在别处更重要，因为 blocker 级误报会毁掉整份报告的可信度。

### 35.3 第三步：不许拿模型记忆当证据（硬守门）

只填库还不够：库没进 prompt，模型仍然会用自己的知识判真伪。三处改动：

1. **事实库进 prompt**：`render_facts()` 把 7 条断言渲染成 FACTS 区块，
   连同三条硬约束一起注入事实维度（其它维度不注入，省 token）：
   ① FACT-001 只能用于区块内的实体/属性，evidence 必须点明依据的条目；
   ② 区块外实体**不得凭记忆断言真伪**，只能按 FACT-002 报「缺可核验出处」；
   ③ 语境命中 `exclude_ctx` 时不比对。
2. **校验层兜底**：`validate(dim="fact", facts=...)` 发现某条 FACT-001 的 quote 里
   **一个事实库实体都没有**，就把它改判成 FACT-002（缺出处），evidence 前面加
   "事实库无对应条目，按「缺可核验出处」判定，不判事实真伪"，并在 `dropped` 里留痕。
   规则不是删掉，而是换成站得住的那一条：**"这个数字我核不了"是可交付的结论，
   "这个数字是假的"不是。** 严重度仍由规则库决定，模型无权改级。
3. **缓存键含事实库签名**：`_cache_key` 增加 `facts_sig`。否则改了 `facts.json`
   而语义缓存不失效，等于拿旧证据判新文本。

### 35.4 自报边界

`limitations` 新增一条（事实维度活跃且事实库非空时）：

> 事实审的证据仅限事实库内 7 条断言（涉及：豆包、个人信息保护法、数据安全法…）；
> 库外实体只判「缺可核验出处」，不判真伪——要判真伪请先补进 knowledge/facts.json。

不写这句，读者会把"没报事实错误"读成"事实全对"，那正是第 4 条设计原则要防的事。

### 35.5 验收：9 组突变，5 中 4 不误报

| 用例 | 期望 | 实测 |
|---|---|---|
| 条例施行写成 2026 | 报 | ✅ FACT-001 |
| 个保法写成 2022 | 报 | ✅ FACT-001 |
| 生成式办法写成 2024 | 报 | ✅ FACT-001 |
| 豆包推出写成 2022 | 报 | ✅ FACT-001 |
| 豆包 MAU 10 亿 | 报 | ✅ FACT-001 |
| 同段三法规**全对** | 不报 | ✅ 0 条 |
| 同段三法规**错 1 处** | 只报那 1 条 | ✅ 1 条（条例） |
| 豆包推出写对 2023 | 不报 | ✅ 0 条 |
| 实体与日期**跨句** | 不报 | ✅ 0 条 |

真实语料侧写：`targets/uploads/…智能招聘平台…报告.docx`（111 段）在扩充前
**匹配断言 0 条**（不含豆包，事实审等于没跑），扩充后**匹配 4 / 7 条**——
两者都不报错，但后者是"查过了没矛盾"，前者是"没查"。

**边界**：FACT-002/003/004 三个启发式仍是低置信线索（0.6~0.65，进"待确认"），
会有噪声（如比喻句「我们慢了 20%」被判"百分比未标口径"）。它们是**线索**不是结论，
不参与拦截；真要降噪，得按第 35.3 的思路给它们配确定性执行器，而不是调阈值。

---

## 36. 确定性算术补齐 logic 漏检 + 金标准测试集（v0.7.7）

### 36.1 四类"根本不该让模型猜"的自相矛盾

人工标注的 4 条漏检全是 logic 类，且全都能用形式规则判定。`tools/arith_check.py`
新增 4 个检查器（原有 6 个 → 10 个）：

| 检查器 | 报什么 | 护栏（防误报） | 规则 |
|---|---|---|---|
| `signed_dir` | 「同比下降 -3.2%」方向词 + 负号互相否定 | 方向词与负号**必须紧邻**（只允许空白）；「增长率为 -3.4%」「降幅收窄至 -3%」不算 | **LOGIC-009**（新增） |
| `share_sum` | 占比分项加总 ≠ 100%（含"分项 105 但合计写 100"） | 必须有「占比/份额/比例/比重」语境词 + 合计词；分项 ≥2 且排除变化量 | LOGIC-001 |
| `unit_convert` | 「1.2 亿元（即 1200 万元）」量级换算错 | 只认 `即/等于/相当于/合/折合/=` 的等价式；差 1 个数量级以上才报 | LOGIC-001 |
| `period_clash` | 同句同口径反向断言（同比增 12% + 同比降 5%） | 必须同口径（同比 vs 环比基期不同，不构成矛盾）+ 必须能提出**相同主语** | LOGIC-001 |

LOGIC-009 按既有规矩登记进 `tools/rule_exec.py`（`arith_check·方向词与负号同现`）——
**未登记的规则在报告里会被标成「仅提示」**，等于宣称一条不存在的覆盖面。
`config/rules_version.txt` 随之升到 `v1-2026-10-28-a3`。

18 例快速验证（12 正例 / 6 负例）全绿。其中一组值得记：`占比分别为 40%、30%、35%，
合计 100%` —— 分项和 105 与"合计 100"自洽（都是 100），`check_sums` 查不出来；
真正的问题是**占比之和必须是 100%**，所以 `share_sum` 独立成一条，而不是复用合计检查。

### 36.2 金标准集：`tests/golden/`（25 用例 / 17 条标注）

结构：

```
tests/golden/
├── cases/*.txt                 25 条（15 阳性 + 10 干净对照）
├── labels/<case>.labels.json   15 个（p05/p06 各含 2 条标注 → 共 17 条）
└── baseline.json               不许变差的底线
```

- **阳性** 15 条：4 条来自 `state/annotations.jsonl` 的真实漏检、其余是同类的
  自相矛盾（占比合计、单位换算、口径打架、事实年份错、MAU 量级错）。
- **干净对照** 10 条：每条都对着一个具体误报风险（负数金额、比率取负、跨口径比较、
  正确换算、`豆包大模型 API` 语境豁免…）。
- 标注用的是 `eval_review.py` 的 AND-of-OR 格式，`exclude_dimensions: ["style"]`
  避免风格维度干扰召回口径。

`tools/golden_eval.py` 是驱动：

```bash
py -3 tools/golden_eval.py                 # 跑一轮，并与 baseline 比较（退出码 1 = 变差）
py -3 tools/golden_eval.py --save-baseline # 有意改进后才存基线
py -3 tools/golden_eval.py --list          # 只看用例清单
```

**当前基线：25 用例 / 标注 17 命中 17 → 召回 100.0% / 误报 0**，单轮 2.8s，无 LLM。

### 36.3 三把锁，防止"假绿灯"

`tests/test_golden_suite.py`（9 项）锁的不是数字，而是**数字的可信度**：

1. **每条标注必须真能命中**——标注里的 match 正则一旦写错，它永远匹配不到任何
   issue，表面上"零误报零漏报"，实际是一组空断言。这条测试逐个用例比对
   `labels_hit == len(labels)`，把"看起来通过"变成"确实命中"。
2. **干净对照必须真的干净**——否则"误报 0"是没跑出来的。
3. **基线用例集必须与磁盘一致**——新增用例却忘了 `--save-baseline`，
   就会拿旧用例集比新用例集，数字永远好看。

这套锁是被自己咬出来的：我写的第一版"干净对照" `c09_sum_ok.txt` 是
「销售费用 360 万，管理费用 210 万，研发费用 480 万，三项加总 1080 万元」——
360+210+480=1050，`LOGIC-001` 立刻报了出来。**不是规则误报，是我的"干净样本"本身算错了。**
处理方式：把 c09 改成 1050（真正的干净对照），把错的那条升为阳性用例
`p15_sum_wrong_1080.txt` 并加标注（`相差 30 万`）。这比删掉它有价值得多。

### 36.4 自学习闭环：从"空转"到"知道自己该学什么"

补了确定性规则之后，`tests/test_learning.py` 里那条"负增长写法应当通过门禁"的
集成用例**必然失败**——`m_report_a/b` 里的 2 处命中现在全由 LOGIC-009 覆盖，
冗余门禁（`redundant_count`）正确地拒绝了这个候选。

这不是回归，这是 ② 生效的证据：**自学习不该去学已经确定性的东西**。
用例改成断言"被拒且理由点名 LOGIC-009"，另加一条合成语料的用例证明
"确定性规则管不到的新表述仍然能通过门禁"（否则门禁就成了"一律拒绝"）。

---

## 37. 红队复核：A/B/C 三类整改（v0.7.8）

第 35/36 轮补完"法典与证据"后，一份独立红队报告（`红队测试报告·法典与证据·20261028.md`）
拿 20 条自测用例复核了同一件事，结论分两半：**执行链路健康（应抓的 14 条全中），
证据库仍然单薄（20 条里只有"豆包"有 rule 级依据）**，并给了 A/B/C 三类整改清单。
这一轮按它的顺序做完，并把每一条都变成可复现的断言。

### 37.1 A 类：事实库补到 9 条，并新增「上限」类型

红队的两个证据缺口很有代表性：

| 用例 | 期望 | 整改前实际 | 整改后 |
|---|---|---|---|
| st03 华为 2024 出货量 5 亿台 | FACT-001（事实库依据） | `LOGIC-004` model major 0.85 | **FACT-001 rule blocker**，evidence 指到 Canalys 2025-01 的 4,600 万台 |
| st04 劳动合同法试用期 12 个月 | FACT-001（法规条款） | `LOGIC-004` model major 0.9 | **FACT-001 rule blocker**，evidence 指到第十九条 |

差别不在"报没报"，在**谁在报**：整改前是模型拿参数记忆判断（蒙对是运气），
整改后是事实库比对（换个人、换台机器、关掉模型，结论一样）。

补库时踩到两个设计问题，都值得记下来：

1. **上限不能复用「数值」类型。** `number` 是对称容差判断（`ratio > tol` 或
   `ratio < 1/tol` 都报），拿它判"试用期不得超过 6 个月"，会把合法的
   「试用期 3 个月」算成"偏差 0.5 倍"直接误报。所以 `type` 增加到第三类
   **`max`**：只在超限时报，`unit_hint` 必填（否则「试用期 3 人」会被当成 3 个月）。
2. **同一指标不同年份量级能差十倍。** 华为手机出货量 2019 年 2.4 亿台、
   2024 年 4,840 万台，都是真话。断言加了 `year_ctx: 2024`：**句内没写这个年份就不判**。
   宁漏不误——拿 2024 年的口径判 2019 年的数字，比漏报更糟。

还有一类实体**故意不写断言**：微信、iPhone、抖音、民法典。它们要么口径按季变
（微信月活含 WeChat 合并），要么单值会互相误伤（民法典诉讼时效 3 年 vs 最长保护期 20 年）。
硬写一个数字，等于给自己埋一个必然过期的误报源。它们进 `watchlist`，
报告 `limitations` 会写：「本次文本涉及事实库暂无断言的高频实体：iPhone——
其数字/日期**未做真伪判定**（不是已核验通过）」。

**"没查"和"查了没问题"必须能分辨**，这条在事实审上比在任何地方都重要。

### 37.2 B 类：同一现象不能一轻一重

红队 B1/B2 的描述是"同一问题两条规则一轻一重、用户只看到 minor"。查下去根因不是
"两条规则都错"，而是**归口没写**：

| 现象 | 轻的那条 | 重的那条 | 整改 |
|---|---|---|---|
| 销量翻倍→归因定价成功 | FACT-003 minor 0.6（fact 启发式） | LOGIC-003 major | fact 管"有没有前提"，logic 管"证据强度"；同时命中时 **LOGIC 主判**，fact 折叠 |
| 本季 12 亿 vs 上季 100 亿 | FACT-004 minor（口径未标） | LOGIC-001 major（量级矛盾） | 量级/加总归 LOGIC，fact 只管"口径未标注"这一线索 |

折叠不是删除：条目进 `suppressed[]`，带 `folded_into: "LOGIC-001"` 与
`fold_reason`，JSON 里查得到。**没有对手时（纯规则审）线索照常保留**——
不能为了去重把唯一的判断吞掉。

顺带修了 FACT-003 自己的级差：它的启发式原来固定 0.6，而 `make_issue` 会把
置信度 <0.7 的 major 自动降级——于是"这条规则写着 major，报出来是 minor"。
现在按**结构**分两档：因果连接词 + 成效/归因词同现（"翻倍…因此…成功"）→ 原级 major；
只有连接词（"因此下周补测"）→ 线索级 minor。分档依据是句子里有没有成效词，
不是调阈值——**结构判断可复现，分数判断不可复现**。

B3 是另一类：COMP-004（披露须标来源）把「本周接口联调完成 8/10」也报了。
对内进度既不是披露，也没有外部来源可标，这是范畴错误。现在命中内部语境
（本周/进度/里程碑/联调/内测…）**且**没有外部数据语境时豁免；内部周报里
引用"行业市场规模 120 亿元"照样要求标来源——豁免不能扩大成"有'内部'二字就免死"。

### 37.3 C 类：缺失型问题的锚点，与 OPEN-* 的可追溯性

C1 是这轮最"丢人"的一条：合同缺争议解决条款，`quote` 却锚在
「甲方：北京示例科技有限公司」上——**证据指向"有甲方"，跟结论毫无关系**。
`quote` 必须原文逐字是为了前端高亮与回查，但缺失型问题没有病句可引，
"逐字可回查"就退化成了"随便找一个真实句子"。

现在 `COMP-001`（主体缺失）锚开头，`COMP-002/003`（生效/争议）锚**文末最后一条**，
`location` 写「文末（最后一条之后，应在此处补该条款）」，evidence 里带
「缺失定位：文末「本合同自双方签字盖章之日起生效。」之后未见该要件」。
读者一眼能对上"本该在这一条后面再补一条"。

C2 的质疑是"OPEN-* 是不是模型即兴发挥"。查证结论：**有治理，但不够显眼**——
它带 `open_discovery: true`、`confidence_basis: "model"`、`rule_text`（写明未经规则库定义）、
单次上限 3 条、默认 minor（置信度 ≥0.85 才升 major）。问题是它混在 `issues[]` 里，
rule_id 长得像规则库条目，审计时分不清"规则判定"和"模型猜测"。

现在报告顶层单独给一份 `open_discoveries[]`（带 `source: "model"`），
`limitations` 自报「模型另报 N 条规则清单外发现，不参与规则覆盖率统计，须人工确认」。
前端汇总表原本就有「规则外」标签，与自学习晋升的 `AUTO-*` 「自动」标签区分。

### 37.4 测试成绩必须机器可复现

红队旧脚本只把原始 issues 写进 `run_result.json`，"14/14 召回"是**人肉数出来的**。
换个读者就会数出别的数——这类成绩不能进基线。

`run_selftest.py` 现在自带判定：`HIT / MISS / GAP / GAP_FILLED / CLEAN / FP /
HIT_DUP / MISS_SEV`，退出码 1 = 有 MISS/FP/HIT_DUP/MISS_SEV。
其中 `HIT_DUP` 专门抓"归口失效"：`expect_rules` 给的是等价集合（FACT-003 或 LOGIC-003），
**任一命中即达标，两条同时出现就是回退**。
`--rejudge` 可以用已有原始数据按新口径重算，不重跑模型、不产生新报告。

用例预期同步改了三处，都写清了理由：st03/st04 从 `expect_miss_gap`（预期缺口）
升为 `should_catch`——**事实库补上了，再漏就是回归**；st06/st07 加 `expect_rules`
+ `expect_severity: major`。

> 顺手纠正一个命名坑：旧脚本把**被审文档的裁定**叫 `verdict`，与"用例是否通过"
> 极易混淆（红队报告里 4 条 `verdict=fail` 其实是"文档被判 fail"，不是"用例失败"）。
> 现在分成 `doc_verdict` 与 `outcome` 两个字段。

### 37.5 本轮数字

| 项 | 数 |
|---|---|
| 红队 selftest（20 例） | **HIT 16 + CLEAN 4 = 20/20**，退出码 0 |
| 其中 st03/st04 | `FACT-001` / `rule` / blocker（整改前是 LOGIC-004 model） |
| 金标准 | 33 例 / 21 标注 / 召回 100% / 精确率 100% / 误报 0 |
| 事实库 | 9 条断言（豆包 2 + 法条 5 + 试用期上限 + 华为量级）+ watchlist 4 |
| 全量回归 | **519 项 OK / 225.6s** |
| docx 暴测 | 31 PASS / 0 FAIL（未退化） |
| ledger | 223 条记录，链完整，无篡改判定 |

**仍然薄的边界**：9 条断言覆盖的是"审查对象里最常被引用的那几部法 + 一个实体量级"，
不是"通用事实核查"。库外实体仍然只判"缺出处"、不判真伪；`watchlist` 里的四个
高频实体要想判真伪，得先补断言（而且要想清楚口径怎么随季度更新）。

---

## 38. 法条库：判法律问题得有原文（v0.7.9）

### 38.1 问题不在"会不会判"，在"依据是什么"

系统能报出「试用期 12 个月违法」，但这条结论的 evidence 一直是模型自己写的：
`《劳动合同法》第十九条规定试用期最长不得超过六个月`。这句话对不对？对。
**但它不是证据，是模型的复述。** 读者拿它去核对，核对的是模型的说法，
不是法律的说法。这跟"裁判引用了一本他自己写的法典"没有区别。

更麻烦的是它错的时候：模型把上限记成"一年"、把条号记成"第十七条"，
报告照样带着 `confidence: 0.9` 输出，没人看得出来。

### 38.2 为什么不是"接个搜索引擎"

最直觉的方案是审查时联网搜一下条文。没做，原因不是做不到，是**做了会毁掉可复现性**：

- 同一份合同，今天搜到的和半年后搜到的可能不是同一条（修订、页面失效、排序变化）；
- 报告是带 hash 链存证的，结论必须能重放。联网检索的结果无法重放；
- 搜索返回的是"某网页说"，不是"现行有效条文"。谁来判断这个网页权威？

所以走的是**离线入库 + 报告只引库内条文**：联网只发生在**入库那一刻**（`law_ingest`），
入库时记下 `source_url` / `retrieved_at` / `status` / `article_count`；
审查时读本地库，`knowledge_hash` 进报告哈希链——半年后能回答"当时引的是哪一版"。

### 38.3 四层，各管一件事

| 层 | 文件 | 管什么 | 不管什么 |
|---|---|---|---|
| 数据 | `knowledge/laws/*.md` + `index.json` | 条文原文、来源、效力状态 | 不做任何判断 |
| 索引 | `tools/law_index.py` | 切条、检索、按条号精确取 | 不判是否适用 |
| 导入 | `tools/law_ingest.py` | 切条校验、写 index、升版本 | 不"润色"原文 |
| 判定 | `tools/law_cite.py`（LAW-001） | 三类法定量的超限判定 | 不判例外情形 |

**为什么要按法律分文件，而不是灌成一锅**：中国没有一部叫"中华人民共和国法典"的东西。
它是《民法典》《劳动合同法》《个人信息保护法》等**单行法**，各自有独立的制定机关、
施行日、修正记录。混在一个文件里，"这条还有效吗"就永远说不清。

### 38.4 四条不可让步的口径

**① 条号必须连续，条数必须对上声明。**
公网页面会分页——实测钦州市钦南区检察院转载的《劳动合同法》只到第五十条，
看起来是一份完整全文，实际少了 48 条。这种库比没有库更危险：读者问"第 82 条怎么说"，
系统答"查无此条"，读者会理解成"法律没规定"。所以 `--article-count 98` 是硬门禁，
50 条会被直接拒绝入库。

**② 正文里引用的条号不是条头。**
「依照本法第四十七条规定处理」在段首出现，朴素正则会把它当成第四十七条的条头，
整部法从那里开始错位。切分器要求**条号必须等于期望的下一条**才认——这条规则同时
解决了"引用句误判"和"漏条后继续错位"两个问题。

**③ 检索到条文 ≠ 条文适用。**
这是最容易被神话的地方。「试用期 12 个月」命中第十九条只是"找到了相关条文"，
是否适用还要看：是不是非全日制用工（第七十条不得约定试用期）、是不是劳务派遣、
合同期限是几个月（上限分三档）。所以报告的 limitations 里有一句写死的话：
**命中条文 ≠ 条文适用（例外情形仍需人工判断）**，也不允许据此给"合法/违法"的总体结论。

**④ 没超限要记"已核验"，缺条文要说"没查"。**
- 「试用期 3 个月」不报，但写进 `key_facts`：`试用期约定 三个月，未超法定上限 6 个月（第十九条）`。
  "查了没问题"和"没查"必须分得开，否则 pass 会变成一句空话。
- 如果法条库缺了第二十条，检查器不是静默跳过，而是返回 `skipped`，
  报告写「法条库缺少「劳动合同法 第二十条 试用期工资」，该项未执行（不是没问题，是没查）」。

### 38.5 一条违法条款是怎么被引到第十九条的

输入：`本合同为三年期固定期限劳动合同，试用期为十二个月。`

1. `law_cite` 正则抽出「试用期 + 十二个月」→ `_to_num` 换算成 12 个月；
2. 12 > 6 → 超限成立（6 是绝对上限，不管合同期限是哪档，超过 6 一定违法）；
3. 依据**按条号直接取** `get_article("labor-contract", 19)`——不用检索去"找回"自己要的那条
   （实测用检索时，「竞业限制三年」会飘到第二十三条，而规定上限的是第二十四条）；
4. 产出 issue：`severity=major`、`tool=law_cite:试用期上限`、
   `citation={law: 劳动合同法, article: 第十九条, text: 原文, status: 现行有效}`；
5. 报告顶层 `law_library.cited` 追加一条，limitations 自报覆盖范围。

### 38.6 归口：不要让同一句话变成两条

| 场景 | 处理 | 理由 |
|---|---|---|
| 同句已有 `FACT-001`（blocker，事实库断言） | 法条原文**并入**它的 evidence，不新增一条 | 同一现象只留一条主判定；并列会让人以为是两个独立问题（金标准 p16 就因此多出 1 条误报） |
| 同句多类违法（既超试用期又压低工资） | 检查器返回 2 条，**报告层合成 1 条**，依据用 ①② 并列 | 读者要知道"这句话有两处问题"，但不需要看到两行几乎一样的清单 |
| 同句 `FACT-004`「百分比未标口径」 | 由 `LAW-001` 折叠进 `suppressed` | 工资比例是约定值，不是对外披露的统计数据 |
| 「试用期工资 85%」（合法） | 直接豁免 `FACT-004` 并留痕 | 要求约定工资比例标同比/环比是范畴错误 |

### 38.7 本轮数字

| 项 | 数 |
|---|---|
| 法条库 | 《劳动合同法》98 条，条号 1~98 连续，来源人社部官网 |
| 测试 | `tests/test_law_library.py` **16 项 OK** |
| 金标准 | **35 例 / 23 标注 / 召回 100% / 精确率 100% / 误报 0**（新增 p20 阳性 + c16 干净对照） |
| 违法合同用例 | 3 条 LAW-001，条号 19 / 20 / 24，全部带条文原文 |
| 全量回归 | **537 项 OK / 244.9s** |
| ledger | 223 条链完整，161 份 STALE（正常演进），**0 TAMPER** |

**仍然薄的边界**：

1. **只有一部法。** 库外法律（个保法、数据安全法、民法典合同编）现在**一条都引不到**，
   报告会照实说"库外不作引用"。要补就走 `law_ingest.py`，一条命令一部。
2. **只判三类法定量。** 试用期/竞业限制/试用期工资之外（经济补偿、加班费、年休假），
   目前仍靠模型与人工——不是忘了，是这三类有明确的"超限即违法"单侧判据，其余要立法推理。
3. **同义词表是手写的。** 它解决"我不想干了 → 解除劳动合同"，但覆盖有限；
   真正的召回率提升要靠术语库或语义检索，那是另一个工程。
4. **条文原文经 web_fetch 转录。** 已做条号连续性与二次源抽样比对，但**没有逐字对照官方 PDF**；
   `index.json` 的 `verify_note` 写明了这一点，引用前请对照 `source_url`。

---

## 39. 知识库自学习：让系统自己报"该补什么"（v0.8.0）

### 39.1 上一节的问题没有解决，只是换了个位置

法条库让"判法律问题"有了原文。但审查结束之后，系统仍然不知道自己缺什么：

- 审到库外实体（微信月活、某公司出货量）→ facts.json 没有对应断言 → **直接跳过**，
  报告里看不出"这里本来该判"；
- 模型按参数记忆下一个判断（`tool=llm_api`）→ **裸判**，库里没有任何东西支撑它；
- LAW-001 因为库里缺条文没跑（经济补偿、加班费）→ 只在 limitations 里留一句话。

三种情形都不留工单。于是"该往 facts.json 补什么"永远靠人回忆，
事实库停在手灌的那 9 条。**规则会自己提案（`rule_miner`），事实不会。**

### 39.2 做法：四类缺口 + 一个只进 state/ 的清单

`tools/fact_gap.py` 在每次审查末尾把上面三种情形抽成工单，追加到 `state/gap_log.jsonl`：

| kind | 触发 | 缺什么 |
|---|---|---|
| `model_judgment`（P1） | 模型在 fact 维度下了判断且不是 FACT-001 | facts.json 断言 或 laws/ 条文 |
| `law_missing`（P2） | LAW-001 因库里缺条文没执行 | `knowledge/laws/` 对应条文 |
| `unsourced_claim`（P3） | FACT-002/003：句内有可核验主张但无出处 | 可引用的断言（判真伪）或出处 |
| `watchlist_unasserted`（P4） | 点名了 watchlist 实体（刻意无断言） | 带口径与复查日的断言 |

三件"必须写死"的事：

1. **每行都有 `usable_as_evidence: false`**。缺口里的 `quote` 来自被审文档，
   是"待审主张"不是事实——文档里谁都能写"华为出货 5 亿台"，它绝不能变成事实库口径。
2. **只增不改**。重复出现追加 `{"op":"repeat"}`（旁证），不覆盖首次时间；
   补齐后追加 `{"op":"resolve"}`，不删行——删了就不知道它曾存在过。
3. **不污染真实清单**。缺口日志默认落 `state/`，但**跟着产物目录走**：
   测试把 `OUTPUTS` 重定向到临时目录时，缺口也写进临时目录。否则跑一次全量回归，
   几十份测试语料就被当成"该补的缺口"灌进真实工单，清单立刻没人读。

### 39.3 补齐：候选隔离 + 门禁 + 唯一入口

`tools/fact_promote.py` 是**唯一**会写 `knowledge/facts.json` 的入口
（`fact_import.py` 现在也走它的快照/升版本/记账，不再自己写文件）。

```
缺口 → fact_gap draft（草稿：expected 与 source_url 都空着，entity 是正则猜的）
     → confirm-entity（改对实体名并留痕：谁、从什么改成什么）
     → 人工从权威源填值（数值与被审文档重合时还要填 source_quote）
     → gate（门禁；语义见 §40.2，本节表格是初版）
     → promote --confirm-manual（快照 → 升版本 → 记账 → 缺口 resolve）
```

| 门禁 | 门槛 | 为什么 |
|---|---|---|
| `schema` | entity/attr/期望值/note/review_due 齐全 | 缺 note 半年后没人知道口径 |
| `source_verifiable` | http(s) 或「《法名》第 X 条」；**不能指向 targets/** | 被审对象不能当证据 |
| `type_correct` | `max` 必须有 `unit_hint`；`number` 必须有 `tolerance > 1` | 用对称容差判上限，会把合法的"试用期 3 个月"算成偏差 |
| `no_conflict` | 同实体同属性同年度不许两个值 | 冲突要人裁定，不能静默覆盖 |
| `entity_confirmed` | 实体经人工确认 | 草稿里的实体是正则猜的，猜错则冲突检查全按错实体走 |
| `value_from_target` | 数值不来自被审文档 | `source_url` 只证明"有链接"，证明不了数值出处（§40.1 #3） |
| `off_target` | **增量 = 0** | 候选**新引入**的误报数；存量误报单独记 `off_target_baseline`（§40.1 #1） |
| `on_target_hits` / `measurable` | 不能是"没测过" | 语料压不到 → `unmeasured`；压到但零正贡献 → `weak_signal`；两者都需人工放行 |

`--auto` 被明确拒绝（退出码 2）：**fact 维度没有自动通道**。存错一条事实会污染
未来所有判断，且比漏检隐蔽十倍。

### 39.4 实测

| 项 | 数 |
|---|---|
| 新增文件 | `tools/fact_gap.py`（缺口识别/清单/起草）、`tools/fact_promote.py`（门禁/晋升/回滚/审计） |
| 改造 | `review.py`（末端记账 + 报告 `fact_gaps` + MD「知识库缺口」小节）、`fact_import.py`（改走治理路径） |
| 测试 | `tests/test_fact_learning.py` **31 项 OK** |
| 缺口命中 | 一段含「据行业报告显示，微信月活 14 亿」的文本 → 记录 3 条（1 条 unsourced_claim + 2 条 watchlist） |
| 门禁 | 会误伤干净语料的候选（毛利率写 90%）→ `off_target=3`，拒绝；来源写 `targets/...` → 拒绝；`type=max` 缺 `unit_hint` → 拒绝；与「豆包月活 1 亿」冲突 → 拒绝 |
| 闭环 | 晋升后断言进 facts.json（带 source/retrieved_at/review_due/promoted_by）、版本 1.1.0→1.1.1、快照留存、缺口自动 resolve；回滚后断言删净、版本再升 |
| 全量回归 | **568 项 OK / 235.2s**（该轮；§40 之后为 594） |
| 金标准 | 35 例 / 23 标注 / 召回 100% / 精确率 100% / 误报 0 |
| 事实库 | 审查前后 sha256 不变（`test_review_never_touches_fact_library`） |

### 39.5 边界：它是工单系统，不是自动事实核查

1. **shadow 只在 35 例金标准语料上跑。** 语料覆盖不到的新断言记 `unmeasured`，
   **拦截**而非放行，要人工 `--allow-unmeasured` 签字（§40.2 ④）。别把"没负贡献"读成"已验证"。
2. **缺口里存的是被审文本片段。** 涉密材料用 `--no-gap-log`（报告写 `disabled: true`），
   代价是下次仍然不知道该补什么。
3. **不自动取证。** 抓网页、多源一致性、自动填值都不做——`docs/知识库自学习.md`
   把这部分列为第二步之后的增强，理由同上节：联网取证的结论不可重放。
4. **法条缺口不进 `fact_promote`。** 那是 `law_ingest.py` 的活（要切条、要条数门禁）；
   `fact_gap draft` 对 `law_missing` 直接报错，避免用"一条断言"冒充"一部法"。
5. **`watchlist` 四实体仍只判"没判"**。要判真伪得先想清楚口径怎么随季度更新，
   否则每补一条就多一个过期误报源。

---

## 40. 门禁复核：把"通过"从自我安慰变成可核对的结论（v0.8.1）

§39 建完闭环后，外部复核给了一句很准的判词：

> 外壳的工程纪律（快照 / 版本 / 审计 / 人工确认）都是对的，先别急着加功能——
> 先把门禁这颗心脏修对。否则每晋升一条断言，你都没法回答"它到底会不会把对的判错"。

复核点了七处，逐条核对后六处属实、一处（实体猜测）确实没被任何地方检查。下面只写结论与修法。

### 40.1 四处硬伤

| # | 原状 | 后果 |
|---|---|---|
| 1 | `off_target = sum(after)` | **只数"加候选之后"的误报，没减"加之前"就有的**。语料里只要有一条存量误报，任何候选都过不了 `off_target=0`——门禁从"守门"变成"一律拒绝" |
| 2 | `shadow(..., use_llm=False)` | 纯规则模式下 FACT-002/003/004、LOGIC-004 根本不存在，在那里算"误报零容忍"测的是另一个系统 |
| 3 | `_source_ok` 只查 URL 里有没有 `targets/` | 草稿的 `evidence_quote` 就是被审文档原句：把「华为出货 5 亿」抄进 `expected`、再贴个真 URL，照样放行。红线防住了"拿文档当链接"，没防住"拿文档当数字" |
| 4 | 门禁失败一律记 `rejected`；`weak_signal` 只警告；`entity_confirmed:false` 从不检查 | 人审队列里"待补草稿"和"真被否的断言"搅成一锅；语料零覆盖的断言照样晋升；正则猜错的实体直接当权威字段 |

顺带两处设计矛盾：`replace_all` 能一次删光整库（与"只增不改不删"自相矛盾，且无任何东西区分"迁移"与"传错文件"）；
`bulk_append` 完全绕过门禁，能把两个互相矛盾的断言同时写进库——之后同一句话有两个期望值，结论取决于遍历顺序。

### 40.2 修法

**① 增量 + 逐句对齐。** `off_target` 改成 `|after_off − before_off|`，指纹是 `(语料名, rule_id + 归一化 quote)`：

```text
before: {c01: FACT-001::存量误报}        存量 1 条
after : {c01: FACT-001::存量误报,
         p02: FACT-001::把对的判错}      增量 1 条  ← 只有这条算候选的账
```

存量误报单独进 `off_target_baseline`，不拦候选，但写进 warnings——**金标准集自己的债不能靠拦住候选来消化**。

**② 模式跟随生产。** `_shadow_llm(None)` 读 `llm_client.load_config()`（本地端点免 key），
`metrics.mode` 记 `rule+llm` / `rule-only`。`--rule-only` 保留给排查，但只要降级就一定留标记，
读报告的人不会把它当成生产结论。

**③ 数值也要防。** 新增 `value_from_target`：期望值与被审文档 `evidence_quote` 里的数字重合时，
必须给独立的 `source_quote`，其中要含这个数，且**不能是被审文档那句**（比对去掉空白与标点，
防止"删个逗号再贴回来"）。`5,000 万台` 与 `0.5 亿` 归一成同一个数比。

判据刻意不是"数值相同就拒"——来源与文档恰好都对的情形是存在的，那会误伤；要的是**来源自己的原话**。

**④ 四档结论。**

| verdict | 含义 | 处置 |
|---|---|---|
| `pass` | 全部通过 | 可晋升 |
| `incomplete` | 草稿没填完（缺字段 / 缺来源标题 / 实体未确认） | 补完可重提，**不记"被否决"** |
| `rejected` | 实质否决（冲突 / 把对的判错 / 数值来自被审文档 / 来源指向被审文档） | 别再提同样的东西 |
| `unverified` | 预演跑了但**测不出来**（语料没提该实体，或零正贡献） | 必须 `--allow-unmeasured`，放行写进 `metrics.override` |

`shadow` 跑不起来也从"警告"改成 `shadow_failed` 否决：没有负贡献证据就不能算通过（fail-closed）。

**⑤ 语料预过滤。** FACT-001 的触发前提是正文出现 `entity`，所以不含该实体的语料不可能产生新误报——
跳过是等价变换，不是抽样。但跳过多少份必须写进 `corpus_skipped`，否则"35 份语料"会被读成"35 份都验过"。

### 40.3 实测

| 项 | 数 |
|---|---|
| 新增测试 | `tests/test_fact_gate.py` **27 项**（增量算账 / 指纹对齐 / 模式跟随生产 / 崩溃 fail-closed / 数值三条拒绝路径 / 四档结论 / replace 守门 / 实体确认留痕） |
| 两模块合计 | **58 项 OK / 1.7s**；全量 **595 项 OK / 245.2s** |
| 真实 LLM 预演（错误断言「毛利率 90%」） | 35 份语料压到 3 份，`off_target` 增量 **3**、`off_target_baseline` 0、**28s** |
| 真实 LLM 预演（合理断言「毛利率 26% ±2 倍」） | 同样 3 份，`off_target` 增量 **0**、**9s**，仅 `weak_signal` 拦（语料无该属性阳性标注） |
| 旧实现对比 | 两者都会被算成同一个绝对值（3），**无法区分**对错候选 |

### 40.4 仍然的边界（别读成"已经安全"）

1. **批量导入不跑预演。** `bulk_append` 只做冲突检查，审计行标 `metrics.shadow="skipped-bulk"`。
   一条候选要跑两轮全语料，导入 20 条就是 40 轮。想逐条验，走 `draft → gate → promote`。
2. **新实体必然 `unverified`。** 金标准集里没有它的标注，`on_target` 只能是 0。
   这不是缺陷，是**语料的债**：要让断言可验，得同时补一条该实体的阳性用例（`cases/` + `labels/`）。
3. **LLM 非确定性不影响这道门的判据。** `off_target` 只数 `FACT-001`，而 FACT-001 由
   `retrieve + regex` 确定性判定，模型说什么都不改变它。模式跟随生产是为了让"预演"与"真审"
   跑同一套链路（覆盖度、折叠、归口都会影响最终 issue 集），不是为了给确定性判定加噪声。
4. **预过滤是推理，不是证明。** 它成立的依据是 `review.fact_review` 里 `if entity not in text: continue`。
   哪天那条前置条件改了，`_exercised_by` 必须跟着改，否则会静默漏测。
5. **`source_quote` 只能防"照抄"，防不了"编来源"。** 它保证你手里有来源原话，不保证那页真写着这句话——
   后者要靠人打开链接核对，`source_url` 的作用就是让人能核。

---

## 41. 回测老库：9 条断言里 3 条过不了新门禁（v0.8.2）

§40 把门禁修对了，但库里那 9 条断言是**门禁之前**写进去的，没人用新尺子量过。
"谁能保证这些断言可验"这个问题，只有拿同一把尺子回测一次才答得出来。

回测出来两件事：**3 条断言确实过不了**（1 条永远验不了，2 条归因不到自己），
以及**两个真 bug**——其中一个会让门禁误杀好断言。后者比结论本身重要。

### 41.1 为什么不能直接拿 `fact_promote shadow` 回测

`shadow(候选)` 的算法是 `库 + 候选  vs  库`，增量归**候选**。
拿现库里的断言当候选再跑一遍，等号两边都是现库——**增量恒为 0，什么都测不出来**。

回测必须用**摘除法**：

```text
回测(已入库断言)  =  库            vs  库 − 本条
归因              =  只有"摘掉它才消失"的误报，才算这条断言的账
```

判定方向也不能反：`before` 是"没有本条"，`after` 是"现库"。
反过来算出来的"增量"是个负数含义，没人读得对。

### 41.2 结果

`py -3 tools/fact_audit.py`（只读，不改库不写 state）：

| # | 断言 | 命中语料 | 正贡献 | 增量误报 | 结论 |
|---|---|---|---|---|---|
| 1 | 豆包·月活 1 亿 | 4/35 | 1 | 0 | `kept` |
| 2 | 豆包·推出 2023 年 | 4/35 | 1 | 0 | `kept` |
| 3 | 个人信息保护法 2021 年 | 1/35 | 0 | 0 | `shadowed` |
| 4 | 数据安全法 2021 年 | **0/35** | 0 | 0 | `unmeasured` |
| 5 | 网络安全法·施行 2026 年 | 1/35 | 0 | 0 | `shadowed` |
| 6 | 网络数据安全管理条例·施行 2025 年 | 1/35 | 1 | 0 | `kept` |
| 7 | 生成式人工智能暂行办法 2023 年 | 1/35 | 1 | 0 | `kept` |
| 8 | 劳动合同法·试用期上限 6 个月 | 2/35 | 1 | 0 | `kept` |
| 9 | 华为·出货量 5,000 万台 | 3/35 | 1 | 0 | `kept` |

**6 保留 / 2 被覆盖 / 1 未测到 / 0 有害。** 没有一条在把对的判错——这是好消息，
且这次是**有证据**的好消息（摘掉后误报数不变，说明它们在干净语料上不开火）。

`rule-only` 与 `rule+llm` 两轮结论完全一致（3.7s vs 1m51s）：说明这 9 条的作用不依赖模型。

### 41.3 回测跑出来的 bug ①：标注 id 跨语料重名，好断言被算成零贡献

第一轮结果里「豆包·月活」显示**正贡献 0**。但它明明找回了 `p11` 的 MAU 错误（标注意为 `L1`）。
查下去是 `_shadow_pass` 的 `on` 集合：

```python
on.update(matched)        # matched 是 ["L1","L2",...]，纯标注 id
```

而标注 id **只在单份语料内唯一**：`p11_fact_doubao_mau` 和 `p12_fact_doubao_year` 的标注都叫 `L1`。
于是 `on` 把两份语料的 `L1` 合并成一个——`before` 轮里 `p12` 的 `L1` 已经在了，
`after` 轮 `p11` 的 `L1` 加进去看不出任何变化，**正贡献被判成 0**。

后果不是"报错了"，而是：`gate` 看到 `on_target_hits == 0` → 判 `weak_signal` →
`unverified` → **一条好断言必须人工放行才能进库**。这比"放过坏断言"更难发现，
因为它的表现是"系统太保守"，人只会顺手点放行，没人去查为什么。

修法：`on` 的 key 改成 `(语料名, 标注 id)`，落盘时拍成 `语料:标注`。

```text
旧：{"L1"}                          → p11 的贡献被 p12 顶掉
新：{("p11.txt","L1"), ("p12.txt","L1")}  → 两份各算各的
```

### 41.4 回测跑出来的 bug ②：模型能自己写 FACT-001，混进事实库的账

`c08_doubao_api_ok` 是干净对照（"豆包大模型/API"属另一口径，应 0 issue）。
`rule-only` 下它确实 0 条；`rule+llm` 那轮却冒出一条 **`rule_id=FACT-001`、`tool=llm_api`** 的 issue。

连跑 3 次都复现不出来（每次都是 FACT-002 + COMP-004）——它是**偶发**的。
根因在 `review.validate`：它只把"quote 里没有事实库实体"的 FACT-001 降级成 FACT-002，
quote 里只要有实体（"豆包""华为"）就**原样保留 rule_id**。于是模型裸判混进了事实库的账本。

两处后果，都修了：

| 位置 | 原状 | 后果 | 修法 |
|---|---|---|---|
| `_shadow_pass` | 只按 `rule_id=="FACT-001"` 统计 | **LLM 非确定性直接变成"候选把对的判错"的证据**——同一份语料这次报、下次不报，就会凭空多出一条 `off_target`，误杀好候选 | 只认 `tool` 不以 `llm` 开头的 FACT-001（事实库判的） |
| `fact_gap` | `rid == "FACT-001"` 一律跳过 | 模型裸判的 FACT-001 **不进缺口清单**——系统明明在没有依据时开了口，账上查不到 | 加 `and "llm" not in tool` |

§40.4 第 3 条原本写着"LLM 非确定性不影响这道门的判据，因为 FACT-001 是确定性的"。
**这句话被这次实测推翻了**：FACT-001 确实由 `retrieve + regex` 确定性判定，
但 `rule_id` 字段不是排他的，模型也能写。判据不能只信 `rule_id`，还得看 `tool`。

改完之后 `豆包·推出` 那条"存量误报 1"消失（就是这条偶发 FACT-001），两轮结论重新对齐。

### 41.5 剩下 3 条过不了的，怎么办

**`shadowed` 2 条（个人信息保护法 / 网络安全法）——不是缺陷，是标注粒度不够。**
`p13_fact_laws_wrong` 里三段法规同段出现，但整份语料只有 **1 条标注**（网络数据管理条例的日期）。
三条断言的错误各自都该有自己的标注，现在只有一条，于是谁先摘掉谁是"零贡献"。
要归因到单条断言，得给 `p13` 补两条标注。

**`unmeasured` 1 条（数据安全法）——语料的债。**
35 份语料里 **0 份**提到"数据安全法"，这条断言永远验不了。
补一条含该实体的用例（`cases/` + `labels/`）它才有意义；在那之前，
这条断言的状态只能写"未验"，不能写"已验通过"。

### 41.6 这套回测回答不了什么

1. **摘除法只能回答"会不会把对的判错"，回答不了"值不值得单独留"。**
   多条断言同句共命中时，单条贡献必然被低估（`shadowed`）。想算单条价值得换方法（如逐条标注）。
2. **结论是"当前语料下的结论"。** 语料增删后必须重跑——`off_target_baseline` 会变，
   `kept` 也可能变成 `harmful`。
3. **只覆盖 13/35 份语料。** 其余 22 份根本没提这 9 个实体（`_exercised_by` 预过滤），
   跳过是等价变换，但"35 份语料"不能读成"35 份都验过"。
4. **不测"断言写错了但语料也错了"的情形。** 语料里若有一句错的和断言期望一致，
   它会被记成 `kept`——回测只能测"与语料的一致性"，不能替代人核来源。

