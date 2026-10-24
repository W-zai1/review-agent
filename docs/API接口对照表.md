# 前后端接口对照表（单一事实来源）

> 对象：`web/app.js`（前端，零依赖原生 JS） ↔ `server.py`（后端，标准库 `http.server`）
> 维护约定：**任何一侧增删接口或改字段，必须同步更新本表 + `tests/test_api_contract.py`**，
> 否则契约测试会红。本表与契约测试共同保证「前端调的每个接口都有后端代码，且字段对得上」。
>
> 最近核对：2026-10-23 · 契约测试 33 项全绿（`python -m unittest tests.test_api_contract -v`）

## 1. 架构与请求链路

```
浏览器 web/index.html
  └─ web/app.js  api(path, opts) ── fetch(API_BASE + path)
        │  API_BASE 来自 web/config.js（"" = 同源，部署时改这里）
        ▼
server.py  Handler.do_GET / do_POST
  ├─ _guard_api()        Host / Origin / Sec-Fetch-Site 防护（仅放行本机）
  ├─ 静态路由            / → web/index.html；/static/<path> → web/
  └─ /api/* 业务 handler
        ├─ 规则库      tools/rules_store.py + config/rules.state.json
        ├─ LLM 配置    tools/llm_client.py + config/llm.yaml / llm.local.json
        ├─ 审查执行    review.py 子进程（同步 / 异步 + --progress 进度事件）
        ├─ 报告日志    outputs/*.json · logs/R-*/{task,decisions,tools,cost}
        └─ 完整性      tools/integrity.py + ledger/chain.log
```

错误约定：所有 `/api/*` 一律返回 HTTP 200 + JSON（业务错误用 `{"ok": false, "error": "..."}`）；
仅静态资源缺失与安全防护拒绝使用 403 / 404。前端 `api()` 只解析 JSON，
非 JSON 响应会统一显示「响应解析失败（HTTP n）」。

## 2. 接口对照总表（19 个前端调用点）

| # | 前端调用点（app.js） | 方法 + 路径 | 后端 handler（server.py） | 响应契约（前端消费的字段） | 契约测试 |
|---|---|---|---|---|---|
| 1 | L130 `loadTargets` | GET `/api/targets` | `_targets` L324 | `items[].{name,rel,size,mtime,uploaded}` | TargetsTests |
| 2 | L175 `uploadFile`（裸 fetch） | POST `/api/upload?name=` | `_upload` L339 | 文本：`ok` + `file.{name,rel,size}`；二进制：`ok` + `kind`(pdf/docx/image/zip) + `file`（首个可审对象） + `extracted[].{name,rel,chars,from,archive}` + `warnings[]` + `extractor` + `original`；图片额外经 OCR 转写，`warnings` 说明识别误差；失败 `error` | UploadTests + BinaryUploadTests |
| 3 | L243 `loadReports` | GET `/api/reports` | `_reports` L447 | `items[].{id,verdict,target_type,target_path,target_name,review_mode,timestamp,issues,stats{blocker,major,minor,nit},report_hash,mtime}`；`order[]`=当前自定义顺序 | ReportsLogsIntegrityTests |
| 4 | L424 `selectReport` | GET `/api/report?id=` | `_report` L574 | `ok`；`report.{review_id,verdict,review_mode,target_path,timestamp,stats,coverage,limitations,issues[]}` | 同上 |
| 5 | L503 `loadIntegrity` | GET `/api/integrity` | `_integrity` L769 | `{ok,chain_ok,entries[]{seq,review_id,report_hash,report_hash_short,prev_hash,ts,ok,op,deleted,deleted_at},reports[]{id,ok,rules_ok,stale,rules_version,verdict,hash_short},broken_at,stale_count,rules_version,detail}` | 同上 + EmptyIntegrityTests |
| 5.5 | `loadAgents` | GET `/api/agents` | 内联 | `{ok,items[]{id,name,version,description,auto,dimensions[]{id,label,color},target_types[],checks[],boundaries[],rules}}`；manifest 无效时该项带 `error` 字段而不是整体 500 | AgentApiTests |
| 6 | L572 `loadRules` | GET `/api/rules` | `_rules` L585 | `{ok,items[]{rule_id,dimension,desc,severity,method,source,file,enabled},total,enabled,dimensions}` | RulesTests |
| 7 | L608 `toggleRule` | POST `/api/rules/toggle` | `_toggle_rule` L615 | `{ok,rule_id,enabled,enabled_count,total}` | 同上 |
| 8 | L631 `loadLogs` | GET `/api/logs?id=` | `_logs` L641 | `{ok,id,task{steps[]{step,name,status,ts,latency_ms,detail{}},final_status},decisions[]{ts,decision,rule_id,rationale,alternatives,why_not_alternative},tools[]{ts,tool,input,output_summary,latency_ms,status,retry},cost{total_latency_ms,llm_*}}` | ReportsLogsIntegrityTests |
| 9 | L766 `startReview` | POST `/api/review/start` | `_start_review` L723 | body `{file, llm?, type?, agent?}`；`{ok,job_id}`；冲突时 `error` 含「进行中」；未知 `agent` 直接返回 `{ok:false,error:"未知 Agent：…；可选：…"}`（**不启动任务**） | AsyncReviewTests + AgentApiTests |
| 10 | L784 `pollJob` | GET `/api/review/status?job=` | `_review_status` L692 | `{ok,job{id,file,status,created,finished,report_id,error,llm,type},steps[]{step,name,status,ts,latency_ms},report}` | 同上 |
| 11 | L861 `loadProviders` | GET `/api/llm/providers` | 内联 L239 | `{ok,items[]{id,name,base_url,model,models[],needs_key,local,hint}}` | LlmConfigTests |
| 12 | L918 `loadLlmStatus` | GET `/api/llm/status` | `_llm_status` L845 | `{ok,enabled,available,base_url,endpoint,model,api_key(masked),api_key_set,timeout,max_retries,max_tokens,source,local_override}` | 同上 |
| 13 | L1002 `saveLlm` | POST `/api/llm/config` | `_save_llm_config` L855 | `{ok,saved,status{…同 12}}`；校验失败 `error` | 同上 |
| 14 | L1044 `testLlm` | POST `/api/llm/test` | `_test_llm` L936 | 成功 `{ok,model,endpoint,latency_ms,usage,reply,note}`；`reply` 为空时 `note` 给出原因（思考型模型 token 预算不足）；失败 `{ok:false,error}` | 同上 |
| 15 | L1074 `resetLlm` | POST `/api/llm/reset` | `_reset_llm_config` L988 | `{ok,status{…同 12}}` | 同上 |
| 16 | L393 `confirmDeleteReport` | **DELETE** `/api/report?id=` | `_delete_report` L500 | 成功 `{ok,id,moved[],trash,ledger_tombstone}`；未确认 `{ok:false,need_confirm:true,error}` | ReportManagementTests |
| 17 | L363 `persistReportOrder` | POST `/api/reports/order` | `_save_report_order` L475 | `{ok,order[],count}`；`order` 非数组 → `{ok:false,error}` | 同上 |
| 18 | L976 `loadLlmHistory` | GET `/api/llm/history` | `_load_llm_history()` | `{ok,items[]{base_url,model,api_key_set,last_used_at}}`（最近 12 条，**不含 Key 明文**） | LlmMemoryTests |
| 19 | L1013 `applyHistoryItem` | POST `/api/llm/history/restore` | `_restore_llm_history` L994 | `{ok,status{…同 12}}`；换 host 且无 Key → `{ok:false,error}` | 同上 |

后端另提供 1 个**前端未调用**的兼容接口：`POST /api/review`（`_run_review` L777，
同步跑 `review.py` 返回最新报告，供命令行/脚本使用；契约测试 SyncReviewTests 覆盖）。

## 3. 关键交互序列

### 3.1 异步审查闭环（审查台主流程）

```
startReview ─POST /api/review/start {file, llm?}→ job_id
pollJob     ─GET  /api/review/status?job=      ─┐ 每 350ms 轮询
            ← {job:{status:running}, steps:[S1..S6 进度事件]}
            ← {job:{status:done, report_id}, report:{…完整报告…}}
            ├→ loadReports / loadIntegrity 刷新右栏
            └→ renderReport 渲染裁定 + issue 列表
```

- 进度事件来源：`review.py --progress <file>`，`Session.step()` 每步追加一行 JSON 到
  `logs/.jobs/<job_id>.jsonl`（S1 接收 / S2 规则匹配 / S3 分维审查 / S4 证据锚定 /
  S5 交叉复核 / S6 裁定汇总）。
- 同一时刻只允许一个审查任务（`JOBS_LOCK` + running 检查），避免 `review_id` 竞态。
- `llm` 字段：`true` → `--llm` 强制调用；`false` → `--no-llm` 纯规则；缺省 → auto。

### 3.2 LLM 配置弹窗

```
openLlmModal → GET /api/llm/status + /api/llm/providers（服务商预设）
saveLlm      → POST /api/llm/config {base_url, model, api_key?, clear_key?, enabled?, timeout?, max_tokens?}
testLlm      → POST /api/llm/test   （草稿配置，不落盘；Key 为空沿用已保存）
resetLlm     → POST /api/llm/reset  （删除 config/llm.local.json）
```

配置优先级：`--no-llm > config/llm.local.json（前端保存）> 环境变量 > config/llm.yaml > 默认值`。
Key 只以 masked 形式经 `/api/llm/status` 与报告 `llm.api_key` 返回；
切换服务商 host 时不带新 Key 会被后端拒绝（防旧 Key 外发）。

### 3.3 历史报告管理（删除 + 拖拽排序）

```
【拖拽排序】
拖动行 ─dragstart/dragover/drop→ moveReportInList() 重排 state.reports
        └→ persistReportOrder() ─POST /api/reports/order {order:[R-xxx…]}
           → config/report_order.json 落盘 → 下次 GET /api/reports 按此顺序返回
        （未在 order 中的新报告按 mtime 倒序补到末尾，不会“消失”）

【删除（二次确认）】
点🗑 → askDeleteReport() → #confirmModal 弹窗说明影响
      → 用户点「确认删除」→ confirmDeleteReport()
      → DELETE /api/report?id=R-xxx  body: {confirm:"R-xxx"}
         · 服务端校验 confirm 必须等于 id，否则 400 语义 {ok:false,need_confirm:true}
         → 报告 JSON/MD + logs/<id>/ 移入 outputs/.trash/（可自行找回）
         → ledger 对应行原位打墓碑 op=delete（seq/report_hash/prev_hash 一律不动）
         → 从 config/report_order.json 摘除
      → loadReports() + loadIntegrity() 刷新
```

**为什么删除不破坏审计链**：`ledger/chain.log` 的每行含 `prev_hash` 与 `report_hash`，
下一行的 `prev_hash` 等于上一行的 `report_hash`。删除时**不删行、不重算 hash**，
只在该行加 `op="delete"`、`deleted_at`、`deleted_hash` 三个字段——链的衔接分毫不动，
`verify_ledger` 依旧通过，删除痕迹留在链上可审计（`/api/integrity` 的 entries 里
`deleted=true`）。

**删除的两道防线**：UI 弹窗是第一道；服务端强制 `confirm=id` 是第二道——
绕过 UI 直接打接口也删不掉（`need_confirm=true`）。

### 3.4 二进制文档上传（PDF / Word / 图片 / 压缩包）

```
uploadFile ─POST /api/upload?name=x.pdf (binary)→ _upload
  ├─ 文本类 → 严格校验(UTF-8/拒 NUL/剥 BOM) → uploads/x.txt
  └─ 二进制类 → 归档 uploads/_originals/x.pdf
              → tools/ingest.py 摄取
                 ├ pdf   → 内置提取器 / pypdf      → uploads/x.pdf.txt  (+_meta/x.pdf.txt.json)
                 ├ docx  → zipfile + document.xml  → uploads/x.docx.txt
                 ├ image → tools/ocr.py 多引擎 OCR   → uploads/x.image.txt
                 └ zip   → 递归展开(防 slip/炸弹)  → 每个成员一个待审对象
              → 响应 {ok,kind,file,extracted[],warnings[],extractor,original}
              → 前端 loadTargets(file.rel) 选中新对象
```

**OCR 引擎优先级**（`tools/ocr.py` 自动探测，失败自动降级）：
`vision`（配置了 `config/ocr.json` 的 vision 段）→ `winrt`（Windows 内置，零安装）→
`rapidocr`（可选 pip）→ `tesseract`（可选）。全部不可用时明确标注「未做内容审查」，
**不返回空文本冒充已识别**。

**审查侧**：派生文本走完全相同的文本链路（quote 逐字校验不受影响）；
`review.py` 通过 `load_source_meta()` 读 `uploads/_meta/<名>.json`，
把来源写进报告 `coverage.source`，并在 `limitations` 补一条「抽取生成，仅覆盖文本层」。

**安全**：zip slip 整包拒绝；解压总量 ≤ 50 MB、成员 ≤ 200、嵌套 ≤ 2；
PDF 流解压 ≤ 8 MB；提取文本 ≤ 200k 字符；`_originals/` `_meta/` 不进待审列表。

### 3.5 审查任务书、开放发现与未覆盖声明（v0.4.0）

```
config/charter.json ──→ review.py:build_charter(target_type)
                          ├ 用途 / 受众 / 后果等级 / 严格度 / 重点关注 / 特别说明
                          └─→ llm_review.build_messages(dim, rules, text, charter)
                                注入每个维度的 prompt（模型由此知道「判定什么、标准是什么」）

LLM 产出 ─→ llm_review.validate()
            ├ rule_id 在白名单   → 正常，severity 取规则库定义
            ├ rule_id 匹配 OPEN-XXX → 开放发现，severity 取系统默认档(major)，
            │                          issue.open_discovery=true
            └ 其它 rule_id       → 丢弃（附「清单外发现请用 OPEN-XXX 格式」）
            **所有分支都必须通过 quote 逐字校验**（安全线不放宽）
```

### 3.5.1 审查分层：确定性算术先行，语义模型补位

```
S2 规则层
 ├ regex / fact库 / 合同要件 / 静态扫描
 └ arith_check（确定性，不调模型）
     ├ sum      : 360+210+480=1050  vs 文中 1080      → LOGIC-001
     ├ weekday  : 2026-10-18 实为周日 vs 文中「周六」  → LOGIC-002
     ├ duration : 7/1→10/15 共106天≈3.5月 vs 文中4.5月 → LOGIC-002
     └ ratio    : 50/220=22.7% vs 文中 25%            → LOGIC-001
S3 语义层（LLM，按维度）
```

`issue.tool` 形如 `arith_check:weekday` / `llm_api(<model>)` / `regex + checklist`，
**看到 `arith_check:*` 就知道这条结论不来自模型**，可直接复算复核。
算术层遵守 `rules.state.json` 的启停（被停用的 LOGIC-xxx 不会产出）。

**报告结构新增字段**（`GET /api/report?id=` 与 `logs/<id>/cost.json` 同步）：

| 字段 | 位置 | 含义 |
|---|---|---|
| `coverage.charter` | report.coverage | 本次审查任务书（用途/标准/后果等级） |
| `target_text` | report | **原文快照**（与 issue.quote 同一基准），前端据此渲染标注视图 |
| `coverage.uncovered[]` | report.coverage | **本次没查什么**（外部核验/链接/图表/法律效力/模型误差） |
| `coverage.source` | report.coverage | 来源标注（PDF/Word/图片 OCR，含 ocr 引擎） |
| `issue.open_discovery` | issue | 是否为规则清单外的独立发现 |
| `llm.charter` | report.llm | 任务书副本 |
| `llm.open_discoveries` | report.llm | 开放发现总数 |
| `llm.truncated_dimensions[]` | report.llm | 因 max_tokens 截断而**未完成审查**的维度（含"已救回部分条目"的情况） |
| `coverage.chars_total` | report.coverage | 待审原文总字符数 |
| `coverage.chars_reviewed` | report.coverage | **实际进入语义审的字符数**（`min(chars_total, max_input_chars)`） |
| `coverage.text_coverage_pct` | report.coverage | 文本覆盖率百分比，前端裁定横幅常驻显示 |
| `coverage.complete` | report.coverage | 是否全文完整审查（输入/输出都无截断才为 true） |
| `coverage.agent` / `coverage.dimensions_checked` | report.coverage | 本次用的是哪个 Agent、实际跑了哪些维度 |
| `agent{id,name,version,selected_by,dimensions[],checked_dimensions[],checks[],rule_sources[],boundaries[]}` | report | **本次判定标准**。`dimensions[]` 含 `label/color`，前端据此渲染新维度，不必改前端 |
| `integrity.agent_dir_hash` / `integrity.agent_version` | report.integrity | Agent 判定标准（manifest + 自带规则/检查器）的哈希与版本；版本没变却 hash 变了判篡改 |
| `integrity.knowledge_version` | report.integrity | 知识库版本；与 `rules_version` 同逻辑，区分「补数据」与「偷改事实库」 |
| `coverage.input_truncated_dimensions[]` | report.coverage | 输入被截断的维度——其后原文**根本没有进入审查** |
| `coverage.output_truncated_dimensions[]` | report.coverage | 输出被截断的维度——结论可能不完整 |
| `coverage.chunked` / `coverage.chunks` | report.coverage | 是否分块审查、分了几块（长文不再截断） |
| `coverage.type_detection` | report.coverage | 类型判定：`{type, confidence, source, reason}`，猜测置信度 <0.8 时只增检查不关检查 |
| `excluded[]` | report | **模型自否**的条目（自己写了不成立/撤回/无需整改），不计入异议与待整改 |
| `suppressed[]` | report | 长文里同类过多的 minor/nit，JSON 完整保留但清单不展开 |
| `issue.aggregated_count` / `aggregated_ids` | issue | 同类提示聚合后的总处数与其余条目 id（数据在 `suppressed` 里） |
| `llm.cache_hits` | report.llm | 语义审命中缓存的次数（命中不消耗 token） |
| `llm.dimensions[dim].finish_reason` | report.llm | `length` 表示输出被截断 |

**截断语义**：`finish_reason=length` 且正文为空时，该维度标记为「未完成审查」，
并写入报告 `limitations`——**不能据此认为该维度无问题**。

**截断容错**：`_loads_array` 在 JSON 数组被截断时，会用括号配平扫描**救回已写完的完整对象**
（`_salvage_objects`），而不是整条丢弃。实测中 `max_tokens=2048` 时模型已正确写出 3810 字符
（含正确的算术矛盾判断）却因 JSON 未闭合被整条丢弃；容错 + `max_tokens=8192` 后
同一份样本的 issue 数从 **5 条 → 23 条**。

## 4. 安全防护（_guard_api，所有 /api/* 必经）

| 检查 | 规则 | 违反时 |
|---|---|---|
| Host | 仅 `127.0.0.1` / `localhost` / `::1` | 403「Host 不被允许」 |
| Origin | 缺失或本机 Origin 放行 | 403「跨站请求已被拒绝」 |
| Sec-Fetch-Site | `cross-site` 拒绝 | 403「跨站请求已被拒绝」 |

其他边界：审查目标必须 resolve 后在 `targets/` 内；报告/日志 ID 正则白名单
`R-\d{8}-\d{3}`；上传 basename 清洗 + 扩展名白名单 + UTF-8 严格解码 + 拒 NUL + 2 MB 上限。

## 5. 契约测试

```bash
python -m unittest tests.test_api_contract -v   # 50 项，约 70s
```

测试在临时项目副本上启动真实 `server.py`，按上表字段逐项断言，并包含：

- **Parity 测试**：解析 `web/app.js` 中全部 `/api/` 调用点，逐个断言 `server.py`
  存在对应路由——前端新增调用即红，后端删路由即红；
- 异步审查闭环（start → running → done → 报告 → 日志 → 完整性）；
- 上传白名单 / NUL / BOM / 重名 / 路径穿越；
- **二进制上传**：PDF 与 DOCX 抽取文本、ZIP 递归展开、图片标注未覆盖、
  zip slip 与旧版 .doc 拒绝、原件归档不进待审列表、派生文本审查标注来源；
- 规则启停往返与计数；
- LLM 配置校验、保存/重置、换商 Key 保护、试连失败优雅降级；
- **报告删除**：缺 confirm / confirm 不匹配一律拒绝；确认后进回收区、列表移除、
  审计链不断且留墓碑、已删日志不可读；
- **报告排序**：顺序落盘并回读生效；非法 id 与非数组输入被拒/过滤；
- **前端 DOM 契约**：`app.js` 引用的每个 `$("#id")` 都必须存在于 `index.html`；
- Host / Origin / Sec-Fetch-Site 拒绝与本机放行。

## 6. 变更检查单

改接口时按此清单过一遍，保持三方（app.js / server.py / 本表+测试）一致：

1. `web/app.js` 增删调用点 → 同步 `server.py` 路由与 handler；
2. 改请求字段 → 更新后端校验与本文档「请求」列；
3. 改响应字段 → 更新本文档「响应契约」列 + `tests/test_api_contract.py` 断言；
4. **动了 `outputs/` 的文件结构（新增/删除/改名）→ 检查 `OUTPUTS.glob` 的匹配模式
   是否会把非报告文件误当报告**（现约定只扫 `R-*.json`，bench 产物放 `outputs/bench/`）；
5. **动了删除/排序 → 必须验证 `ledger/chain.log` 仍能通过 `tools/integrity.py verify`**；
6. 跑 `python -m unittest discover -s tests -v` 全绿再收工。
