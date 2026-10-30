# docs/ 索引

这套文档是 Architecture v1 的**约束**，不是介绍材料。修改代码前按顺序读，
改动完成后按 [CODING_STANDARDS.md](CODING_STANDARDS.md) §5 写改动说明。

| 文档 | 回答什么问题 | 谁必须读 |
| --- | --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 分几层、依赖方向、为什么这样选、旧代码边界在哪 | 所有人 |
| [DOMAIN_MODEL.md](DOMAIN_MODEL.md) | 六个对象与值对象、状态机、旧报告怎么映射 | 改模型/报告/裁定的人 |
| [AGENT_RUNTIME.md](AGENT_RUNTIME.md) | 节点清单、八阶段、失败与恢复语义 | 改流程/加节点的人 |
| [TOOL_SPECIFICATION.md](TOOL_SPECIFICATION.md) | 工具权限、执行管线、不可信输入、迁移批次 | 加工具/迁 `tools/` 的人 |
| [CODING_STANDARDS.md](CODING_STANDARDS.md) | 禁止清单、测试要求、版本号、AI 工作协议 | 所有人（含 AI） |
| [REFACTOR_PLAN.md](REFACTOR_PLAN.md) | 阶段、验收、风险、下一步做什么 | 排期与验收的人 |
| [PHASE0_INVENTORY.md](PHASE0_INVENTORY.md) | 仓库事实（自动生成，勿手改） | 需要数据时查 |

## 与既有文档的关系

* `review-agent-framework.md`（仓库根）：v0.2 的**产品定义**（身份、五原则、S1~S6 SOP），
  仍然有效；其中「多 Agent 对抗」的表述以 [AGENT_RUNTIME.md](AGENT_RUNTIME.md) 为准。
* `docs/多Agent框架.md`：历史设计稿，保留作背景，不再作为实现依据。
* `docs/系统诊断报告·2026-10-29.md`：现状诊断，与本套文档不冲突。
  该文含内部缺陷清单，按 `.gitignore` 的「测试/漏洞报告不公开」规则**只留本地、不入公开仓库**；
  公开仓库里没有这个文件属正常，不要当成链接失效去「修」。

## 维护规则

* 文档与代码冲突时，**以代码 + 测试为准**，并立刻修文档或修代码，不允许长期并存。
* 改了枚举值、报告字段、依赖方向、阶段划分，必须同步改这里对应的文档。
* 本目录不放「计划中的功能清单」——只在 [REFACTOR_PLAN.md](REFACTOR_PLAN.md) 里写阶段与验收。
