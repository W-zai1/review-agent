# 审查 Agent 框架定义 v0.2（Demo 版）

> 定位：个人日常 / Demo 级别。只读、独立裁判、可举证、可迭代。
> 不追求工程完备，追求「跑通一次完整审查闭环」。



***

## 1. 定位与边界



| 维度      | 是什么                             | 不是什么                |
| ------- | ------------------------------- | ------------------- |
| 身份      | 独立审查裁判，与生成模型解耦                  | 不是生成器、不是润色器         |
| 动作      | 只读、标记问题、引用证据、出报告                | 不修改原文件、不直接改代码 / 文案  |
| 决策      | 给裁定 + 建议，人做最终拍板                 | 不自动提交整改、不自动回滚       |
| 范围（MVP） | 文本类对象：LLM 回答、提示词、代码片段、合同 / 政策文本 | 图片 OCR、视频、音视频理解列为扩展 |

一句话：**「我不替你写，我只替你查；我给出每一条异议的出处和规则依据。」**



***

## 2. 五条核心原则（你原有 5 条，细化为可执行口径）



1. **独立裁判**：不假设上游生成模型是对的，所有结论必须独立复算 / 独立检索。

2. **规则显示化**：每条异议必须挂一个 `rule_id`，禁止凭「我觉得不对」下结论。

3. **最小权限只读**：审查全程不写回 targets/，所有产物只落到 `outputs/`。

4. **可追溯举证**：每条异议 = 原文片段 + 规则条目 + 来源 + 工具 + 置信度。

5. **多视角对抗**：事实 / 安全 / 合规 / 风格四个子视角独立出报告，再交叉复核，避免单视角盲区。



***

## 3. 分层架构



```
┌─────────────────────────────────────────────┐
│ 输入层  targets/ + rules/ + context(知识库)  │
├─────────────────────────────────────────────┤
│ 调度层  解析对象类型 → 匹配规则集 → 分维度派发 │
├─────────────────────────────────────────────┤
│ 审查层  ┌────┐ ┌────┐ ┌─────┐ ┌──────┐    │
│        │事实│ │安全│ │合规 │ │风格/ │    │
│        │审  │ │审  │ │审   │ │代码审│    │
│        └─┬──┘ └─┬──┘ └─┬───┘ └──┬───┘    │
│          └──────┴──────┴────────┘           │
│                   ↓ 交叉复核                  │
├─────────────────────────────────────────────┤
│ 证据层  quote + rule_id + source + confidence│
├─────────────────────────────────────────────┤
│ 输出层  outputs/<review_id>.json + .md       │
└─────────────────────────────────────────────┘
```



***

## 4. 标准审查 SOP（六步闭环）



| 步骤      | 动作                                          | 产出                  |
| ------- | ------------------------------------------- | ------------------- |
| S1 接收解析 | 识别待审对象类型（text/code/prompt/contract），加载对应规则集 | `target_meta`       |
| S2 规则匹配 | 从 `rules/` 拉取适用规则条目，列出本次要查的 checklist       | `active_rules[]`    |
| S3 分维审查 | 每个子审查器独立跑，产出各自的 issue 列表                    | `issues_by_dim{}`   |
| S4 证据锚定 | 每条 issue 绑定原文位置（行号 / 句段）、rule\_id、工具、置信度    | 带证据的 issues         |
| S5 交叉复核 | 安全审看事实审结论是否越权；合规审看风格审是否漏红线；冲突进入 disputes    | `disputes[]`        |
| S6 裁定汇总 | 综合定级 pass /warn/fail，生成结构化报告                | `outputs/<id>.json` |



***

## 5. 规则库设计（Demo 用 YAML 即可）

每条规则是一个独立 YAML 条目，禁止把规则散落在 prompt 里。



```
# rules/fact.yaml 示例
- rule_id: FACT-001
  dimension: fact
  desc: 涉及具体数字、日期、人名、机构名的陈述，必须能在知识库/可检索来源中复现
  method: retrieve_then_compare
  severity: blocker        # blocker / major / minor / nit
  source: 审查内部事实库 v1
  auto_fix: false
```



```
# rules/safety.yaml 示例
- rule_id: SAFE-002
  dimension: safety
  desc: 输出中不得包含可被直接利用的攻击指令、违法操作步骤、敏感政治人物负面暗示
  method: keyword_plus_semantic
  severity: blocker
  source: 内部安全红线清单 v1
```



```
# rules/style.yaml 示例
- rule_id: STYLE-003
  dimension: style
  desc: 正式文书中不得出现 emoji、口语化语气词、第一人称情绪化表达
  method: regex + llm_judge
  severity: minor
  source: 写作规范 v1
```

> Demo 阶段规则库 10~20 条即可覆盖常见场景，不要贪多。



***

## 6. 单条异议（Issue）的强制字段

每条异议必须长这样，缺字段 = 审查不合格：



```
{
  "issue_id": "ISS-0001",
  "dimension": "fact",
  "severity": "blocker",
  "quote": "原文片段（必须逐字摘录，不得改写）",
  "location": "第 3 段第 2 句 / 第 42 行",
  "rule_id": "FACT-001",
  "rule_text": "规则原文",
  "evidence": "知识库第X条 / 检索到的来源URL / linter报错原文",
  "tool": "retrieve / regex / ruff / ocr",
  "confidence": 0.9,
  "suggestion": "整改建议（怎么改，改成什么方向）"
}
```



* `confidence < 0.7` 的 issue 自动进入「待人工确认」，不直接判错。

* `quote` 必须逐字，禁止 paraphrase—— 否则下游无法回查。



***

## 7. 裁定输出 Schema（下游系统直接消费）



```
{
  "review_id": "R-20261022-001",
  "target_type": "llm_response",

  "integrity": {
    "target_hash":      "<sha256 of target file raw bytes>",
    "ruleset_hash":     "<sha256 of all active rules used this run>",
    "knowledge_hash":   "<sha256 of knowledge/ snapshot>",
    "report_hash":      "<sha256 of this report after finalized, self-filled back>",
    "logs_hash":        "<sha256 of logs/<review_id>/ directory bundle>",
    "prev_report_hash": "<last report_hash from ledger, forms chain>"
  },

  "verdict": "warn",
  "verdict_scale": ["pass", "warn", "fail"],

  "core_conclusion": "一句话总结：整体方向正确，但存在 1 条事实硬伤和 2 条风格问题，需整改后使用。",

  "key_facts": [
    "核心事实 1（已核验）",
    "核心事实 2（已核验）"
  ],

  "issues": [ /* 第 6 节的 issue 对象数组 */ ],

  "disputes": [
    {
      "between": ["fact-reviewer", "safety-reviewer"],
      "point": "事实审认为该陈述可公开检索，安全审认为涉及未公开个人信息",
      "resolution_hint": "交人工裁定"
    }
  ],

  "to_confirm":  [ /* confidence < 0.7 的 issue */ ],
  "to_fix":      [ /* blocker + major 的 issue，按严重度排序 */ ],

  "stats": {
    "blocker": 1, "major": 2, "minor": 3, "nit": 0
  },

  "reviewer": "independent-review-agent",
  "model_version": "v0.2-demo",
  "timestamp": "2026-10-22T15:30:00+08:00"
}
```

同时输出一份同内容的 `.md` 人读版，给你自己看。



***

## 8. 四个子审查器的分工



| 子审查器     | 查什么                                              | Demo 用什么手段                |
| -------- | ------------------------------------------------ | ------------------------- |
| 事实审      | 数字 / 日期 / 人名 / 机构 / 引用是否与事实库一致，有没有 hallucination | 语义检索事实库 + 数字正则抽取后比对       |
| 安全审      | 敏感内容、prompt 注入、越权操作、违法指引、个人隐私                    | 关键词表 + LLM 二次判定           |
| 合规审      | 对照政策文档、合同条款、披露规范                                 | 规则条目逐条 checklist          |
| 风格 / 代码审 | 写作规范、语气、格式；代码 lint、常见安全坑                         | 正则 + `ruff`/`eslint`（代码类） |

> Demo 阶段不用真起四个独立 Agent，用
>
> **一个 Agent 内分四个函数 / 四段 prompt**
>
>  顺序跑即可；等真要做多 Agent 并行再拆。



***

## 9. 工具清单（MVP 够用）



| 工具            | 用途           | Demo 实现                                     |
| ------------- | ------------ | ------------------------------------------- |
| `retrieve`    | 拉法规、事实库、历史基线 | 对 `knowledge/` 文件夹做 embedding 检索，或直接 `grep` |
| `static_scan` | 代码 lint      | 调 `ruff`（py）/ `eslint`（js）                  |
| `regex_check` | 数字、日期、敏感词抽取  | Python 正则脚本                                 |
| `ocr`（扩展）     | 图片里的文字       | Demo 先不做，留接口                                |
| `diff`        | 与历史版本对比      | `git diff` 或简单文本 diff                       |



***

## 10. 迭代对抗机制（Demo 版）



```
Round 1: 四维独立审查 → issues_v1
Round 2: 交叉挑刺
   - 安全审 挑 事实审 的结论有没有安全盲区
   - 合规审 挑 风格审 有没有漏红线
   - 互相给对方的 issue 标 confidence 修正
Round 3: 汇总，confidence<0.7 进 to_confirm
```

Demo 里做两轮就够，第三轮可以人工看一眼结束。



***

## 11. 完整性与防篡改（哈希链 + Ledger）

审查 Agent 作为独立裁判，输入和报告都必须可证明「没被改过」。Demo 级别用 **SHA-256 + append-only 日志**，不上区块链。

### 11.1 哪些东西要算 hash



| 对象           | hash 字段            | 计算时机                                |
| ------------ | ------------------ | ----------------------------------- |
| 待审 target 文件 | `target_hash`      | S1 接收时，对原始字节算                       |
| 本次生效的规则集     | `ruleset_hash`     | S2 规则匹配后，把所有 active rule 序列化后算      |
| 事实库快照        | `knowledge_hash`   | S3 开始前，对 `knowledge/` 目录算一次         |
| 本份报告自己       | `report_hash`      | S6 报告写完后，去掉自身 `report_hash` 字段再算，回填 |
| 上一份报告        | `prev_report_hash` | 从 ledger 末尾读，形成链                    |

### 11.2 Ledger（append-only 审计日志）

`ledger/chain.log` 每行一条 JSON，**只追加不修改**：



```
{"seq":1,"review_id":"R-20261022-001","target_hash":"abc...","report_hash":"def...","prev_hash":"0000...","ts":"2026-10-22T15:30:00+08:00"}
{"seq":2,"review_id":"R-20261022-002","target_hash":"ghi...","report_hash":"jkl...","prev_hash":"def...","ts":"2026-10-22T16:00:00+08:00"}
```

任何一行被改，下一行的 `prev_hash` 就对不上，链断裂 → 一眼发现篡改。

### 11.3 校验命令



```
python tools/integrity.py verify          # 全量校验 ledger 链 + 所有报告自洽
python tools/integrity.py verify R-xxx    # 只校验某一份报告
```

校验逻辑（四步）：



1. 重新算 target 文件的 sha256，对比报告里的 `target_hash`；

2. 重新算规则集和知识库的 hash，对比报告；

3. 重新算报告内容的 hash，对比 `report_hash`；

4. 沿 ledger 链往前追，每一行 `prev_hash` 必须等于上一行 `report_hash`。

### 11.4 Demo 边界（别吹过头）



* SHA-256 只防**无意改动**和**事后抵赖**；防故意篡改需要数字签名或第三方存证，那是 v1.0 以后的事。

* `report_hash` 算的时候必须先把自己这个字段置空，否则永远对不上（自指问题）。

* Ledger 文件建议设成只读权限，防止本地误改。



***

## 12. 日志与可观测性（Agent 自己也要被监督）

审查 Agent 作为独立裁判，它自己的每一次运行也必须可追溯、可举证。Demo 级别用本地 JSON 文件，不上 ELK / Prometheus / OpenTelemetry。

### 12.1 四类日志各自回答什么问题



| 日志类型                | 回答的问题                               | 粒度       |
| ------------------- | ----------------------------------- | -------- |
| 任务日志 `task_log`     | 这次任务是什么？工作流走到哪一步？成功还是在哪步挂了？         | 每次审查一条   |
| 决策日志 `decision_log` | Agent 为什么这么判？依据是什么？考虑过哪些替代方案？为什么没选？ | 每个关键决策一条 |
| 工具日志 `tool_log`     | 调了什么工具？输入什么？输出什么？耗时多久？重试了几次？        | 每次工具调用一条 |
| 成本日志 `cost_log`     | 烧了多少 token？总耗时？错误率？成功率？             | 每次审查汇总一条 |

### 12.2 日志目录结构

每次审查在 `logs/R-<日期>-<序号>/` 下产出四个文件：



```
logs/
└── R-20261022-001/
    ├── task.json        # 任务日志（单条）
    ├── decisions.jsonl  # 决策日志（逐条追加）
    ├── tools.jsonl      # 工具日志（逐条追加）
    └── cost.json        # 成本汇总（单条）
```

> `.jsonl`
>
>  \= JSON Lines，每行一个独立 JSON，适合逐条追加，不用每次重写整个文件。

### 12.3 各日志 Schema 示例

**task.json** —— 记录工作流每一步的状态：



```
{
  "review_id": "R-20261022-001",
  "user_request": "审查 targets/answer_001.txt",
  "steps": [
    {"step": "S1", "name": "接收解析", "status": "success", "ts": "2026-10-22T15:30:01+08:00", "latency_ms": 120},
    {"step": "S2", "name": "规则匹配", "status": "success", "ts": "2026-10-22T15:30:01+08:00", "latency_ms": 45},
    {"step": "S3", "name": "分维审查", "status": "success", "ts": "2026-10-22T15:30:04+08:00", "latency_ms": 3200}
  ],
  "final_status": "success",
  "abort_reason": null
}
```

如果中途报错终止，`final_status` 写成 `"aborted_at_S3"`，`abort_reason` 写具体错误。

**decisions.jsonl** —— 每行一个决策：



```
{"ts":"2026-10-22T15:30:03+08:00","decision":"MAU=10亿 定为 blocker","rule_id":"FACT-001","rationale":"与知识库偏差一个数量级","alternatives":["major","minor"],"why_not_alternative":"major 适用于小偏差，这里差 10 倍属事实硬伤"}
{"ts":"2026-10-22T15:30:05+08:00","decision":"verdict=fail","rule_id":"OVERALL-001","rationale":"存在 ≥1 条 blocker 级 issue","alternatives":["warn"],"why_not_alternative":"warn 仅允许 minor/major 问题"}
```

**tools.jsonl** —— 每次工具调用一行：



```
{"ts":"...","tool":"retrieve","input":"豆包 MAU 2024","output_summary":"命中 3 条知识库文档，最相关 doc_005","latency_ms":320,"status":"success","retry":0}
{"ts":"...","tool":"regex_check","input":"数字+单位抽取","output_summary":["2023年9月","10亿"],"latency_ms":5,"status":"success","retry":0}
```

**cost.json** —— 一次审查的成本快照：



```
{
  "total_latency_ms": 5400,
  "llm_tokens": {"input": 12000, "output": 800},
  "tool_calls": 5,
  "tool_success": 5,
  "tool_failures": 0,
  "success_rate": "5/5 = 100%",
  "errors": []
}
```

### 12.4 日志本身也进哈希链

报告 schema 的 `integrity` 块里已经加了 `logs_hash`（见第 7 节）。这样三份东西绑定：



* 报告（裁决定论）

* ledger（历史链）

* 日志（Agent 自己的行为记录）

**改任何一处，另外两处都对不上。** 防止 Agent 事后改自己的运行记录来粉饰结论。

### 12.5 一条铁律：决策日志必须当场写

决策日志不能等审查结束后回忆补写 —— 否则就失去了「决策依据可举证」的意义。这和 issue 的 `quote` 必须逐字摘录是同一个道理：**记录的瞬间决定证据的可信度**。

### 12.6 Demo 边界



* 不接 ELK / Grafana / OpenTelemetry，本地文件就够；

* 不做分布式 trace（单进程 Demo 用不上）；

* 提供一个 `tools/log_summary.py`，跑完后批量统计成功率、平均耗时、总 token；

* `logs/` 目录定期归档，别和 `outputs/` 混在一起。



***

## 13. Demo 项目目录结构



```
review-agent/
├── README.md
├── rules/                  # 规则库（YAML）
│   ├── fact.yaml
│   ├── safety.yaml
│   ├── compliance.yaml
│   └── style.yaml
├── knowledge/              # 事实库 / 政策文档 / 历史基线
├── targets/                # 待审对象（只读）
│   ├── answer_001.txt
│   └── contract_001.md
├── tools/                  # 检查脚本
│   ├── retrieve.py
│   ├── regex_check.py
│   ├── lint.py
│   ├── integrity.py       # 哈希计算 + ledger 校验
│   └── log_summary.py    # 批量统计成功率/耗时/token
├── review.py               # 主流程：S1~S6
├── outputs/                # 审查产物（可写）
│   ├── R-20261022-001.json
│   └── R-20261022-001.md
├── ledger/                 # 审计链（append-only，建议设只读）
│   └── chain.log
└── logs/                   # 运行日志（按 review_id 分目录）
    └── R-20261022-001/
        ├── task.json
        ├── decisions.jsonl
        ├── tools.jsonl
        └── cost.json
```



***

## 14. 一个完整跑通样例（让你有体感）

**输入 target**（`targets/answer_001.txt`）：

> "豆包是字节跳动 2023 年 9 月推出的大模型产品，截至 2024 年月活已经突破 10 亿。"

**S3 事实审命中**：



* `quote`: "截至 2024 年月活已经突破 10 亿"

* `rule_id`: FACT-001

* `evidence`: 知识库中豆包公开 MAU 数据为～1 亿量级，10 亿与公开口径不符

* `confidence`: 0.85

* `severity`: blocker

**最终裁定**：`verdict: fail`，`to_fix` 里挂这条，建议改为「以公开披露数据为准」。



***

## 15. MVP vs 后续扩展（别一次做太大）



| 阶段           | 做什么                                                       |
| ------------ | --------------------------------------------------------- |
| **MVP（先跑通）** | 文本类对象、4 维顺序审查、YAML 规则、JSON+MD 输出、retrieve+regex+lint 三个工具 |
| v0.3         | 真・多 Agent 并行调度、Web 表单上传                                   |
| v0.4         | 图片 OCR 审查、PDF 合同审查                                        |
| v1.0         | 规则库版本管理、历史审查对比、一键导出整改单                                    |



***

## 16. 容易踩的坑（提前写在这里）



1. **规则写在 prompt 里** → 后期没法维护，必须落到 `rules/*.yaml`。

2. **quote 被改写** → 下游没法回查证据，必须逐字摘。

3. **confidence 全写 1.0** → 等于没标，保留 0.6\~0.9 的分布才有意义。

4. **审查器直接改原文** → 越权，只能写建议，整改由人或另一个修改 Agent 做。

5. **规则贪多** → Demo 阶段 10\~20 条规则跑顺，比 100 条半吊子规则有用。

6. **把 hash 当防恶意篡改** → SHA-256 只防无意改动和事后抵赖；防故意篡改要数字签名，Demo 阶段别吹过头。

7. **report\_hash 自指死循环** → 算报告 hash 前必须先把 `report_hash` 字段置空，否则永远对不上。

8. **决策日志事后补写** → 决策必须当场记，事后回忆等于没记；这和 quote 必须逐字摘是同一个道理。

9. **日志和报告分家** → 日志不算进哈希链，Agent 改自己的运行记录就发现不了；`logs_hash` 必须挂进 `integrity` 块。