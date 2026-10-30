# 编码规范 v1

> 适用于所有人（包括 AI Coding Agent）对 review-agent 的修改。
> 目的不是统一风格，而是**防止每次改动都长出一套新设计**。

## 1. 开工前必须做的三件事

1. 读文档：本目录 `ARCHITECTURE.md`、`DOMAIN_MODEL.md`、`AGENT_RUNTIME.md`、
   `TOOL_SPECIFICATION.md`，以及与本任务相关的既有测试。
2. 回答三个问题（写进改动说明）：
   * 这次改动属于哪一层？（domain / runtime / capabilities / application / 旧流程）
   * 这个能力是不是已经存在？（先搜 `tools/` 与 `review.py`，再决定新增）
   * 会影响哪些测试、哪些报告字段、哪些历史数据？
3. 跑一遍基线：`python -m unittest discover -s checks`。

回答不了第一个问题，说明改动不该开始；回答不了第二个，大概率要重复造轮子。

## 2. 禁止清单

| 禁止 | 具体含义 | 本项目里的反例 |
| --- | --- | --- |
| 擅自增加架构层 | 不新增 `xxx_engine/`、`pipeline/` 之类平行结构 | 历史上同时存在 `review.py` 主流程 + `_h_*` 分发 + `agents/` manifest 三套「谁来跑」 |
| 重复实现已有工具 | 新写函数前先搜同名/同语义实现 | 同名 `check` / `load` / `run_all` 散落在多个模块（见盘点 §5） |
| 改未授权的领域模型 | 改 `review_agent/domain` 的字段、枚举、状态机必须单独评审 | 直接给 `Finding` 加字段并在适配器里硬塞 |
| 删除未确认的旧功能 | 旧代码只能标记 deprecated，不能顺手删 | `tools/fact_promote.py` 仍被 `server.py` 引用 |
| 用 Mock 代替真实集成测试 | 单元测试可 mock 外部网络；不允许 mock 掉被验证的业务链路 | 用假 report 验证「报告能生成」，却没跑过一次真实 `run_one` |
| 静默降级/静默跳过 | 少跑检查、解析失败、字段缺失必须写进 limitations 或抛错 | 规则 YAML 结构不对时 `continue`（已被 `rules_store` 修掉） |

## 3. 代码规范

* **Python 3.12 语法**：CI 跑 3.12 / 3.13。可以用 `match`、`StrEnum`、`X | None`；
  不要用 3.13+ 专属特性。本机 3.14 只用于开发。
* **UTF-8、无 BOM**。报告、规则、文档都是 UTF-8；读文件一律显式 `encoding="utf-8"`
  （版本号文件用 `utf-8-sig`，见 `integrity.rules_version` 的注释）。
* **类型注解**：新增/修改的函数必须有参数与返回值注解；`from __future__ import annotations`。
* **不可变优先**：领域对象 `frozen=True`；状态变化返回新对象。可变全局状态只允许出现在
  `review.py` 的兼容层，并且必须写明为什么。
* **异常**：领域层用 `DomainError` 家族；不要 `except Exception: pass`。
  确实需要兜底的地方（第三方解析器）必须注释说明「为什么不能向上抛」。
* **错误信息**：必须能定位——带文件/字段/取值。禁止 `raise ValueError("invalid")`。
* **随机与时间**：领域层不直接取当前时间（通过 `Clock` 端口）；排序必须稳定。
* **路径**：统一 `pathlib.Path`；跨平台路径比较用 `as_posix()`，不要拼反斜杠。
* **日志**：结构化字段（`review_id` / `run_id` / `stage` / `tool`），不要只打一句中文。
* **中文注释**：解释「为什么」，不复述「做了什么」；与现有代码风格一致。

## 4. 测试规范

| 目录 | 定位 | 是否进 CI | 要求 |
| --- | --- | --- | --- |
| `checks/` | 可公开的最小不变量 | 是 | 不依赖 `outputs/`、不联网、< 2s |
| `tests/` | 全量回归、对抗、暴力测试 | 本地（`tests/` 被 .gitignore 排除） | 可读真实语料，允许慢 |

* 新增行为必须同时加 `checks/` 用例；只加 `tests/` 等于「远程仓库看不到」。
* 修 bug 先写失败用例，再修；用例名写清「什么输入 → 什么结论」。
* 依赖历史语料的测试用 `skipUnless` 守卫，CI 缺语料时跳过而不是报错。
* 不允许为了让测试过而放宽断言；确属口径变化的，改断言时必须写明原因。
* 合并前必跑：`python -m unittest discover -s checks`、`python tools/golden_eval.py`。
  改了 `CHANGELOG.md` / 版本相关文件，再跑 `python tools/changelog_guard.py --explain`。

## 5. 变更流程

1. **单主题**：一次改动只做一件事；重构与功能不要混在一个提交里。
2. **小步**：每次提交可独立回滚；大改先落文档与测试，再动实现。
3. **版本号四处**（按需）：
   * `VERSION`：报告模型/产品版本（`REPORT_MODEL_VERSION` 来源）
   * `config/rules_version.txt`：规则集变更
   * `config/knowledge_version.txt`：知识库变更
   * `agents/<id>/manifest.json` 的 `version`：判定标准变更
   改了内容却不升版本，历史报告会被判成「疑似篡改」（`integrity.verify_report`）。
4. **CHANGELOG**：写清「改了什么、为什么、影响哪些报告/规则」。
5. **改动说明模板**：

```text
改了什么：
影响的层与模块：
数据/报告兼容性：
验证命令与结果：
未做/已知风险：
```

## 6. 架构守护

依赖方向由测试强制（`checks/test_architecture_guard.py`）：

* `review_agent/domain` 不得 import `runtime` / `capabilities` / 第三方库
* `review_agent/capabilities` 不得 import `runtime`
* `review_agent/runtime` 不得 import 具体工具实现

自查：

```powershell
python -m unittest checks.test_architecture_guard -v
python tools/arch_inventory.py   # 看扇入/扇出与重名函数有没有恶化
```

`arch_inventory.py` 的输出是「事实清单」不是评分：新增扇入 > 10 的模块，
要么是真正的公共底座，要么是新的上帝对象，必须在改动说明里解释。

## 7. AI Coding Agent 工作协议

1. 先读 §1 的文档，再读要改的文件（不许凭猜测改）。
2. 输出计划：改哪几个文件、每个文件为什么、怎么验证；计划外的新增文件要说明理由。
3. 修改时保持旧行为：旧流程可运行是硬约束；新能力默认旁路，不默认接管。
4. 完成后报告：改了哪些文件、跑了哪些命令、输出是什么、有哪些未完成。
5. 不许用「应该可以」「理论上」代替验证结果；没跑就写没跑。
6. 遇到与文档冲突的现状，先报告冲突，不要自行选一边。

## 8. 评审清单

- [ ] 改动属于某一层，没有新增平行结构
- [ ] 没有重复实现已有工具/函数
- [ ] 领域模型改动有 ADR 或至少一段理由
- [ ] 旧功能没有被顺手删除
- [ ] 新增行为有 `checks/` 用例，且 CI 不需要新依赖
- [ ] 报告字段/历史数据兼容性已说明
- [ ] 版本号与 CHANGELOG 已按需更新
- [ ] 验证命令与真实输出贴在改动说明里
