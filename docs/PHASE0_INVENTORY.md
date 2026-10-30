# Phase 0 盘点：review-agent 现状（自动生成）

> 生成时间：2026-10-30 13:18:45 +0800　生成命令：`python tools/arch_inventory.py`

> 本文件只做事实清点，不做评价。结论与取舍写在 [REFACTOR_PLAN.md](REFACTOR_PLAN.md)。

## 1. 规模

- Python 文件：**115** 个，有效代码行（非空行）：**34,714**
- 单文件最大：`review.py` **3,574** 行，占全部 Python 代码的 **10.3%**
- Architecture v1 新包：`review_agent/` **2,117** 行（占 6.1%，尚未接管旧流程）
- 解析失败：0 个

| 目录 | 职责 | Python 文件 | 代码行 |
| --- | --- | ---: | ---: |
| `tools` | 能力实现（规则、检索、LLM、格式、学习…），平铺无注册表 | 42 | 13,274 |
| `tests` | 全量回归与对抗测试（.gitignore 排除，仅本地） | 48 | 12,271 |
| `.` | 入口与主流程（review.py / server.py） | 2 | 5,675 |
| `review_agent` | Architecture v1（domain / runtime / capabilities），尚未接管旧流程 | 15 | 2,117 |
| `checks` | 公开不变量测试（CI 必跑） | 7 | 1,227 |
| `agents` | Agent manifest + 自带规则 / 检查器 | 1 | 150 |

## 2. 最大的 15 个文件

| 文件 | 代码行 | 被 import 次数 |
| --- | ---: | ---: |
| `review.py` | 3,574 | 25 |
| `tests/test_api_contract.py` | 2,379 | 3 |
| `server.py` | 2,101 | 7 |
| `tools/research.py` | 1,084 | 2 |
| `tools/fact_promote.py` | 1,005 | 4 |
| `tests/bruteforce_global.py` | 981 | 1 |
| `tools/rule_learning.py` | 951 | 12 |
| `tests/test_review.py` | 807 | 2 |
| `tools/purpose.py` | 650 | 6 |
| `tools/arith_check.py` | 621 | 7 |
| `tests/bruteforce_docx.py` | 555 | 1 |
| `review_agent/domain/models.py` | 530 | 3 |
| `tools/remediation.py` | 527 | 3 |
| `tests/bruteforce_purpose.py` | 517 | 0 |
| `tools/holdout_eval.py` | 516 | 1 |

## 3. 扇入 / 扇出最高的模块

扇入高 = 事实上的公共底座（动它要先有测试）；扇出高 = 协调者（可能是上帝对象）。

| 模块 | 扇入（被谁依赖） | 扇出（依赖谁） |
| --- | ---: | ---: |
| `review.py` | 25 | 24 |
| `tools/rules_store.py` | 15 | 0 |
| `tools/rule_learning.py` | 12 | 2 |
| `tools/regex_check.py` | 10 | 0 |
| `tools/arith_check.py` | 7 | 1 |
| `server.py` | 7 | 10 |
| `tools/purpose.py` | 6 | 2 |
| `tools/format_spec.py` | 6 | 0 |
| `tools/integrity.py` | 6 | 0 |
| `tools/llm_client.py` | 6 | 0 |
| `review_agent/domain/__init__.py` | 6 | 5 |
| `tools/match_matrix.py` | 5 | 1 |
| … | | |
| `review.py`（扇出 Top） | 25 | 24 |
| `tests/test_review.py`（扇出 Top） | 2 | 11 |
| `server.py`（扇出 Top） | 7 | 10 |
| `tests/bruteforce_global.py`（扇出 Top） | 1 | 7 |
| `tests/test_api_contract.py`（扇出 Top） | 3 | 7 |

## 4. 零扇入模块（待人工确认，不是死代码判决）

生产代码里共 **11** 个模块没有任何 Python 文件 import（另有 55 个测试文件本身就是入口，未列入）。判断口径：

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
| `tools/annotate.py` | 53 | 是 | 5 |
| `tools/log_summary.py` | 51 | 是 | 0 |
| `review_agent/domain/ports.py` | 48 | 否 | 12 |
| `review_agent/__init__.py` | 17 | 否 | 12 |

<details><summary>零扇入明细（引用来源）</summary>

- `tools/llm_bench.py` ← —
- `tools/arch_inventory.py` ← —
- `tools/law_ingest.py` ← `tests/test_law_library.py`、`tools/fact_gap.py`
- `tools/fact_audit.py` ← `tests/test_fact_gate.py`、`tools/fact_promote.py`
- `tools/rule_review.py` ← `server.py`、`tools/rule_learning.py`
- `tools/rule_miner.py` ← `tools/annotate.py`、`tools/rule_review.py`
- `tools/rule_promote.py` ← `server.py`、`tests/test_api_contract.py`、`tests/test_learning.py`、`tools/fact_promote.py`
- `tools/annotate.py` ← `tests/test_api_contract.py`、`tests/v7_promote_demo.py`、`tools/remediation.py`、`tools/rule_miner.py`
- `tools/log_summary.py` ← —
- `review_agent/domain/ports.py` ← `server.py`、`checks/test_architecture_guard.py`、`checks/test_legacy_adapter.py`、`tests/bruteforce_global.py`
- `review_agent/__init__.py` ← `review.py`、`checks/test_domain_v1.py`、`review_agent/capabilities/tools.py`、`review_agent/domain/errors.py`

</details>

## 5. 重名函数（≥3 个模块同名，重复实现信号）

| 函数名 | 出现模块数 | 模块 |
| --- | ---: | --- |
| `main` | 27 | `review.py`、`server.py`、`tools/annotate.py`、`tools/arch_inventory.py`、`tools/changelog_guard.py`、`tools/eval_review.py`… |
| `summarize` | 6 | `tools/eval_review.py`、`tools/holdout_eval.py`、`tools/log_summary.py`、`tools/match_matrix.py`、`tools/purpose.py`、`tools/rule_exec.py` |
| `get` | 4 | `review_agent/capabilities/tools.py`、`review_agent/domain/ports.py`、`tools/format_spec.py`、`tools/scenario.py` |
| `load_config` | 4 | `tools/llm_client.py`、`tools/match_matrix.py`、`tools/research.py`、`tools/rule_learning.py` |
| `_fmt` | 3 | `tools/arith_check.py`、`tools/remediation.py`、`tools/rule_learning.py` |
| `_rel` | 3 | `tools/fact_import.py`、`tools/holdout_eval.py`、`tools/rule_learning.py` |
| `add` | 3 | `review.py`、`tools/arch_inventory.py`、`tools/fact_promote.py` |
| `check` | 3 | `tools/changelog_guard.py`、`tools/law_cite.py`、`tools/law_index.py` |
| `cmd_list` | 3 | `tools/fact_promote.py`、`tools/rule_miner.py`、`tools/rule_review.py` |
| `cmd_show` | 3 | `tools/fact_promote.py`、`tools/rule_miner.py`、`tools/rule_review.py` |
| `load` | 3 | `tools/fact_gap.py`、`tools/format_spec.py`、`tools/scenario.py` |
| `run_all` | 3 | `tools/arith_check.py`、`tools/docx_format_checks.py`、`tools/logic_checks.py` |

> 同名不必然是重复代码（`main` / `load` / `_norm` 这类短名很常见）；这里是人工比对清单的入口，不是自动结论。

## 6. 测试与验收基线

- `tests/`：48 个文件，673 个 `test_*` 用例（.gitignore 排除，仅本地）
- `checks/`：7 个文件，109 个 `test_*` 用例（公开，CI 必跑）
- CI 实际命令（`.github/workflows/ci.yml`）：
  - `python -m unittest discover -s checks -v`
  - `python -m unittest discover -s tests -v`（tests/ 存在时）
  - `python tools/golden_eval.py`（金标准：召回与误报不许回退）
  - `python tools/changelog_guard.py --explain`（干净树必须通过）
- Python 版本：CI 3.12 / 3.13；本机 3.14.0

