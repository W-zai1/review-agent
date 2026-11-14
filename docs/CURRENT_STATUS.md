# 当前状态（CURRENT_STATUS）

> 本文件是**项目当前状态的唯一来源**：版本、正式执行链、已完成能力、已知问题、
> 下一阶段任务。每次阶段整改结束，先改这里，再改 `CHANGELOG.md`——不允许只更新流水。
>
> 文中每条结论都尽量带 `file:line` 或命令；与代码冲突时以代码 + 测试为准，
> 并立刻回来改本文件。
>
> **最后核对：2026-11-14**（Python 3.14.0 / Windows 11 10.0.26100 / 本轮基线 `db38d1a`）

## 1. 版本与运行环境

| 项 | 当前值 | 证据 |
|---|---|---|
| 版本 | **0.9.2** | `VERSION` |
| 运行环境 | CPython 3.12+；CI 跑 3.12 / 3.13；本机实测 3.14.0 | `.github/workflows/ci.yml` 矩阵；`python -V` |
| 硬依赖 | 仅 `pyyaml>=6.0,<7`（PDF / Word / OCR 走标准库或可选适配器，缺库时降级） | `requirements.txt` |
| 平台 | Windows 为主（OCR / `.bat` / 路径），CI 用 `windows-latest` | `.github/workflows/ci.yml` |

## 2. 正式执行链（唯一）

**V1 是正式执行链，默认启用；Legacy 冻结为回滚 / 差异验证路径。**

| 入口 | 命令 | 说明 |
|---|---|---|
| CLI | `python review.py <target> [--engine v1\|legacy] [--agent <name>] [--no-llm]` | 缺省 `--engine` 取环境变量 `REVIEW_ENGINE`，再缺省 **v1**（`review.py:4245-4255`；`:4241` 明写「正式执行链路是 v1（--engine v1，默认）」） |
| CLI 任务控制 | `python review.py --task-status R-...` / `--task-cancel R-... [--task-reason 文案]` / `--task-recover` / `--task-resume R-...` | 与 Web 共用 `state/tasks/`；取消可跨进程协作；恢复体检列出可续任务；续跑按 schema v2 执行清单恢复（4.2-A/B/C） |
| Web 控制台 | `python server.py [--port 8000]` → `http://127.0.0.1:8000` | 零依赖，仅标准库（`server.py` 文件头与 `main()`） |
| 控制台快捷方式 | `start-console.bat` | 调 `server.py` |

- Server 侧同步审查适配层缺省 `engine="v1"`（`server.py:1674`）；注释 `:1688` 记录了
  「任务记录写 v1、实际跑 legacy」这个已修过的坑。
- `--engine shadow` 仍在 CLI 解析器里，用于差异对照，不是正式路径。
- 报告落盘：v1 写 `outputs/<R-id>.json`，`schema_version = "1.0-v1-shadow"`、
  `engine = "architecture-v1"`（`review_agent/application/shadow_report.py:32-33`）；
  Legacy 报告 `schema_version = "1.2"`，引擎键不同，读取方按 `engine` 分流
  （`review_agent/application/reports.py:68`、`tasks.py:146`、`report_store.py:138`）。

## 3. 能力现状

| 能力 | 状态 | 证据 |
|---|---|---|
| 审查 Agent | 两套：`doc-review 1.4.0`（34 规则 / 5 检查器 / 自动路由）、`security-audit 1.0.1`（18 规则 / 0 检查器 / 需 `--agent` 显式） | `python review.py --list-agents` |
| 审查流水线 | V1 八阶段（S1–S6 + 证据评估 / 关系图 / 编排器 + 检查点） | `docs/AGENT_RUNTIME.md`；`review_agent/runtime/` |
| 工具层 | 工具注册表 + 权限判定；`tools/*` 为可复用实现，`review_agent/capabilities/` 为注册表包装 | `review_agent/capabilities/builtin_tools.py`；`docs/TOOL_SPECIFICATION.md` |
| 任务服务 | `TaskService` 已有 start/wait/cancel/recover/resume 等完整接口；task schema v2 保存执行清单，兼容读取 v1 | `review_agent/application/tasks.py` |
| 报告完整性 | `integrity.verify_report` 可复算 report_hash；ledger 哈希链 | `python tools/integrity.py verify`（290 条记录，含 290 条墓碑，链完整） |
| 发布前检查 | 工作区干净 + 暂存区快照 + 顶层白名单 + 禁止文件 + 密钥扫描 + `checks/` | `tools/release_check.py`；`docs/RELEASE_CHECKLIST.md` |
| 知识学习 / 规则晋升 | 保持人工门禁，不自动晋升 | 决策书 4.9；`docs/自学习闭环.md` |

## 4. 已完成的整改（截至 2026-11-12）

| 来源 | 内容 | 验证 |
|---|---|---|
| 决策书 **4.8** | 外部 linter 降级与密钥扫描边界：`lint_status` / `secret_status` 双轴，补齐权限 / 其他 OSError / 非零退出码，密钥扫描独立执行 | `checks/test_lint.py` 13 项；全量 `checks` 921 OK；临时副本 4 组 E2E |
| 决策书 **4.3** | 发布包白名单与运行时数据边界：`RELEASE_TOP_LEVEL` + `_FORBIDDEN_IN_PACKAGE` + 快照清单；历史凭据核查结论 | `checks/test_release_check.py` 14 项；克隆内 4 组反例 + 空目录解压 |
| 决策书 **4.2** | 任务生命周期统一：`TaskService` 为唯一业务事实源；CLI/Web 新任务保存 schema v2 执行清单；CLI 支持状态、跨进程取消、恢复体检和自动续跑；阶段状态先于检查点持久化 | `test_task_acceptance` 13 + `test_cli_task_control` 9 + `test_application_service` 10 项；续跑端到端 2 项 |
| 决策书 **4.6** | 外发审计前置，两条路径全覆盖：① Research 先写 `egress_attempt`、写不进不发、结束写 `egress_result`；② LLM 在唯一出口 `tools/llm_client.chat()` 上做总闸口（contextvar 决定归属，attempt → 调用 → result 配对，写不进抛 `LLMError` 不调用）；③ server 起 `review.py --llm` 子进程前登记，失败直接落 FAILED 不 `Popen`。日志只记元信息 | `checks/test_research_egress.py` 8 项；`checks/test_llm_egress.py` 13 项；临时副本 E2E：本地假模型 5 次调用 → 5 对 attempt/result（review_id/run_id 齐全、无原文）；审计不可写 → 0 次 HTTP、报告写明阻断 |
| 决策书 **4.4** | 反证接入（A 档）：`EvidenceAssessorNode` 评估前先 `link_contradictions()`——被外部检索反驳的主张，其原文引用证据标 `CONTRADICTED` 并用 `contradicted_by` 指名外部证据；未被反驳的引用仍按原判定表评 | `checks/test_evidence_contradiction.py` 11 项 + `test_research_v1.py` 报告层 1 项；证据相关 111 项；全量 `checks` 966 OK |
| 第二阶段 2.1–2.16 | 应用层 / 领域层 / 基础设施层拆分（`server.py` → 1849 行、`review.py` → 约 5000 行） | `docs/第二阶段整改任务单.md`；`tests` 686 OK |
| 阶段三（第二刀） | 密钥正则收敛到 `tools/secret_patterns.py` | `checks/test_secret_patterns.py`；`tools/secret_scan.py` 0 命中 |

## 5. 已知问题（与决策书条目对应）

| 条目 | 现状 | 影响 |
|---|---|---|
| 4.1 双引擎 | v1 已是默认，但 Legacy 仍可运行、两套报告 schema 并存 | 新功能只进 v1；legacy 仅回滚/对照 |
| 4.2 任务生命周期 | 已统一：CLI 已有状态、跨进程取消、恢复体检和自动续跑；task schema v2 保存执行清单并兼容 v1；同一 review_id 的续跑由跨进程租约互斥。剩余：续跑节点日志会覆盖同 review_id 的上一轮日志 | 并发续跑已拒绝，历史节点日志保留待增强 |
| 4.4 证据可信度 | 已整改反证链路：被外部检索反驳的主张，其引用证据落 `CONTRADICTED` 并指名外部证据（不再是 VERIFIED）。剩余：supported 主张不产证据（样本②只能从 `research.supported` 段看出）、文本存在性仍与事实成立同轴（B 档未做） | 反证可见；「文档写过」与「事实成立」仍靠强度语义区分 |
| 4.5 工具与能力层 | `tools/*` 与注册表工具两套并存，部分能力只在一边 | 行为分叉风险 |
| 4.6 外发审计 | 已整改：Research 与 LLM 都是「先登记、写不进不发、结果配对」；LLM 的记录点在 `tools/llm_client.chat()`，CLI 在 `interfaces/cli/commands.py::_run_review` 设上下文、server 在 `server.py::_run_review_job` 起进程前登记。剩余：`state/egress.log` 没有轮转/容量策略 | 外发可追溯；日志长期增长需运维关注 |
| 4.7 大文件 | `review.py` 约 5000 行、`server.py` 1849 行 | 改动成本与冲突面 |
| 4.8 Linter | 已整改；遗留：`static_scan` 无 try/except 兜底、`TextLintTool` 不在默认规则链 | 已知，待阶段 3 决策 |
| 4.9 / 4.10 / 4.12 | 知识学习门禁、人工复核闭环、质量评测口径 | 阶段 4 |

## 6. 测试与质量基线（2026-11-12 实测）

| 检查 | 命令 | 结果 |
|---|---|---|
| 公开不变量 | `python -m unittest discover -s checks` | **989 项 OK（skipped=11，约 27s）** |
| 私有回归 | `python -m unittest discover -s tests` | **686 项 OK（skipped=1，约 453s）** |
| 密钥扫描 | `python tools/secret_scan.py` | 265 文件 / 命中 0 |
| 账本守门 | `python tools/changelog_guard.py` | 退出 0 |
| 发布检查 | `python tools/release_check.py --no-tests` | 快照 258 文件 / 3,766,759 字节 / 顶层 24 项 → 通过 |
| 账本完整性 | `python tools/integrity.py verify` | 290 条记录（含 290 墓碑），链完整 |

> 数字会随改动漂移；复跑上表命令即为当前值，不要凭本文件的历史数字下结论。

## 7. 下一阶段任务（按决策书路线图）

1. **阶段 1（消除双重事实来源）**：4.1 固定 V1 为唯一新功能入口、Legacy 冻结；
   4.2 任务生命周期闭环已完成：schema v2 执行清单、阶段状态持久化、CLI 自动续跑。
   后续增强：历史节点日志合并。
2. **阶段 2（证据与外发）**：4.6 外发审计已完成（Research + LLM 两条路径同一约束：
   先登记、写不进不发、结果配对）；4.4 反证链路已接通（`CONTRADICTED` 可达），
   剩余「文本存在性 vs 事实核验」分轴（B 档，需决策是否把锚定降为 PARTIAL）。
3. **阶段 3（边界收敛）**：4.5 工具与能力层收敛（先改调用方，再删旧实现）；
   4.7 `review.py` / `server.py` 按模块拆分，避免大爆炸式重构。
4. **阶段 4（质量闭环）**：4.9 知识学习人工门禁、4.10 人工复核闭环、4.12 质量评测口径。

阶段 0 的 6 项（凭据核查、历史扫描、基线固化、发布白名单、Linter 降级、
本文件）**已全部完成**。

## 8. 数据与发布边界

| 目录 | 内容 | 是否进发布包 |
|---|---|---|
| `outputs/` `logs/` `state/` `ledger/` `targets/` `cache/` | 报告、日志、运行状态、审计账本、待审对象、缓存 | **否**（不在 `RELEASE_TOP_LEVEL`，也不被 Git 跟踪） |
| `config/llm.local.json` | 本地 LLM 配置（可能含 Key） | **否**（禁止文件规则拦截） |
| `ledger/` | 审计记录，**永不随清理删除** | 否（但必须保留在运行环境） |

发布流程：`docs/RELEASE_CHECKLIST.md`（凭据核查 → 暂存区快照检查 → `git archive` → 空目录复跑）。

## 9. 文档地图

| 文档 | 回答什么 |
|---|---|
| `README.md` | 产品定位、安装、运行、演示 |
| `docs/ARCHITECTURE.md` | 分层与依赖方向 |
| `docs/AGENT_RUNTIME.md` | 节点、八阶段、失败与恢复语义 |
| `docs/TOOL_SPECIFICATION.md` | 工具契约与权限 |
| **`docs/CURRENT_STATUS.md`**（本文件） | **当前版本 / 执行链 / 能力 / 已知问题 / 下一阶段** |
| `docs/RELEASE_CHECKLIST.md` | 发布前检查与打包边界 |
| `CHANGELOG.md` | 历史变更流水（逐条） |

## 10. 如何复核本文件

```bash
python -V && type VERSION
python review.py --list-agents
python -m unittest discover -s checks
python tools/secret_scan.py
python tools/integrity.py verify
python tools/release_check.py --no-tests
```

任何一条与本文件描述不符，先改本文件，再决定是否改代码。
