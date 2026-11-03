#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 命令层：解析交给 parser，这里只做审查执行与错误映射。

从 `review.py::main()` 迁出（第二阶段 2.1a），2.1b 把参数解析拆到 parser.py。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv=None, *, runtime):
    from interfaces.cli.parser import parse_args
    opts = parse_args(argv, runtime=runtime)
    if opts is None:
        return
    return _run_review(opts, runtime)


def _run_review(opts, runtime):
    target_path = ((runtime.ROOT / opts.target_arg)
                   if not os.path.isabs(opts.target_arg) else Path(opts.target_arg))
    if not target_path.exists():
        print(f"找不到待审文件：{target_path}")
        runtime._emit_domain_error(runtime.NotFoundError(f"找不到待审文件：{target_path}"))
        sys.exit(1)
    rules = runtime.load_rules()
    if opts.engine == "shadow" and opts.use_llm is not False:
        # 双跑比对不能拿模型输出比：语义审本身有波动，差异无从判定是 bug 还是随机。
        # 显式拒绝，比产出一堆「未解释差异」强。
        print("--engine shadow 目前只支持纯规则模式：请加 --no-llm")
        runtime._emit_error("--engine shadow 需要 --no-llm", kind="input",
                            http_status=400)
        sys.exit(2)
    if opts.engine == "v1":
        if opts.review_id is not None:
            # 服务端子进程：父进程已建任务记录（含幂等键），这里只执行、不再建任务。
            try:
                runtime._run_v1(
                    target_path, rules,
                    type_override=opts.type_override, agent_id=opts.agent_id,
                    use_llm=opts.use_llm, purpose=opts.purpose,
                    purpose_max=opts.purpose_max, review_id=opts.review_id,
                    research_enabled=opts.research_enabled,
                    purpose_notes=opts.purpose_notes, scenario=opts.scenario,
                    format_spec=opts.format_spec,
                    progress_path=opts.progress_path)
            except runtime.scenario_mod.ScenarioError as e:
                print(f"审查情境错误：{e}")
                runtime._emit_domain_error(runtime.ConfigError(f"审查情境错误：{e}"))
                sys.exit(2)
            except runtime.format_spec_mod.FormatSpecError as e:
                print(f"格式规格错误：{e}")
                runtime._emit_domain_error(runtime.ConfigError(f"格式规格错误：{e}"))
                sys.exit(2)
            return
        # 手工 CLI：任务生命周期走 TaskService（与 Web 同一条链）
        try:
            runtime._run_v1_via_task(
                target_path, rules,
                type_override=opts.type_override, agent_id=opts.agent_id,
                use_llm=opts.use_llm, purpose=opts.purpose,
                purpose_max=opts.purpose_max, purpose_notes=opts.purpose_notes,
                scenario=opts.scenario, format_spec=opts.format_spec,
                research_enabled=opts.research_enabled,
                progress_path=opts.progress_path)
        except runtime.scenario_mod.ScenarioError as e:
            print(f"审查情境错误：{e}")
            runtime._emit_domain_error(runtime.ConfigError(f"审查情境错误：{e}"))
            sys.exit(2)
        except runtime.format_spec_mod.FormatSpecError as e:
            print(f"格式规格错误：{e}")
            runtime._emit_domain_error(runtime.ConfigError(f"格式规格错误：{e}"))
            sys.exit(2)
        except runtime.ConfigError as e:
            print(f"[v1] {e}")
            runtime._emit_domain_error(e)
            sys.exit(2)
        except runtime.UnreadableTargetError as e:
            print(f"目标不可读：{e}")
            runtime._emit_domain_error(e)
            sys.exit(1)
        return
    try:
        report = runtime.run_one(
            target_path, rules, use_llm=opts.use_llm,
            progress_path=opts.progress_path, type_override=opts.type_override,
            agent_id=opts.agent_id, purpose=opts.purpose,
            purpose_max=opts.purpose_max, purpose_notes=opts.purpose_notes,
            scenario=opts.scenario, format_spec=opts.format_spec,
            research_enabled=opts.research_enabled, review_id=opts.review_id)
    except runtime.agent_spec.AgentError as e:
        print(f"Agent 配置错误：{e}")
        runtime._emit_domain_error(runtime.ConfigError(f"Agent 配置错误：{e}"))
        sys.exit(2)
    except runtime.scenario_mod.ScenarioError as e:
        print(f"审查情境错误：{e}")
        runtime._emit_domain_error(runtime.ConfigError(f"审查情境错误：{e}"))
        sys.exit(2)
    except runtime.format_spec_mod.FormatSpecError as e:
        print(f"格式规格错误：{e}")
        runtime._emit_domain_error(runtime.ConfigError(f"格式规格错误：{e}"))
        sys.exit(2)
    except runtime.UnreadableTargetError as e:
        print(f"目标不可读：{e}")
        runtime._emit_domain_error(e)
        sys.exit(1)
    if opts.engine == "shadow":
        runtime._run_shadow(report, target_path, rules,
                            type_override=opts.type_override,
                            agent_id=opts.agent_id, purpose=opts.purpose,
                            purpose_max=opts.purpose_max,
                            research_enabled=opts.research_enabled)
    print(f"[{report['review_id']}] 裁定={report['verdict']} | "
          f"Agent={(report.get('agent') or {}).get('id')} | "
          f"情境={(report.get('scenario') or {}).get('id') or '-'} | "
          f"规格={(report.get('format_spec') or {}).get('id') or '-'} | "
          f"模式={report['review_mode']} | 异议 {len(report['issues'])} 条 | "
          f"输出：outputs/{report['review_id']}.json/.md")
    runtime._emit_result(report["review_id"],
                         runtime.OUTPUTS / f"{report['review_id']}.json",
                         verdict=report.get("verdict"))
