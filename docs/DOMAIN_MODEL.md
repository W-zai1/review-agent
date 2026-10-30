# 领域模型 v1

> 代码：`review_agent/domain/`（models / enums / ids / transitions / policy / ports）
> 迁移适配：`review_agent/adapters/legacy_report.py`（边界层，依赖 domain + profiles）
> Profile 合成：`review_agent/profiles/resolver.py`
> 验收：`checks/test_domain_v1.py`、`checks/test_legacy_adapter.py`

## 1. 六个对象

```
Review ──1:N──▶ ReviewRun ──1:N──▶ Finding ──N:M──▶ Evidence
                    │                 │
                    │                 └──▶ Dispute（两条以上 Finding 互相矛盾）
                    └──1:1──▶ Decision（最终裁定）
```

| 对象 | 回答的问题 | 旧实现的对应物 |
| --- | --- | --- |
| `Review` | 审什么、按哪套标准、谁在什么时候发起 | 无（`review_id` 同时承担任务与执行身份） |
| `ReviewRun` | 这一次执行跑到哪、覆盖多少、是否完整 | `review_id` + `logs/<id>/task.json` + 报告 `coverage` |
| `Finding` | 哪里有问题、违了哪条规则、多确定 | 报告 `issues[]` / `suppressed[]` |
| `Evidence` | 凭什么这么说、校验过没有 | `issue.quote` + `issue.evidence` 两个字符串 |
| `Dispute` | 哪两条结论互相矛盾、谁裁的 | 报告 `disputes[]`（无 id、无状态） |
| `Decision` | 最终结论是什么、依据哪几条 | 报告 `verdict` + `core_conclusion` |

## 2. 对象定义

字段表只列持久化字段；所有对象都是 `frozen=True` 的 dataclass，构造即校验。

### 2.1 Review

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `R-YYYYMMDD-NNN` | 任务身份，稳定不随重跑变化 |
| `target` | `ReviewTarget` | 路径、SHA-256、类型判定、编码 |
| `profile_id` / `profile_version` | str | 本次审查按哪套口径 |
| `created_at` | datetime | 创建时间（时区敏感） |
| `status` | `ReviewStatus` | created / in_progress / completed / archived |
| `origin` | `ReviewOrigin` | web / api / cli / import |
| `title` | str | 人类可读标题（旧报告无此字段） |
| `declared_purpose` | str \| None | 用户填的审查目的 |
| `runs` | tuple[ReviewRun] | 执行历史，序号必须从 1 连续 |

**硬不变量**：`IN_PROGRESS` 及以上状态必须有 Run；`COMPLETED` 要求最后一个 Run 也是 `COMPLETED`；
Run 的 `review_id` 必须等于本 Review 的 id；Run 序号连续。

### 2.2 ReviewRun

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `R-…-RUNnn` | 执行身份 |
| `review_id` / `seq` | str / int | 所属任务、第几次执行 |
| `status` | `RunStatus` | created / running / awaiting_decision / completed / failed / aborted |
| `stage` | `Stage` | 当前阶段（八阶段之一） |
| `mode` | `RunMode` | `rule-only` / `rule+llm` |
| `profile` | `ReviewProfile` | **快照**：口径随 Run 落盘，规则库以后升级也可复现 |
| `started_at` / `finished_at` | datetime | 终态必须有结束时间，非终态不得有 |
| `integrity` | `IntegrityHashes` | 目标 / 规则集 / 知识库 / Agent / 报告的哈希 |
| `coverage` | `Coverage` \| None | 审了多少、哪些没审 |
| `stats` | `RunStats` | 四档计数（含被折叠的条目，口径同旧报告） |
| `decision_id` | str \| None | 完成态必须关联裁定 |
| `error` | str \| None | 失败/中止必须说明原因 |
| `checkpoint` | `Checkpoint` \| None | 最近可恢复点 |

**硬不变量**：`COMPLETED` ⇒ `stage == FINALIZE` ∧ 有 `decision_id` ∧ 无 `error`；
`FAILED`/`ABORTED` ⇒ `error` 非空；`finished_at >= started_at`。

> 为什么 `stage` 与 `status` 分开：阶段回答「做到哪一步」，状态回答「这次执行还活着吗」。
> 旧实现把两者混成一个布尔（有没有报告），中断后无法判断该重跑分析还是只重渲染。

### 2.3 Finding

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `ISS-0001` | 运行内序号（沿用旧格式） |
| `run_id` | str | 所属执行 |
| `dimension` | str | 维度 id（小写，来自 manifest） |
| `severity` | `Severity` | blocker / major / minor / nit |
| `quote` | str | 原文片段（缺失型问题可为空，但必须有 `statement`） |
| `location` | `Location` | 句子序号 / 字符区间 / 人类可读标签 |
| `confidence` | float 0..1 | 置信度 |
| `origin` | `FindingOrigin` | rule / model / heuristic / research / open_discovery |
| `rule` | `RuleRef` \| None | 挂哪条规则（规则外发现为 None） |
| `statement` | str | 结论说明（旧报告的 `issue.evidence`） |
| `confidence_basis` | `ConfidenceBasis` \| None | 置信度依据类型 |
| `status` | `FindingStatus` | proposed / needs_confirmation / confirmed / rejected / suppressed |
| `tool` | str | 产生该结论的工具名（自由文本，审计用） |
| `suggestion` | str | 整改建议 |
| `evidence_ids` | tuple[str] | 指向 Evidence |
| `severity_note` | str \| None | 严重度被调整时的说明（旧实现写进 evidence 文本） |
| `created_at` | datetime \| None | 产生时间 |

**硬不变量**：置信度在 0..1；`quote` 或 `statement` 至少一个非空；ID / 维度格式合法。

**软规则**（`policy.finding_violations`，不阻止加载）：
`blocker`/`major` 且置信度 < 0.7 时必须有 `severity_note`。历史报告有 19 条不满足，
适配器会补说明并写 warning——这是数据问题，不是模型 bug，不能靠抛异常掩盖。

### 2.4 Evidence

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `EV-0001` | 运行内序号（旧报告没有） |
| `run_id` | str | 所属执行 |
| `kind` | `EvidenceKind` | quote / external_source / rule_reference / knowledge_entry / tool_output |
| `quote` | str | 原文片段（quote 类必填） |
| `location` | `Location` | 位置 |
| `source_ref` | str \| None | 文件 / URL / 规则号 / 知识库键 |
| `verification` | `Verification` | anchored / placeholder / unanchored / not_checked |
| `verified_by` | str \| None | 谁校验的（工具名）；`ANCHORED` 时必填 |
| `note` | str \| None | 备注 |
| `created_at` | datetime \| None | 产生时间 |
| `finding_ids` | tuple[str] | 这条证据支撑哪些发现（一对多：同一句可同时支撑多条规则） |

**硬不变量**：`ANCHORED` ⇒ `verified_by` 非空；`EXTERNAL_SOURCE` ⇒ `source_ref` 非空；
`finding_ids` 里的每个 id 必须格式合法，且运行时必须指向本 Run 真实存在的发现
（`runtime.advance()` 校验，防止「报告上看起来有证据、点进去是一条不存在的 ISS 号」）。

**为什么证据自己记 `finding_ids`**：证据是**分析之后**才产出的（evidence 阶段），
而 `Finding` 不可变、构造时就得写 `evidence_ids`——晚到的证据没法再回填。
`Finding.evidence_ids` 因此只保留为「我引用了哪些证据」的反向索引，
由「构造发现时证据已存在」的路径（历史报告导入）填写。

「逐字命中原文」的校验规则沿用旧实现 S4（`review.py:2517-2557`）：非空 quote 回查不到原文
即丢弃；`（文档首段）` 这类占位引用属于缺失型问题，保留并标记 `PLACEHOLDER`。

### 2.5 Dispute

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `DSP-0001` | 运行内序号（旧报告没有） |
| `run_id` | str | 所属执行 |
| `point` | str | 争议点描述 |
| `issue_ids` | tuple[str] | 涉及的 Finding（至少 1 条） |
| `kind` | `DisputeKind` | severity_conflict / duplicate_finding / insufficient_evidence / contradiction / other |
| `status` | `DisputeStatus` | open / resolved / escalated |
| `between` | tuple[str] | 哪两个视角（如 `fact-reviewer`） |
| `resolution_hint` | str | 给人工的裁定提示 |
| `resolution` / `resolved_by` / `resolved_at` | | 解决后必填（`RESOLVED` 时前两项） |

### 2.6 Decision

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `DEC-0001` | 运行内序号 |
| `run_id` | str | 所属执行 |
| `verdict` | `Verdict` | pass / warn / fail |
| `rationale` | str | 结论正文（旧 `core_conclusion`） |
| `risk_level` | `RiskLevel` | low / moderate / high / critical |
| `reasons` | tuple[str] | 策略给出的分条理由（可复算） |
| `rule_refs` | tuple[str] | 依据的规则号 |
| `finding_ids` | tuple[str] | 支撑结论的发现 |
| `requires_human_review` | bool | 是否必须人工复核 |
| `policy_id` | str | 用的哪版策略（`review-decision-v1`） |
| `approved_by` / `approved_at` | | 人工批准（批准必须有时间） |
| `is_final` | 属性 | 不需要复核，或已有人批准 |

## 3. 值对象

| 值对象 | 字段要点 | 为什么单独存在 |
| --- | --- | --- |
| `Location` | `label` + `sentence` + `char_start/end` | 旧实现只有中文标签，无法算、无法比对 |
| `RuleRef` | `rule_id` + `dimension` + `text` + `declared_severity` | 「违了哪条」要能独立校验 |
| `RunStats` | 四档计数 + `from_findings()` | 统计口径统一，避免各处自己数 |
| `Coverage` | 字数、块数、失败块、未覆盖面 | 「没审」必须与「没问题」分开 |
| `IntegrityHashes` | 目标 / 规则 / 知识 / Agent / 报告 / 日志 / 前序哈希 | 对应旧报告 `integrity` 块，允许缺字段 |
| `ReviewTarget` | 路径 + SHA-256 + 类型判定 + 置信度 + 来源 | 类型猜错会让整套判定标准跑偏，需记录置信度 |
| `ReviewProfile` | 口径快照 + `fingerprint()` | 同一 id 不同指纹 = 判定标准被改过 |
| `Checkpoint` | 阶段 + 序号 + 状态哈希 | 恢复执行的最小依据 |
| `AuditEvent` | 序号 + 类型 + 名称 + 状态 + 耗时 + 明细 | 统一 `steps` / `tools` / `decisions` 三种旧记录 |

## 4. 标识符

| 对象 | 格式 | 分配方式 |
| --- | --- | --- |
| Review | `R-YYYYMMDD-NNN` | 当日内递增；旧实现扫 `outputs/` + `ledger`，用 `.lock` 防并发 |
| Run | `R-YYYYMMDD-NNN-RUNnn` | 同一 Review 内从 01 递增 |
| Finding | `ISS-NNNN` | 运行内递增（沿用旧格式） |
| Evidence | `EV-NNNN` | 运行内递增（新增） |
| Dispute | `DSP-NNNN` | 运行内递增（新增） |
| Decision | `DEC-NNNN` | 运行内递增（新增） |

解析函数在 `review_agent/domain/ids.py`，非法格式抛 `IdFormatError`（`DomainError` + `ValueError`）。
**不允许**在别处手写 f-string 拼 ID：格式一改，历史报告与 ledger 全部对不上。

## 5. 状态机

### Run

| 当前 | 允许迁移到 |
| --- | --- |
| created | running, aborted |
| running | awaiting_decision, completed, failed, aborted |
| awaiting_decision | completed, failed, aborted |
| completed / failed / aborted | （终态，不可复活） |

### Stage

只能前进，不能跳级、不能回退：

```
intake → ingestion → planning → analysis → evidence → cross_review → decision → finalize
```

* 原地重试允许（`ensure_stage_advance(s, s)`）。
* 回退一律禁止：要重跑上一阶段，应新建 Run，而不是改旧 Run 的阶段。
* 跳级禁止：跳过 `evidence` 直接 `cross_review`，等于「没校验证据的结论参与冲突判定」。

### Finding

```
proposed ─┬─▶ needs_confirmation ─┬─▶ confirmed
          │                       ├─▶ rejected
          ├─▶ confirmed           └─▶ suppressed
          ├─▶ rejected
          └─▶ suppressed
```

终态不可回退；`confirmed` 必须有证据（软规则会报出来）。

### Dispute

```
open ─┬─▶ resolved
      └─▶ escalated ─▶ resolved
```

## 6. 硬不变量 vs 业务规则（为什么分两层）

| | 硬不变量 | 业务规则 |
| --- | --- | --- |
| 位置 | 对象 `__post_init__` | `policy.finding_violations` / `consistency_issues` |
| 违反时 | 抛 `InvariantViolation`，拒绝构造 | 返回违规清单，由调用方决定处置 |
| 例子 | 置信度 1.5、ID 非法、完成态没有结束时间 | blocker 置信度 0.6、finding 没挂证据 |
| 为什么 | 结构坏了，任何降级都是错的 | 历史数据/口径演进会产生「不合规但真实」的记录 |

如果把业务规则也做成异常，历史报告就再也读不进来——这是迁移中最典型的自伤。

## 7. 旧报告 → 领域对象

### 7.1 映射表

| 旧报告键 | 去向 |
| --- | --- |
| `review_id` | `Review.id` / `Run.review_id` / `Run.id`（+`-RUN01`） |
| `target_path` / `target_type` / `target_type_source` | `ReviewTarget` |
| `review_mode` | `ReviewRun.mode` |
| `integrity.*` | `IntegrityHashes` |
| `coverage.*` | `Coverage` + `ReviewTarget.encoding` |
| `stats` | `RunStats` |
| `issues[]` / `suppressed[]` | `Finding`（suppressed → `FindingStatus.SUPPRESSED`）+ 合成 `Evidence`（`finding_ids` 指回该条） |
| `disputes[]` | `Dispute`（`kind` 固定 `severity_conflict`，状态 `open`） |
| `verdict` / `core_conclusion` | `Decision`（reasons 由策略复算） |
| `timestamp` | `created_at` / `started_at` / `finished_at` |
| `agent` / `scenario` / `format_spec` | `ReviewProfile`（四者叠加的口径快照） |
| 其余全部键 | `AdaptedReview.extra`（原样，不改写） |

被消费的键清单是 `CONSUMED_KEYS`（16 个）。**新增消费一个键必须同时改适配器与测试**：
`checks/test_legacy_adapter.py::test_every_top_level_key_is_consumed_or_preserved` 会拦住漏项。

### 7.1.1 运行期反向：issue dict ⇄ Finding（阶段 1 收口）

上面是「历史报告 → 对象」的**读取**方向。运行期还需要**写回**方向：`review.py` 的
S6 出口把最终 issue 列表交给 `review_agent/adapters/issue_codec.py` 编成 `Finding`，
再由领域对象回写成报告里的 dict。三条约束：

| 约束 | 实现 | 验收 |
| --- | --- | --- |
| 表达力 | 领域装不下的键进 `EncodedIssue.extras`（`fix`/`consequence`/`aggregated_count`/`folded_into`…） | `checks/test_issue_codec.py` |
| 可逆性 | encode→decode 必须逐键相等；不等抛 `CodecMismatch` 并指出第一个不一致的键 | 全量历史语料（含 `.trash` 归档）逐条通过 |
| 字节稳定 | 标量类型原样保住（`1` 不变 `1.0`、`null` 不变 `false`、缺键不补键） | 同上；报告 `report_hash` 不因迁移漂移 |

管线内部（S3~S5）仍是 dict 工作格式——issue 在这些阶段被折叠、降权、补整改字段。
出口闸门保证「对外只有一份结论，且它由领域对象生成」，而不是把整条管线推倒重写。

### 7.2 已知差异（2026-10-30 复核，202 份历史报告）

| 现象 | 数量 | 处理 |
| --- | --- | --- |
| 无 `agent` 块（旧版本） | 87 | Profile 记为 `legacy-unversioned`，报告自身无法复现口径 |
| `verdict=pass` 但库外事实未核完 | 5 | 策略复算为 warn；登记为已知偏差（`R-20261028-045/046/047/055/071`） |
| blocker/major 且置信度 < 0.7 | 19 | 适配器补 `severity_note` + warning，不拒收 |
| 顶层键变体 | 12 种 | 适配器按缺省值处理；缺 `coverage` 时 `Coverage=None` |

这 5 份偏差不是「策略太严」：旧代码的裁定规则本身要求「库外未核完不得给 pass」，
只是早期版本没执行。保留差异清单的意义是——**它能证明新策略在历史数据上是可证伪的**。

## 8. 持久化（阶段 2 草案，未实现）

倾向 SQLite 起步（单机、零运维），表结构按对象一一对应：

```
reviews(id PK, target_path, target_sha256, profile_id, profile_version, status, created_at, ...)
runs(id PK, review_id FK, seq, status, stage, mode, profile_json, started_at, finished_at, ...)
findings(id, run_id FK, severity, confidence, status, rule_id, quote, location_json, ...)
evidence(id, run_id FK, kind, quote, source_ref, verification, ...)
disputes(id, run_id FK, status, point, issue_ids_json, resolution, ...)
decisions(id, run_id FK, verdict, risk_level, rationale, policy_id, approved_by, ...)
audit_events(seq, run_id FK, kind, name, status, ts, latency_ms, detail_json)
checkpoints(run_id FK, stage, sequence, state_hash, created_at, payload_json)
```

JSON 列只用来存「结构未定的明细」（`detail_json` / `profile_json`），
不把整个对象塞进一个 JSON 列——那样等于把 schema 问题推迟到查询时爆发。

## 9. 演进规则

* 枚举值改名 = 数据迁移 + ADR；不允许悄悄改。
* 新增字段必须给默认值，旧报告要能继续适配。
* 新增对象前先问：它是否只是某个对象的属性？六个对象的边界是「有自己的生命周期」，
  不是「有一个名字」。
* 任何模型改动都要跑：`python -m unittest discover -s checks`（含历史语料回归）。
