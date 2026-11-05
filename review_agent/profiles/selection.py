# -*- coding: utf-8 -*-
"""审查配置选择：Agent / 情境 / 格式规格的 id 校验，只此一份实现。

2.5 之前，server 的 `_resolve_*` 与 CLI parser 各自实现了一遍同样的规则：
空值怎么归一、未知 id 的候选列表怎么拼、配置读取失败时用哪个前缀。两份
代码的文案已经出现过细微差异（`格式规格错误` vs `格式规格配置错误`），
继续各写各的迟早会让 Web 与 CLI 对同一个输入给出不同结论。

这里的函数是纯函数：配置文件由调用方注入 loader 读取，异常类型也由调用方
传入。这样既不把 `tools/` 的旧模块反向 import 进 profiles 层，也不复制
它们的解析/校验逻辑——本模块只负责「选哪个 id」和「怎么报错」。
"""
from __future__ import annotations

from typing import Any, Callable


def resolve_agent(raw: Any, list_agents: Callable[[], list[dict]]) -> tuple[str | None, str | None]:
    """校验 Agent id；空值/auto 表示走自动路由。"""
    if raw in (None, "", "auto"):
        return None, None
    known = {a["id"] for a in list_agents()}
    if raw not in known:
        return None, f"未知 Agent：{raw}；可选：{'、'.join(sorted(known))}"
    return str(raw), None


def resolve_scenario(
    raw: Any,
    load: Callable[[], dict],
    error_type: type[Exception],
    *,
    config_prefix: str = "情境配置错误",
    allow_default: bool = True,
) -> tuple[str | None, str | None]:
    """校验情境 id；空值/default 表示不指定（全规则，等价旧行为）。"""
    if raw in (None, ""):
        return None, None
    if allow_default and raw == "default":
        return None, None
    try:
        known = [s["id"] for s in load()["scenarios"]]
    except error_type as exc:
        return None, f"{config_prefix}：{exc}"
    if str(raw) not in known:
        return None, f"未知审查情境：{raw}；可选：{'、'.join(known)}"
    return str(raw), None


def resolve_format_spec(
    raw: Any,
    load: Callable[[], dict],
    error_type: type[Exception],
    *,
    config_prefix: str = "格式规格配置错误",
    allow_default: bool = True,
    allow_dict: bool = False,
) -> tuple[Any, str | None]:
    """校验格式规格 id。

    `allow_dict` 只给 CLI 的 `--format-spec-json` 用：前端已经构建好的
    check dict 没有 id，不应该再拿它去规格表里查；服务端仍走
    `_resolve_format_custom`，不会把 dict 当 id。
    """
    if raw in (None, ""):
        return None, None
    if allow_default and raw == "default":
        return None, None
    if isinstance(raw, dict) and allow_dict:
        return raw, None
    try:
        known = [s["id"] for s in load()["specs"]]
    except error_type as exc:
        return None, f"{config_prefix}：{exc}"
    if str(raw) not in known:
        return None, f"未知格式规格：{raw}；可选：{'、'.join(known)}"
    return str(raw), None
