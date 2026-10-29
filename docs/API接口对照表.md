# 前后端接口对照表（单一事实来源）

> 对象：`web/app.js`（前端，零依赖原生 JS） ↔ `server.py`（后端，标准库 `http.server`）
> 维护约定：**任何一侧增删接口或改字段，必须同步更新本表 + `tests/test_api_contract.py`**，
> 否则契约测试会红。本表与契约测试共同保证「前端调的每个接口都有后端代码，且字段对得上」。
>
> 最近核对：2026-10-26 · 契约测试 130 项全绿（`python -m unittest tests.test_api_contract -v`）

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

## 2. 接口对照总表（25 个前端调用点）

| # | 前端调用点（app.js） | 方法 + 路径 | 后端 handler（server.py） | 响应契约（前端消费的字段） | 契约测试 |
|---|---|---|---|---|---|
| 1 | L130 `loadTargets` | GET `/api/targets` | `_targets` L324 | `items[].{name,rel,size,mtime,uploaded,role,reviewable}`；**排序 = 可审对象在前 + 组内 mtime 降序**（刚上传的排第一）；`role=labels`（`*.labels.json` 机读标注）`reviewable=false`，前端置灰不可选，后端 `_resolve_target` 直接拒绝 | TargetsTests |
| 2 | L175 `uploadFile`（裸 fetch） | POST `/api/upload?name=` | `_upload` L339 | 文本：`ok` + `file.{name,rel,size}`；二进制：`ok` + `kind`(pdf/docx/image/zip) + `file`（首个可审对象） + `extracted[].{name,rel,chars,from,archive}` + `warnings[]` + `extractor` + `original`；图片额外经 OCR 转写，`warnings` 说明识别误差；失败 `error` | UploadTests + BinaryUploadTests |
| 2.5 | `uploadPurposeFile`（裸 fetch） | POST `/api/purpose/upload?name=` | `_purpose_upload` | `{ok,id,name,kind,chars,truncated,preview,warnings[],items[]}`；与 `/api/upload` 同一套白名单/体积上限/摄取链，但产物暂存 `state/purpose_uploads/`（**不进 targets/**），文本截断至 2000 字并记 warning；提取不到文本（扫描件/无 OCR）明确拒绝；暂存满 20 个拒绝 | PurposeUploadTests |
| 2.6 | `loadPurposeFiles` | GET `/api/purpose/files` | `_list_purpose_records` | `{ok,items[]{id,name,kind,chars,truncated,created,preview}}`（**不含原文**，只给 80 字预览） | 同上 |
| 2.7 | `removePurposeFile` | DELETE `/api/purpose/files?id=` | `_delete_purpose_record` | `{ok,id,items[]}`（删暂存记录 + `_originals/` 归档原件）；id 非法（含路径穿越）或不存在 → `{ok:false,error}` | 同上 |
| 2.8 | `loadScenarios` | GET `/api/scenarios` | 内联（`scenario.public_list`） | `{ok,version,default,items[]{id,name,desc,keep_note,disabled_count,disabled_rules[],disabled_dimensions[]}}`；配置损坏时 `{ok:false,error,items:[]}`（前端把下拉置灰并显示原因，不当成「没有情境」） | ScenarioApiTests |
| 2.9 | `loadFormatSpecs` | GET `/api/format-specs` | 内联（`format_spec.public_list`） | `{ok,version,default,items[]{id,name,source,desc,checks[],disabled_count,disabled_rules[]}}`；`checks` = 该规格会判哪几项（正文字体/字号/首行缩进/行距/标题层级/图表编号），前端塞进下拉选项文案；配置损坏时 `{ok:false,error,items:[]}` | FormatSpecApiTests |
| 3 | L304 `loadReports` | GET `/api/reports` | `_reports` | `items[].{id,verdict,target_type,target_path,target_name,review_mode,timestamp,issues,stats{blocker,major,minor,nit},report_hash,favorite,mtime}`；`favorites[]`=已收藏 ID。**排序唯一依据 `(timestamp, id)` 倒序，最新在最前**（不再有 `order`） | ReportsLogsIntegrityTests |
| 4 | L424 `selectReport` | GET `/api/report?id=` | `_report` L574 | `ok`；`report.{review_id,verdict,review_mode,target_path,timestamp,stats,coverage,limitations,issues[]}` | 同上 |
| 5 | L503 `loadIntegrity` | GET `/api/integrity` | `_integrity` L769 | `{ok,chain_ok,entries[]{seq,review_id,report_hash,report_hash_short,prev_hash,ts,ok,op,deleted,deleted_at},reports[]{id,ok,rules_ok,stale,rules_version,verdict,hash_short},broken_at,stale_count,rules_version,detail}` | 同上 + EmptyIntegrityTests |
| 5.4 | `markFalsePositive` / `markFalseNegative` | POST `/api/annotations` | `_add_annotation` | body `{kind, by, report_id?, issue_id?, target_path?, quote?, dimension?}` → `{ok,annotation{...}}`；`quote` 必须逐字出现在被审文本，否则 `{ok:false,error}`（**只能写标注，不能写规则库**） | LearningApiTests |
| 5.6 | （控制台未接） | GET `/api/annotations?id=` | 内联 | `{ok,items[]{ts,kind,by,target_path,quote,dimension,report_id,issue_id,note,trusted}}` | LearningApiTests |
| 5.7 | （控制台未接） | GET `/api/learning` | `rule_learning.summary` | `{ok,rules_version,annotations,negatives,candidates,by_status,promoted[],rolled_back[],auto_this_week,quota_week}`（只读） | LearningApiTests |
| 5.8 | `loadCandidates`（规则库右上角） | GET `/api/candidates` | `rule_learning.console_candidates` | `{ok,count,pending,items[]{candidate_id,dimension,status,severity,anchor,pattern,require,guard,desc,suggestion,confidence,provenance[]{doc,quote,by,ts},metrics{corpus_size,hit_doc_count,offtarget_count,internal_offtarget_count,suppressed_others,redundant_count,redundant_covered_by[],offtarget_docs[]},gate{passed,reasons[]},human_review{decision,decided_at,by,note},pending}}`；待审（pending）排在最前 | CandidateReviewApiTests |
| 5.9 | `decideCandidate`（批准/拒绝） | POST `/api/candidates/decide` | `_decide_candidate` | body `{candidate_id, decision:approve\|reject, note?, by?}` → `{ok,candidate_id,decision,dimension,status,human_review{...},next_step}`；**只写候选文件的 `human_review`，不动 rules/**；candidate_id 非法（含路径穿越）与 decision 非法均返回 `{ok:false,error}`；服务端设 `REVIEW_AGENT_ANNOTATION_TOKEN` 时需 `X-Review-Token` 头 | 同上 |
| 5.5 | `loadAgents` | GET `/api/agents` | 内联 | `{ok,items[]{id,name,version,description,auto,dimensions[]{id,label,color},target_types[],checks[],boundaries[],rules}}`；manifest 无效时该项带 `error` 字段而不是整体 500 | AgentApiTests |
| 6 | L572 `loadRules` | GET `/api/rules` | `_rules` L585 | `{ok,items[]{rule_id,dimension,desc,severity,method,source,file,enabled,capability{kind,executor,label}},total,enabled,dimensions,execution{deterministic,declarative,prompt_only}}` | RulesTests |
| 7 | L608 `toggleRule` | POST `/api/rules/toggle` | `_toggle_rule` L615 | `{ok,rule_id,enabled,enabled_count,total}` | 同上 |
| 8 | L631 `loadLogs` | GET `/api/logs?id=` | `_logs` L641 | `{ok,id,task{steps[]{step,name,status,ts,latency_ms,detail{}},final_status},decisions[]{ts,decision,rule_id,rationale,alternatives,why_not_alternative},tools[]{ts,tool,input,output_summary,latency_ms,status,retry},cost{total_latency_ms,llm_*}}` | ReportsLogsIntegrityTests |
| 9 | `startReview` | POST `/api/review/start` | `_start_review` | body `{file, llm?, type?, agent?, purpose?, purpose_files?, scenario?}`；`scenario` = 审查情境 id（`general` / `resume-screening`，见 §3.6；未知 id 直接返回 `{ok:false,error}`，错误文案含「未知审查情境」，**不起任务**）；`purpose` = 「审查目的」（≤300 字，控制字符清洗后经 `--purpose` 传给子进程）；`purpose_files` = 暂存的目的文件 id 数组（≤5，启动前校验，id 不存在/超量直接 `{ok:false,error}` 不起任务）；两者可同时用，服务端组装成一段目的并经 `--purpose-max <实际字数>` 告知解析上限（文件类目的可超 300 字）；`{ok,job_id}`；冲突时 `error` 含「进行中」；未知 `agent` 直接返回 `{ok:false,error:"未知 Agent：…；可选：…"}`（**不启动任务**）；报告新增 `purpose{text,lexicon_version,intents[],terms[],goals[]{id,intent,question,status,evidence_ids[]},evidence[]{id,quote,start,relation,source,rule_id,goal_id,goal_ids[],intent_id,relevance,rationale,judged},judge{mode,calls,error,judged,unjudged},limitations[]}`，未填目的时为 `null` | AsyncReviewTests + AgentApiTests + PurposeApiTests + PurposeUploadTests |
| 10 | L784 `pollJob` | GET `/api/review/status?job=` | `_review_status` L692 | `{ok,job{id,file,status,created,finished,report_id,error,llm,type},steps[]{step,name,status,ts,latency_ms},report}` | 同上 |
| 11 | L861 `loadProviders` | GET `/api/llm/providers` | 内联 L239 | `{ok,items[]{id,name,base_url,model,models[],needs_key,local,hint}}` | LlmConfigTests |
| 12 | L918 `loadLlmStatus` | GET `/api/llm/status` | `_llm_status` L845 | `{ok,enabled,available,base_url,endpoint,model,api_key(masked),api_key_set,timeout,max_retries,max_tokens,source,local_override}` | 同上 |
| 13 | L1002 `saveLlm` | POST `/api/llm/config` | `_save_llm_config` L855 | `{ok,saved,status{…同 12}}`；校验失败 `error` | 同上 |
| 14 | L1044 `testLlm` | POST `/api/llm/test` | `_test_llm` L936 | 成功 `{ok,model,endpoint,latency_ms,usage,reply,note}`；`reply` 为空时 `note` 给出原因（思考型模型 token 预算不足）；失败 `{ok:false,error}` | 同上 |
| 15 | L1074 `resetLlm` | POST `/api/llm/reset` | `_reset_llm_config` L988 | `{ok,status{…同 12}}` | 同上 |
| 16 | L393 `confirmDeleteReport` | **DELETE** `/api/report?id=` | `_delete_report` L500 | 成功 `{ok,id,moved[],trash,ledger_tombstone}`；未确认 `{ok:false,need_confirm:true,error}` | ReportManagementTests |
| 17 | L448 `toggleFavorite` | POST `/api/reports/favorite` | `_set_favorite` | body `{id:"R-xxx", favorite:true|false}` → `{ok,id,favorite,favorites[]}`；`favorite` 非布尔 / id 格式非法 / 报告不存在 → `{ok:false,error}`；重复收藏幂等 | ReportManagementTests |
| 18 | L976 `loadLlmHistory` | GET `/api/llm/history` | `_load_llm_history()` | `{ok,items[]{base_url,model,api_key_set,last_used_at}}`（最近 12 条，**不含 Key 明文**） | LlmMemoryTests |
| 19 | L1013 `applyHistoryItem` | POST `/api/llm/history/restore` | `_restore_llm_history` L994 | `{ok,status{…同 12}}`；换 host 且无 Key → `{ok:false,error}` | 同上 |

后端另提供 1 个**前端未调用**的兼容接口：`POST /api/review`（`_run_review` L777，
同步跑 `review.py` 返回最新报告，供命令行/脚本使用；同样支持 `purpose` / `purpose_files`；
契约测试 SyncReviewTests 覆盖）。

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

### 3.3 历史报告管理（时间排序 + 收藏 + 删除）

```
【排序：只按时间，最新在最上面】
GET /api/reports → 每份报告取 timestamp（生成时刻，非文件 mtime）
                 → sort(key=(timestamp, review_id), reverse=True)
                 → 同秒连跑两份时 timestamp 相同，用 review_id 兜底保证稳定
（拖拽排序已整套移除：draggable/⋮⋮手柄/moveReportInList/persistReportOrder
  /POST /api/reports/order 全部删除；config/report_order.json 不再读取）

【收藏】
点行内 ☆/★ → toggleFavorite() → POST /api/reports/favorite {id, favorite:true|false}
            → config/favorites.json 落盘 → 本节列表项 favorite 字段随之变化
            → 前端以响应为准改 state（不本地先点亮再回滚）

标题「★ 收藏（N）」→ favoritesOnly = !favoritesOnly（纯前端筛选，不发请求）
                  → 标题变 收藏（N），按钮变 ← 全部（总数）

【刷新列表（页面不实时）】
标题「⟳ 刷新」→ refreshReports({force:true}) ─GET /api/reports→ 重渲染 + 提示新增条数
window focus / visibilitychange → refreshReports()（8s 节流；收藏请求在飞时跳过）

【删除（二次确认）】
点🗑 → askDeleteReport() → #confirmModal 弹窗说明影响
      → 用户点「确认删除」→ confirmDeleteReport()
      → DELETE /api/report?id=R-xxx  body: {confirm:"R-xxx"}
         · 服务端校验 confirm 必须等于 id，否则 400 语义 {ok:false,need_confirm:true}
         → 报告 JSON/MD + logs/<id>/ 移入 outputs/.trash/（可自行找回）
         → ledger 对应行原位打墓碑 op=delete（seq/report_hash/prev_hash 一律不动）
         → 从 config/favorites.json 摘除（否则收藏里留下点不开的幽灵条目）
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

### 3.4.1 「审查目的」文件上传（PDF / Word / 图片 / ZIP / 文本）

```
purpose 面板「上传目的文件」─POST /api/purpose/upload?name=x.pdf (binary)→ _purpose_upload
  ├─ 文本类 → 严格校验(UTF-8/拒 NUL/剥 BOM) → 暂存 state/purpose_uploads/<id>.json
  └─ 二进制类 → 归档 state/purpose_uploads/_originals/x.pdf
              → tools/ingest.py 摄取（与待审对象同一条链，图片同样走 OCR）
              → 文本截断至 2000 字（超了记 warning，不静默）
              → 暂存 <id>.json {id,name,kind,chars,truncated,text,warnings,original,created}
  → 响应 {ok,id,name,kind,chars,truncated,preview,warnings,items[]}
  → 前端 renderPurposeFiles 渲染附件条（名字 + 字数 + ✕ 移除）

运行审查时 payload 带 purpose_files:[id,…]（≤5）
  → _assemble_purpose(手写目的, ids)：手写(≤300) + 各文件【目的文件：名】+ 文本
  → 总量超 6000 字整体截断，截断说明**并进目的文本**（报告 purpose.text 原样可见）
  → --purpose <组装文本> --purpose-max <实际字数> 传给 review.py 子进程
```

**与待审对象上传的三点区别**（有意为之）：

| | 待审对象 `/api/upload` | 目的文件 `/api/purpose/upload` |
|---|---|---|
| 落点 | `targets/uploads/`（进待审列表） | `state/purpose_uploads/`（**不进待审列表**） |
| 体积/字数 | 文本 2 MB / 二进制 20 MB | 同左，且**文本截断至 2000 字**（目的会进每个维度的 prompt） |
| 空文本 | 图片可"仅登记" | **明确拒绝**：不留空目的文件等着被静默忽略 |

**为什么 `--purpose-max` 单独一个 flag**：`purpose.parse()` 默认只解析前 300 字
（手写目的的 prompt 预算）；文件类目的需要更大上限，但**不能悄悄抬高全局上限**——
所以由调用方显式传入，截断与否都写进报告 `purpose.limitations`。

同步兼容接口 `POST /api/review` 同样接受 `purpose_files`（同一组装函数）。

### 3.5 审查任务书、开放发现与未覆盖声明（v0.4.0）

```
config/charter.json ──→ review.py:build_charter(target_type)
                          ├ 用途 / 受众 / 后果等级 / 严格度 / 重点关注 / 特别说明
                          └─→ llm_review.build_messages(dim, rules, text, charter)
                                注入每个维度的 prompt（模型由此知道「判定什么、标准是什么」）

LLM 产出 ─→ llm_review.validate()
            ├ rule_id 在白名单   → 正常，severity 取规则库定义
            ├ rule_id 匹配 OPEN-XXX → 开放发现，severity 取系统默认档(minor，
            │                          置信度 ≥0.85 才升 major)，issue.open_discovery=true，
            │                          rule_text 标明「未经规则库定义」，单次最多 3 条
            └ 其它 rule_id       → 丢弃（附「清单外发现请用 OPEN-XXX 格式」）
            **所有分支都必须通过 quote 逐字校验**（安全线不放宽）
```

**开放发现的可追溯性（2026-10-28 红队 C2）**：`OPEN-*` 不是规则库条目，因此
① 每条带 `open_discovery: true`、`confidence_basis: "model"`、`rule_text` 写明未定义；
② 报告顶层单独给一份 `open_discoveries[]{rule_id,severity,quote,confidence,source:"model"}`，
不混在规则判定里；③ `limitations` 自报「模型另报 N 条规则清单外发现，须人工确认」。
前端汇总表给这类条目加「规则外」标签，与 `AUTO-*`（自学习晋升）区分。

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
| `rule_execution{deterministic[],declarative[],prompt_only[],executors{}}` | report | 每条生效规则的执行方式。`prompt_only` 表示该规则只作为模型提示，结论不可复现 |
| `coverage.rule_execution_counts` | report.coverage | 三类执行方式的条数，供前端/摘要直接显示 |
| `coverage.input_truncated_dimensions[]` | report.coverage | 输入被截断的维度——其后原文**根本没有进入审查** |
| `coverage.output_truncated_dimensions[]` | report.coverage | 输出被截断的维度——结论可能不完整 |
| `coverage.chunked` / `coverage.chunks` | report.coverage | 是否分块审查、分了几块（长文不再截断） |
| `coverage.type_detection` | report.coverage | 类型判定：`{type, confidence, source, reason}`，猜测置信度 <0.8 时只增检查不关检查 |
| `excluded[]` | report | **模型自否**的条目（自己写了不成立/撤回/无需整改），不计入异议与待整改 |
| `suppressed[]` | report | ①长文里同类过多的 minor/nit；②**归口折叠**的条目（同片段让位给结论级规则、内部进度豁免 COMP-004）。两类都带 `folded_into` / `fold_reason`，JSON 完整保留、清单不展开 |
| `open_discoveries[]` | report | 规则清单外的模型发现（`OPEN-*`）：`{rule_id,severity,quote,confidence,source:"model"}`。与 `issues[]` 分开列，避免被误读成规则判定 |
| `issue.folded_into` / `fold_reason` | issue（suppressed 内） | 归口折叠去向与理由（如 `FACT-004→LOGIC-001`） |
| `issue.aggregated_count` / `aggregated_ids` | issue | 同类提示聚合后的总处数与其余条目 id（数据在 `suppressed` 里） |
| `law_library{available,articles_total,laws[],cited[],skipped[],error?}` | report | 法条库：本次收录了几部法几条、**实际引用了哪几条**（`cited[]{law,law_name,article,article_no,text,status,citation}`）、哪些检查因缺条文没跑（`skipped[]`）。`available:false` 表示库不可用，本次未做法条级比对 |
| `issue.citation` | issue | 单条依据：`{law,law_name,article,article_no,chapter,text,status,citation}`。`citation` 是可直接展示的一行文案（「《劳动合同法》第十九条（第二章 …）：原文」） |
| `issue.citations[]` | issue | 同句命中多条法条时的全部依据（如既超试用期又压低工资）。`issue.citation` 恒等于 `citations[0]`，兼容只读单条的前端 |
| `LAW-001` | rules/compliance.yaml | 法定量 vs 条文原文（试用期上限/竞业限制期限/试用期工资）。与 `FACT-001` 同句时**并入**其 evidence，不新增异议 |
| `fact_gaps{recorded,new,repeat,log,kinds{},top[],usable_as_evidence,disabled?,reason?,error?}` | report | 知识库缺口：本次有多少判断是「库里没依据」时下的。`kinds` 是四类计数（`model_judgment` / `law_missing` / `unsourced_claim` / `watchlist_unasserted`），`top[]` 是前 5 条 `{gap_id,kind,entity_guess,quote}`。**`usable_as_evidence` 恒为 false**——缺口里的 quote 来自被审文档，不能当依据。`disabled:true` 表示本次用 `--no-gap-log` 运行（不落被审文本片段） |
| `llm.cache_hits` | report.llm | 语义审命中缓存的次数（命中不消耗 token） |
| `llm.dimensions[dim].finish_reason` | report.llm | `length` 表示输出被截断 |

**截断语义**：`finish_reason=length` 且正文为空时，该维度标记为「未完成审查」，
并写入报告 `limitations`——**不能据此认为该维度无问题**。

**截断容错**：`_loads_array` 在 JSON 数组被截断时，会用括号配平扫描**救回已写完的完整对象**
（`_salvage_objects`），而不是整条丢弃。实测中 `max_tokens=2048` 时模型已正确写出 3810 字符
（含正确的算术矛盾判断）却因 JSON 未闭合被整条丢弃；容错 + `max_tokens=8192` 后
同一份样本的 issue 数从 **5 条 → 23 条**。

### 3.6 审查情境：同一份材料，不同用途关不同规则（v0.7.2）

```
左栏「审查情境」下拉 ──GET /api/scenarios──→ scenario.public_list()
                     （取清单与「关几条」，配置坏了就置灰并显示原因）
        │ 选择
        ▼
startReview ─POST /api/review/start {file, scenario:"resume-screening"}
        │  服务端 _resolve_scenario() 先校验：未知 id → {ok:false}，**不起任务**
        ▼
review.py --scenario resume-screening
        │  run_one(): merge_agent_rules() 之后、set_rule_index() 之前过滤
        │  （过滤晚了，被关的规则还会被 pattern 执行器跑到）
        ▼
报告：scenario{id,name,version,desc,disabled_rules[],disabled_detail[]}
      + limitations 里写明「关了哪几条、未报出 ≠ 已核实」
        │
        ▼
状态条 chip「情境：简历批量初筛 · 关闭 2 条规则」，悬停看每条规则原意
```

**为什么在 `set_rule_index` 之前过滤**：规则索引是给 `pattern_rules` 执行器用的。
先建索引再过滤列表，索引里仍留着被关规则，等于"报告说关了、实际还在跑"。

**为什么未知情境必须报错**：静默回退到默认情境，用户以为用了 A 尺度、
实际跑的是 B，报告照样"看起来正常"——这种错最难发现，也最伤可信度。

**为什么关了什么必须进 limitations**：不然「未报出资质问题」会被读成
「资质已核实」。二者差别很大，尤其在这种批量筛选场景。

### 3.7 格式规格：Word 版式按哪套标准判（v0.7.5）

```
左栏「格式规格」下拉 ──GET /api/format-specs──→ format_spec.public_list()
                     （每项带 checks[]：这条标准会判哪几项）
        │ 选择（默认空 = 不判版式）
        ▼
startReview ─POST /api/review/start {file, format_spec:"course-report-v1"}
        │  _resolve_format_spec()：未知 id → {ok:false}，**不起任务**
        ▼
review.py --format-spec course-report-v1
        │  run_one(): 仅当目标是 .docx/.docm 才处理；scenario 之后、set_rule_index 之前
        │  1) 按规格 disable_rules 过滤（none → FORMAT-001~005 不参与）
        │  2) spec.check 经 ctx["format_spec"] 传给 docx_format_checks.run_all()
        ▼
报告：format_spec{id,name,version,source,desc,criteria{},disabled_rules[],disabled_detail[]}
      + limitations 写明「哪几项本次未判」
```

**为什么数值不能写在 rules/ 里**：`rules/*.yaml` 是长期裁判尺度（什么算问题），
而「正文宋体小四、固定 22 磅」是**某一份委托方的格式要求**，换份期刊就全变。
写进规则库的结果是：规则库攒下多套互相矛盾的标准、审的时候没人知道选哪个、
改标准还得改 Python。现在加新标准 = 在 `config/format_specs.yaml` 加一段。

**为什么默认是 `none` 而不是某个"通用标准"**：字体/字号/行距没有通用正确答案。
没有委托方标准时判任何一项都是瞎猜，误报会让人放弃整份清单。
`none` 只保留 FORMAT-006（图表编号与引用一致），它不依赖具体版式数值。

**为什么没选规格也要写 limitations**：不写，「没报出版式问题」会被读成「版式合规」。
报告里必须出现「未提供格式规格：本报告不判断字体、字号、首行缩进、行距、标题层级」。

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
python -m unittest tests.test_api_contract -v   # 130 项，约 2 分钟
```

测试在临时项目副本上启动真实 `server.py`，按上表字段逐项断言，并包含：

- **Parity 测试**：解析 `web/app.js` 中全部 `/api/` 调用点，逐个断言 `server.py`
  存在对应路由——前端新增调用即红，后端删路由即红；
- 异步审查闭环（start → running → done → 报告 → 日志 → 完整性）；
- 上传白名单 / NUL / BOM / 重名 / 路径穿越；
- **二进制上传**：PDF 与 DOCX 抽取文本、ZIP 递归展开、图片标注未覆盖、
  zip slip 与旧版 .doc 拒绝、原件归档不进待审列表、派生文本审查标注来源；
- **目的文件上传**：txt / pdf / docx / zip 摄取暂存、2000 字截断告警、
  不进待审列表、无文本图片明确拒绝、暂存容量上限、删除含路径穿越防护、
  随审查进报告（含手写 + 文件组装）、启动前拒绝坏 id 与超量；
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
