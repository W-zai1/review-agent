#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CLI 参数解析：argv -> CliOptions，或直接完成 --list*/--print-review-id。

从 `interfaces/cli/commands.py` 拆出（第二阶段 2.1b）。解析顺序、错误文案、
退出码与拆出前逐行一致；`runtime` 注入正在运行的模块对象（`__main__` 或 `review`）。
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CliOptions:
    engine: str = "v1"
    target_arg: str = ""
    use_llm: bool | None = None
    progress_path: str | None = None
    type_override: str | None = None
    agent_id: str | None = None
    purpose: str | None = None
    purpose_max: int | None = None
    purpose_notes: list[str] | None = None
    scenario: str | None = None
    format_spec: dict | str | None = None
    research_enabled: bool = False
    review_id: str | None = None


def parse_args(argv=None, *, runtime):
    """返回 CliOptions；`--list*` / `--print-review-id` 已处理时返回 None。"""
    args = list(sys.argv[1:] if argv is None else argv)
    use_llm = None
    progress_path = None
    type_override = None
    agent_id = None
    purpose = None
    purpose_max = None
    purpose_notes = None
    scenario = None
    research_enabled = False
    review_id_opt = None          # `--review-id`：服务端预先发的号（见 runtime._emit_result）
    # 引擎选择：默认 v1 八阶段流水线；legacy = 旧链路回滚；shadow = 两条都跑并比对。
    engine, args = runtime._take_engine(args)
    if engine == "off":
        return None
    if "--scenario" in args:
        i = args.index("--scenario")
        if i + 1 < len(args):
            scenario = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            try:
                opts = "、".join(s["id"] for s in runtime.scenario_mod.load()["scenarios"])
            except runtime.scenario_mod.ScenarioError as e:
                opts = f"（情境配置读取失败：{e}）"
            print(f"--scenario 缺少取值。可用：{opts}")
            sys.exit(2)
    if "--list-scenarios" in args:
        data = runtime.scenario_mod.load()
        print(f"可用审查情境（默认 {data['default']}，配置版本 {data['version']}）：")
        for s in data["scenarios"]:
            print(f"  {s['id']:<20}{s['name']}")
            print(f"      关闭规则：{'、'.join(s['disable_rules']) or '无'}"
                  + (f"｜关闭维度：{'、'.join(s['disable_dimensions'])}"
                     if s["disable_dimensions"] else ""))
            if s["keep_note"]:
                print(f"      仍然检查：{s['keep_note']}")
        return None
    if scenario and runtime.scenario_mod.get(scenario) is None:
        try:
            opts = "、".join(s["id"] for s in runtime.scenario_mod.load()["scenarios"])
        except runtime.scenario_mod.ScenarioError as e:
            print(f"审查情境错误：{e}")
            sys.exit(2)
        print(f"未知审查情境：{scenario}；可选：{opts}")
        sys.exit(2)
    format_spec = None
    if "--format-spec-json" in args:
        i = args.index("--format-spec-json")
        if i + 1 < len(args):
            raw_json = args[i + 1]
            del args[i:i + 2]
            try:
                format_spec = json.loads(raw_json)
            except json.JSONDecodeError as e:
                print(f"--format-spec-json 不是合法 JSON：{e}")
                sys.exit(2)
            if not isinstance(format_spec, dict):
                print("--format-spec-json 必须是对象（check 块）")
                sys.exit(2)
        else:
            del args[i]
            print("--format-spec-json 缺少取值")
            sys.exit(2)
    if "--format-spec" in args:
        i = args.index("--format-spec")
        if i + 1 < len(args):
            # 同时给了 --format-spec-json 时以 json 为准：它是更精确的那一个
            # （前端已经按分项表单算好了数值），id 只是它的来源说明。
            if not isinstance(format_spec, dict):
                format_spec = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            try:
                opts = "、".join(s["id"] for s in runtime.format_spec_mod.load()["specs"])
            except runtime.format_spec_mod.FormatSpecError as e:
                opts = f"（格式规格配置读取失败：{e}）"
            print(f"--format-spec 缺少取值。可用：{opts}")
            sys.exit(2)
    if "--list-format-specs" in args:
        data = runtime.format_spec_mod.load()
        print(f"可用格式规格（默认 {data['default']}，配置版本 {data['version']}）：")
        for s in data["specs"]:
            print(f"  {s['id']:<24}{s['name']}")
            print(f"      判定项：{'、'.join(sorted(s['check'])) or '无'}")
            if s["source"]:
                print(f"      来源：{s['source']}")
            if s["disable_rules"]:
                print(f"      不参与：{'、'.join(s['disable_rules'])}")
        return None
    if format_spec and runtime.format_spec_mod.get(format_spec) is None:
        try:
            opts = "、".join(s["id"] for s in runtime.format_spec_mod.load()["specs"])
        except runtime.format_spec_mod.FormatSpecError as e:
            print(f"格式规格错误：{e}")
            sys.exit(2)
        print(f"未知格式规格：{format_spec}；可选：{opts}")
        sys.exit(2)
    if "--agent" in args:
        i = args.index("--agent")
        if i + 1 < len(args):
            agent_id = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            print("--agent 缺少取值。可用："
                  + "、".join(a["id"] for a in runtime.agent_spec.list_agents()))
            sys.exit(2)
    if "--list-agents" in args:
        print("可用审查 Agent（每个 Agent = 一套判定标准）：")
        for a in runtime.agent_spec.list_agents():
            if a.get("error"):
                print(f"  {a['id']:<16} [无效] {a['error'][:100]}")
                continue
            print(f"  {a['id']:<16}{a['version']:<8}{a['name']}")
            print(f"      维度：{'、'.join(d['label'] for d in a['dimensions'])}")
            print(f"      规则：{a['rules']} 条｜检查器：{len(a['checks'])} 项｜"
                  f"自动路由：{'是' if a['auto'] else '否（需 --agent 显式指定）'}")
        return None
    if "--type" in args:
        i = args.index("--type")
        if i + 1 < len(args):
            type_override = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
    valid_types = runtime.agent_spec.all_target_types() or list(runtime.DIM_FOR_TYPE)
    if type_override and type_override not in valid_types:
        print(f"未知类型：{type_override}，可选：{', '.join(valid_types)}")
        sys.exit(2)
    if "--purpose" in args:
        i = args.index("--purpose")
        if i + 1 < len(args):
            purpose = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            print("--purpose 缺少取值（例：--purpose \"这是给客户的报价邮件，找承诺过头的地方\"）")
            sys.exit(2)
    if "--purpose-max" in args:
        i = args.index("--purpose-max")
        if i + 1 < len(args):
            try:
                purpose_max = int(args[i + 1])
            except ValueError:
                print("--purpose-max 必须是整数（目的文本参与解析的字数上限）")
                sys.exit(2)
            if not 1 <= purpose_max <= 20000:
                print("--purpose-max 需在 1-20000 之间")
                sys.exit(2)
            del args[i:i + 2]
        else:
            del args[i]
            print("--purpose-max 缺少取值（例：--purpose-max 2000，配合上传的目的文件使用）")
            sys.exit(2)
    if "--purpose-notes" in args:
        i = args.index("--purpose-notes")
        if i + 1 < len(args):
            raw = args[i + 1]
            try:
                parsed = json.loads(raw)
            except Exception:
                print("--purpose-notes 必须是 JSON 数组（目的来源的质量提示）")
                sys.exit(2)
            if not isinstance(parsed, list):
                print("--purpose-notes 必须是 JSON 数组（目的来源的质量提示）")
                sys.exit(2)
            purpose_notes = [str(x) for x in parsed]
            del args[i:i + 2]
        else:
            del args[i]
            print("--purpose-notes 缺少取值（JSON 数组）")
            sys.exit(2)
    if "--progress" in args:
        i = args.index("--progress")
        if i + 1 < len(args):
            progress_path = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
    if "--no-llm" in args:
        use_llm = False
        args = [a for a in args if a != "--no-llm"]
    if "--no-research" in args:
        # 离线/涉密场景：不联网核验，报告会写明「未做外部事实核验」
        research_enabled = False
        args = [a for a in args if a != "--no-research"]
    if "--research" in args:
        research_enabled = True
        args = [a for a in args if a != "--research"]
    if "--no-gap-log" in args:
        # 缺口清单里会存被审文本的片段。涉密/涉个人信息的材料可以不落这份工单
        # （代价：下次仍然不知道该补什么，报告里 fact_gaps.disabled=True 会写明）。
        runtime.GAP_LOG = False
        args = [a for a in args if a != "--no-gap-log"]
    if "--llm" in args:
        use_llm = True
        args = [a for a in args if a != "--llm"]
    # `--review-id <R-...>`：由服务端预先发号（它要先建任务记录，记录里的
    # review_id 必须与报告一致）。号仍然由本模块统一发，见 `--print-review-id`。
    if "--review-id" in args:
        i = args.index("--review-id")
        if i + 1 < len(args):
            review_id_opt = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
            print("--review-id 缺少取值（形如 R-20261101-001）")
            sys.exit(2)
    if "--print-review-id" in args:
        # 服务端在起任务前调用它拿号：只发号、不做任何审查、不写任何报告。
        # 为什么用子进程而不是把发号逻辑复制到服务端：两份发号逻辑迟早会发出同一个号。
        date = datetime.date.today().strftime("%Y%m%d")
        new_id, lock = runtime.allocate_review_id(date)
        try:
            lock.unlink()
        except OSError:
            pass
        runtime._ID_LOCKS.discard(lock)
        print(json.dumps({"review_id": new_id}, ensure_ascii=False))
        return None
    if not args or args[0] == "--list":
        print("可用待审对象：")
        paths = sorted(glob.glob(str(runtime.TARGETS / "*")))
        paths += sorted(glob.glob(str(runtime.TARGETS / "uploads" / "*")))
        for p in paths:
            if os.path.isfile(p):
                print("  ", str(Path(p).relative_to(runtime.ROOT)).replace("\\", "/"))
        print("\n可选参数：--llm 强制调用大模型；--no-llm 强制纯规则审；"
              "--type 显式指定对象类型；--agent 指定审查 Agent（--list-agents 查看）；"
              "--scenario 指定审查情境（--list-scenarios 查看：不同用途关掉不同规则）；"
              "--format-spec 指定格式规格（--list-format-specs 查看：docx 版式按哪套标准判，"
              "默认 none 即不判字体字号行距）；"
              "--purpose \"这份文本想干什么\"（按目的派生目标并定位证据，默认解析前 300 字）；"
              "--purpose-max N 目的文本参与解析的字数上限（1-20000，上传目的文件时用）；"
              "--research 对事实库外的数字/日期主张联网检索来源并核验（默认关闭）；"
              "--no-research 强制关闭联网核验；"
              "--engine legacy|v1|shadow 选择执行引擎（v1 默认；v1 跑八阶段流水线、"
              "写报告 outputs/v1/ 并落状态；legacy 为旧链路回滚；shadow 出旧报告并双跑比对，"
              "差异写 state/double_run/；shadow 需配合 --no-llm，v1 可带 --llm）；"
              "--no-gap-log 不把知识库缺口写进 state/（缺口里含被审文本片段，涉密材料用）")
        return None
    target_arg = args[0]
    return CliOptions(
        engine=engine, target_arg=target_arg, use_llm=use_llm,
        progress_path=progress_path, type_override=type_override,
        agent_id=agent_id, purpose=purpose, purpose_max=purpose_max,
        purpose_notes=purpose_notes, scenario=scenario,
        format_spec=format_spec, research_enabled=research_enabled,
        review_id=review_id_opt)
