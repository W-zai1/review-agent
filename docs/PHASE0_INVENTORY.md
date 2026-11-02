# Phase 0 盘点：review-agent 现状（自动生成）

> 生成时间：2026-11-01 17:05:43 +0800　生成命令：`python tools/arch_inventory.py`

> 本文件只做事实清点，不做评价。结论与取舍写在 [REFACTOR_PLAN.md](REFACTOR_PLAN.md)。

## 1. 规模

- Python 文件：**184** 个，有效代码行（非空行）：**50,326**
- 单文件最大：`review.py` **4,721** 行，占全部 Python 代码的 **9.4%**
- Architecture v1 新包：`review_agent/` **8,528** 行（占 16.9%，尚未接管旧流程）
- 解析失败：3 个（`checks/test_server_governance.py` SyntaxError: invalid non-printable character U+FEFF (test_server_governance.py, line 1)、`checks/test_server_tasks.py` SyntaxError: invalid non-printable character U+FEFF (test_server_tasks.py, line 1)、`checks/test_task_lifecycle.py` SyntaxError: invalid non-printable character U+FEFF (test_task_lifecycle.py, line 1)）

| 目录 | 职责 | Python 文件 | 代码行 |
| --- | --- | ---: | ---: |
| `tools` | 能力实现（规则、检索、LLM、格式、学习…），平铺无注册表 | 44 | 13,667 |
| `tests` | 全量回归与对抗测试（.gitignore 排除，仅本地） | 48 | 12,271 |
| `review_agent` | Architecture v1（domain / runtime / capabilities），尚未接管旧流程 | 45 | 8,528 |
| `checks` | 公开不变量测试（CI 必跑） | 44 | 8,476 |
| `.` | 入口与主流程（review.py / server.py） | 2 | 7,234 |
| `agents` | Agent manifest + 自带规则 / 检查器 | 1 | 150 |

## 2. 最大的 15 个文件

| 文件 | 代码行 | 被 import 次数 |
| --- | ---: | ---: |
| `review.py` | 4,721 | 47 |
| `server.py` | 2,513 | 9 |
| `tests/test_api_contract.py` | 2,379 | 3 |
| `tools/research.py` | 1,133 | 3 |
| `tools/fact_promote.py` | 1,005 | 4 |
| `tests/bruteforce_global.py` | 981 | 1 |
| `tools/rule_learning.py` | 951 | 12 |
| `tests/test_review.py` | 807 | 2 |
| `checks/test_research_v1.py` | 698 | 0 |
| `review_agent/application/tasks.py` | 692 | 1 |
| `review_agent/domain/models.py` | 688 | 3 |
| `tools/purpose.py` | 650 | 6 |
| `checks/test_builtin_tools.py` | 634 | 0 |
| `tools/arith_check.py` | 621 | 7 |
| `review_agent/capabilities/builtin_tools.py` | 592 | 1 |

## 3. 扇入 / 扇出最高的模块

扇入高 = 事实上的公共底座（动它要先有测试）；扇出高 = 协调者（可能是上帝对象）。

| 模块 | 扇入（被谁依赖） | 扇出（依赖谁） |
| --- | ---: | ---: |
| `review_agent/domain/__init__.py` | 50 | 6 |
| `review.py` | 47 | 41 |
| `review_agent/runtime/__init__.py` | 20 | 4 |
| `review_agent/domain/ids.py` | 17 | 1 |
| `review_agent/runtime/nodes/__init__.py` | 15 | 9 |
| `tools/rules_store.py` | 15 | 0 |
| `review_agent/runtime/node.py` | 13 | 2 |
| `tools/rule_learning.py` | 12 | 2 |
| `tools/regex_check.py` | 10 | 0 |
| `review_agent/application/__init__.py` | 10 | 5 |
| `server.py` | 9 | 14 |
| `tools/integrity.py` | 8 | 2 |
| … | | |
| `review.py`（扇出 Top） | 47 | 41 |
| `server.py`（扇出 Top） | 9 | 14 |
| `tests/test_review.py`（扇出 Top） | 2 | 11 |
| `checks/test_research_v1.py`（扇出 Top） | 0 | 10 |
| `review_agent/runtime/nodes/__init__.py`（扇出 Top） | 15 | 9 |

## 4. 零扇入模块（待人工确认，不是死代码判决）

生产代码里共 **10** 个模块没有任何 Python 文件 import（另有 92 个测试文件本身就是入口，未列入）。判断口径：

- `__main__` 存在且被文档 / .bat 调用 → **CLI 入口，合理**
- 只被测试或文档引用 → **测试夹具 / 待接线**
- 既无入口也无引用 → **疑似遗留，阶段 3 清理前需人工确认**

| 模块 | 行数 | 有 `__main__` | 被其他文件文本引用 |
| --- | ---: | :---: | ---: |
| `tools/llm_bench.py` | 377 | 是 | 0 |
| `tools/arch_inventory.py` | 284 | 是 | 0 |
| `tools/law_ingest.py` | 186 | 是 | 2 |
| `tools/fact_audit.py` | 175 | 是 | 2 |
| `tools/rule_review.py` | 160 | 是 | 2 |
| `tools/rule_miner.py` | 128 | 是 | 2 |
| `tools/rule_promote.py` | 125 | 是 | 6 |
| `tools/annotate.py` | 53 | 是 | 6 |
| `tools/log_summary.py` | 51 | 是 | 0 |
| `review_agent/__init__.py` | 17 | 否 | 38 |

<details><summary>零扇入明细（引用来源）</summary>

- `tools/llm_bench.py` ← —
- `tools/arch_inventory.py` ← —
- `tools/law_ingest.py` ← `tests/test_law_library.py`、`tools/fact_gap.py`
- `tools/fact_audit.py` ← `tests/test_fact_gate.py`、`tools/fact_promote.py`
- `tools/rule_review.py` ← `server.py`、`tools/rule_learning.py`
- `tools/rule_miner.py` ← `tools/annotate.py`、`tools/rule_review.py`
- `tools/rule_promote.py` ← `server.py`、`tests/test_api_contract.py`、`tests/test_learning.py`、`tools/fact_promote.py`
- `tools/annotate.py` ← `tests/test_api_contract.py`、`tests/v7_promote_demo.py`、`tools/dup_audit.py`、`tools/remediation.py`
- `tools/log_summary.py` ← —
- `review_agent/__init__.py` ← `review.py`、`checks/test_builtin_tools.py`、`checks/test_domain_v1.py`、`checks/test_failure_injection.py`

</details>

## 5. 重名函数（≥3 个模块同名，重复实现信号）

| 函数名 | 出现模块数 | 模块 |
| --- | ---: | --- |
| `main` | 30 | `review.py`、`review_agent/infrastructure/retention.py`、`server.py`、`tools/annotate.py`、`tools/arch_inventory.py`、`tools/changelog_guard.py`… |
| `summarize` | 7 | `tools/double_run.py`、`tools/eval_review.py`、`tools/holdout_eval.py`、`tools/log_summary.py`、`tools/match_matrix.py`、`tools/purpose.py`… |
| `as_dict` | 5 | `review_agent/application/reports.py`、`review_agent/domain/errors.py`、`review_agent/infrastructure/retention.py`、`review_agent/runtime/nodes/research.py`、`review_agent/runtime/orchestrator.py` |
| `get` | 5 | `review_agent/application/tasks.py`、`review_agent/capabilities/tools.py`、`review_agent/domain/ports.py`、`tools/format_spec.py`、`tools/scenario.py` |
| `load_config` | 5 | `review_agent/infrastructure/retention.py`、`tools/llm_client.py`、`tools/match_matrix.py`、`tools/research.py`、`tools/rule_learning.py` |
| `ok` | 5 | `review_agent/runtime/node.py`、`review_agent/runtime/nodes/rules.py`、`review_agent/runtime/nodes/semantic.py`、`review_agent/runtime/orchestrator.py`、`tools/docx_format_checks.py` |
| `_rel` | 4 | `review_agent/application/tasks.py`、`tools/fact_import.py`、`tools/holdout_eval.py`、`tools/rule_learning.py` |
| `add` | 4 | `review.py`、`review_agent/application/audit.py`、`tools/arch_inventory.py`、`tools/fact_promote.py` |
| `check` | 4 | `tools/changelog_guard.py`、`tools/double_run.py`、`tools/law_cite.py`、`tools/law_index.py` |
| `label` | 4 | `review_agent/domain/enums.py`、`review_agent/domain/models.py`、`review_agent/runtime/nodes/cross_review.py`、`tools/agent_spec.py` |
| `load` | 4 | `review_agent/runtime/serialization.py`、`tools/fact_gap.py`、`tools/format_spec.py`、`tools/scenario.py` |
| `_fmt` | 3 | `tools/arith_check.py`、`tools/remediation.py`、`tools/rule_learning.py` |
| `_path` | 3 | `review_agent/application/tasks.py`、`review_agent/runtime/orchestrator.py`、`review_agent/runtime/serialization.py` |
| `_read_json` | 3 | `review_agent/application/tasks.py`、`server.py`、`tools/fact_promote.py` |
| `apply` | 3 | `review_agent/infrastructure/retention.py`、`tools/format_spec.py`、`tools/scenario.py` |
| `chat` | 3 | `review_agent/adapters/model.py`、`review_agent/domain/ports.py`、`tools/llm_client.py` |
| `cmd_list` | 3 | `tools/fact_promote.py`、`tools/rule_miner.py`、`tools/rule_review.py` |
| `cmd_show` | 3 | `tools/fact_promote.py`、`tools/rule_miner.py`、`tools/rule_review.py` |
| `now_iso` | 3 | `review.py`、`review_agent/infrastructure/ledger.py`、`tools/rule_learning.py` |
| `run_all` | 3 | `tools/arith_check.py`、`tools/docx_format_checks.py`、`tools/logic_checks.py` |
| `save` | 3 | `review_agent/application/tasks.py`、`review_agent/domain/ports.py`、`review_agent/runtime/serialization.py` |
| `scan` | 3 | `review_agent/infrastructure/ledger.py`、`tools/dup_audit.py`、`tools/pattern_rules.py` |
| `stats` | 3 | `review_agent/adapters/legacy_report.py`、`review_agent/runtime/nodes/rules.py`、`tools/fact_gap.py` |
| `total` | 3 | `review_agent/application/tasks.py`、`review_agent/domain/models.py`、`review_agent/infrastructure/retention.py` |
| `verify` | 3 | `review_agent/application/reports.py`、`review_agent/infrastructure/ledger.py`、`tools/research.py` |

> 同名不必然是重复代码（`main` / `load` / `_norm` 这类短名很常见）；这里是人工比对清单的入口，不是自动结论。

## 6. 测试与验收基线

- `tests/`：48 个文件，673 个 `test_*` 用例（.gitignore 排除，仅本地）
- `checks/`：44 个文件，680 个 `test_*` 用例（公开，CI 必跑）
- CI 实际命令（`.github/workflows/ci.yml`）：
  - `python -m unittest discover -s checks -v`
  - `python -m unittest discover -s tests -v`（tests/ 存在时）
  - `python tools/golden_eval.py`（金标准：召回与误报不许回退）
  - `python tools/changelog_guard.py --explain`（干净树必须通过）
- Python 版本：CI 3.12 / 3.13；本机 3.14.0

