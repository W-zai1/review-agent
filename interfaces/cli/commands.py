#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 命令层：解析交给 parser，这里只做审查执行与错误映射。

从 `review.py::main()` 迁出（第二阶段 2.1a），2.1b 把参数解析拆到 parser.py。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _fail(runtime, message, exc, code):
    """CLI 错误的唯一出口：人类可读信息 + 结构化分类行 + 退出码。"""
    print(message)
    runtime._emit_domain_error(exc)
    sys.exit(code)


def _fail_config(runtime, prefix, exc):
    """把配置类异常包装成 `ConfigError` 后的统一出口。"""
    _fail(runtime, f"{prefix}：{exc}",
          runtime.ConfigError(f"{prefix}：{exc}"), 2)


def _execute_v1(runtime, target_path, rules, opts):
    """v1 的两条入口：服务端子进程直跑，或手工 CLI 走 TaskService。"""
    kwargs = dict(
        type_override=opts.type_override, agent_id=opts.agent_id,
        use_llm=opts.use_llm, purpose=opts.purpose,
        purpose_max=opts.purpose_max, purpose_notes=opts.purpose_notes,
        scenario=opts.scenario, format_spec=opts.format_spec,
        research_enabled=opts.research_enabled,
        progress_path=opts.progress_path)
    if opts.review_id is not None:
        # 服务端子进程：父进程已建任务记录（含幂等键），这里只执行、不再建任务。
        return runtime._run_v1(
            target_path, rules, review_id=opts.review_id, **kwargs)
    # 手工 CLI：任务生命周期走 TaskService（与 Web 同一条链）
    return runtime._run_v1_via_task(target_path, rules, **kwargs)


def main(argv=None, *, runtime):
    from interfaces.cli.parser import parse_args
    opts = parse_args(argv, runtime=runtime)
    if opts is None:
        return
    if opts.task_action:
        return _run_task_control(opts, runtime)
    return _run_review(opts, runtime)


def _run_task_control(opts, runtime):
    """只读状态与取消：复用 TaskService，不进入审查装配。"""
    service = runtime.make_task_service()
    try:
        if opts.task_action == "status":
            record = service.get_task(opts.task_review_id)
            if record is None:
                exc = runtime.NotFoundError(f"找不到任务：{opts.task_review_id}")
                _fail(runtime, str(exc), exc, 1)
        else:
            try:
                record = service.cancel(
                    opts.task_review_id,
                    reason=opts.task_reason or "CLI 用户取消")
            except runtime.DomainError as exc:
                _fail(runtime, f"取消任务失败：{exc}", exc, 1)
        payload = {"ok": True, **record.to_dict()}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return record
    finally:
        service.close()


def _run_review(opts, runtime):
    """审查入口：先套一层外发审计上下文（决策书 4.6），再走引擎分支。

    为什么套在 CLI 入口而不是各条链里：真正的 HTTP 出口只有
    `llm_client.chat()` 一处，它由 contextvar 决定「这次外发属于哪个任务」。
    入口处设一次，OCR、目的裁决、分块语义审的外发就自动有了 attempt/result。
    `_fail()` 走 `sys.exit`，所以必须 try/finally 复位，否则同进程后续任务
    会把记录写到上一个任务名下。
    """
    target_path = ((runtime.ROOT / opts.target_arg)
                   if not os.path.isabs(opts.target_arg) else Path(opts.target_arg))
    scope = runtime.llm_client.EgressScope(
        runtime.STATE / "egress.log", target=str(target_path), kind="llm")
    token = runtime.llm_client.set_egress_scope(scope)
    try:
        return _run_review_body(opts, runtime)
    finally:
        runtime.llm_client.reset_egress_scope(token)


def _run_review_body(opts, runtime):
    target_path = ((runtime.ROOT / opts.target_arg)
                   if not os.path.isabs(opts.target_arg) else Path(opts.target_arg))
    if not target_path.exists():
        _fail(runtime, f"找不到待审文件：{target_path}",
              runtime.NotFoundError(f"找不到待审文件：{target_path}"), 1)
    rules = runtime.load_rules()
    if opts.engine == "shadow" and opts.use_llm is not False:
        # 双跑比对不能拿模型输出比：语义审本身有波动，差异无从判定是 bug 还是随机。
        # 显式拒绝，比产出一堆「未解释差异」强。
        _fail(runtime, "--engine shadow 目前只支持纯规则模式：请加 --no-llm",
              runtime.InputError("--engine shadow 需要 --no-llm"), 2)
    if opts.engine == "v1":
        try:
            _execute_v1(runtime, target_path, rules, opts)
        except runtime.scenario_mod.ScenarioError as e:
            _fail_config(runtime, "审查情境错误", e)
        except runtime.format_spec_mod.FormatSpecError as e:
            _fail_config(runtime, "格式规格错误", e)
        except runtime.ConfigError as e:
            _fail(runtime, f"[v1] {e}", e, 2)
        except runtime.UnreadableTargetError as e:
            _fail(runtime, f"目标不可读：{e}", e, 1)
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
        _fail_config(runtime, "Agent 配置错误", e)
    except runtime.scenario_mod.ScenarioError as e:
        _fail_config(runtime, "审查情境错误", e)
    except runtime.format_spec_mod.FormatSpecError as e:
        _fail_config(runtime, "格式规格错误", e)
    except runtime.UnreadableTargetError as e:
        _fail(runtime, f"目标不可读：{e}", e, 1)
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
