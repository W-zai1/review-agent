"""审查请求的参数归一与幂等键组装。

同步 /api/review 与异步 /api/review/start 原本各写一遍：
目标解析、llm/research/type 归一、agent/scenario/format 校验、
目的组装、幂等键计算。两份实现一旦漂移，同一个请求走同步和异步
会得到不同任务身份。这里把这组纯逻辑收成一份。

本模块不 import server，也不碰 TaskService / JOBS / 子进程：
调用方把解析器、目的组装器和幂等键函数注入进来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReviewRequest:
    """一次审查请求中与执行无关、但决定任务身份的参数。"""

    file: str
    target: Path
    llm: object
    research: object
    type: object
    agent: object
    scenario: object
    format_spec: object
    format_custom: object
    purpose_text: str
    purpose_max: int | None
    purpose_quality: object
    spec_key: str
    idempotency_key: str


def resolve_target(data, resolver):
    """解析目标文件；返回 (file, target, error)。"""
    file = str((data or {}).get("file") or "").strip()
    target, error = resolver(file)
    if error:
        return None, None, error
    return file, target, None


def assemble_request(data, *, file, target, target_types, resolve_agent,
                     resolve_scenario, resolve_format_spec,
                     resolve_format_custom, clean_purpose, assemble_purpose,
                     idempotency_key, normalize_flags=True):
    """把 HTTP 请求体归一成 ReviewRequest；错误语义与旧 Handler 一致。

    normalize_flags=False 用于同步接口：它历史上不把非布尔 llm/research
    改成 None，保持这一差异，避免顺手改变既有行为。
    """
    payload = data or {}
    llm_opt = payload.get("llm")
    research_opt = payload.get("research")
    if normalize_flags:
        if llm_opt not in (True, False, None):
            llm_opt = None
        if research_opt not in (True, False, None):
            research_opt = None

    type_opt = payload.get("type")
    if type_opt not in target_types:
        type_opt = None

    agent_opt, agent_err = resolve_agent(payload.get("agent"))
    if agent_err:
        return None, agent_err
    scenario_opt, scenario_err = resolve_scenario(payload.get("scenario"))
    if scenario_err:
        return None, scenario_err
    spec_opt, spec_err = resolve_format_spec(payload.get("format_spec"))
    if spec_err:
        return None, spec_err
    custom_opt, custom_err = resolve_format_custom(payload.get("format_custom"))
    if custom_err:
        return None, custom_err

    typed_purpose = clean_purpose(payload.get("purpose"))
    purpose_text, purpose_err, purpose_quality = assemble_purpose(
        typed_purpose, payload.get("purpose_files"))
    if purpose_err:
        return None, purpose_err
    purpose_max = len(purpose_text) if purpose_text else None

    # 自定义格式要求必须进指纹：同一份文件、同样的其他选项，
    # 只改了「行距 2 倍 → 固定 22 磅」也是一次新的审查。
    spec_key = (spec_opt if not custom_opt
                else "custom:" + json.dumps(custom_opt.get("check") or {},
                                            ensure_ascii=False, sort_keys=True))
    key = idempotency_key(file=file, llm=llm_opt, research=research_opt,
                          type_=type_opt, agent=agent_opt,
                          scenario=scenario_opt, spec=spec_key,
                          purpose_text=purpose_text)
    return ReviewRequest(
        file=file,
        target=target,
        llm=llm_opt,
        research=research_opt,
        type=type_opt,
        agent=agent_opt,
        scenario=scenario_opt,
        format_spec=spec_opt,
        format_custom=custom_opt,
        purpose_text=purpose_text,
        purpose_max=purpose_max,
        purpose_quality=purpose_quality,
        spec_key=spec_key,
        idempotency_key=key,
    ), None


def build_runner_spec(request, *, job_id, progress):
    """把 ReviewRequest 封成 _SubprocessRunner 需要的 spec。"""
    return {
        "job_id": job_id,
        "file": request.file,
        "progress": progress,
        "llm_opt": request.llm,
        "research_opt": request.research,
        "type_opt": request.type,
        "agent_opt": request.agent,
        "purpose": request.purpose_text or None,
        "purpose_max": request.purpose_max,
        "purpose_quality": request.purpose_quality,
        "scenario_opt": request.scenario,
        "spec_opt": request.format_spec,
        "custom_opt": request.format_custom,
    }


def build_execution_options(request):
    """保存可重建续跑 ports 的请求参数（不包含临时进度/任务视图身份）。"""
    notes = request.purpose_quality if isinstance(
        request.purpose_quality, (list, tuple)) else []
    return {
        "manifest_version": 1,
        "engine": "v1",
        "use_llm": request.llm,
        "type_override": request.type,
        "agent_id": request.agent,
        "purpose_max": request.purpose_max,
        "purpose_notes": [str(x) for x in notes],
        "scenario": request.scenario,
        "format_spec": (request.format_custom or request.format_spec),
        "research_enabled": request.research is True,
    }
