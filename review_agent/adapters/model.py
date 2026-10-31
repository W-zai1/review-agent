# -*- coding: utf-8 -*-
"""模型客户端适配器：把「有 chat() 的东西」接成领域端口 `ModelClient`。

为什么需要这一层：
    领域层不许 import `tools/`（ARCHITECTURE §3）。但语义审要读模型返回的
    `content` / `model` / `finish_reason` / `usage` / `latency_ms`。
    直接在 `llm_review` 里绑死 `llm_client.LLMResult`，等于让「换供应商」
    必须改 prompt 与解析代码——这正是方案里「能否单独替换 LLM」要回答的问题。

    `ChatClientAdapter` 只做一件事：把任意「`.chat(messages)` 返回带上述字段的对象」
    归一成 `ModelReply`。所以：
        · 真实供应商（`llm_client.LLMClient`）走同一条缝
        · 测试 / 离线 / 别的厂商，只要实现 `chat()` 就能接进来，不改审查代码

反过来为什么不给 `tools/llm_client.py` 加 `ModelClient` 的继承：
    那会让迁移期的 `tools/` 依赖 `review_agent`，违反依赖方向。适配器放在
    `adapters/`（迁移期代码），由装配处决定用哪个真实客户端。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any

from ..domain.ports import ModelReply


@dataclasses.dataclass(frozen=True, slots=True)
class ModelClientSettings:
    """注入客户端时仍需的那几个「审查侧」参数。

    这些不是供应商配置（endpoint / key / 超时），而是**审查流程自己的**预算：
    一次送多少字、一个维度最多要几条、要不要用缓存。所以换供应商时它们不跟着变。
    """

    model: str = ""
    max_input_chars: int = 12000
    max_issues_per_dim: int = 5
    cache: bool = False


class ChatClientAdapter:
    """把一个 `chat()` 客户端适配成 `ModelClient`。

    容忍两种返回：
        · 已经是 `ModelReply`（自己实现的客户端）
        · 任意带 `content` / `model` / `finish_reason` / `usage` / `latency_ms` 的对象
          （例如 `tools/llm_client.LLMResult`）
    缺字段就给默认值，不让 `getattr` 失败把一次审查打断在「读结果」这一步。
    """

    def __init__(self, client: Any) -> None:
        if not callable(getattr(client, "chat", None)):
            raise TypeError(
                f"{type(client).__name__} 不满足 ModelClient：缺少 chat(messages) 方法")
        self._client = client

    @property
    def client(self) -> Any:
        return self._client

    def chat(self, messages: Sequence[Mapping[str, str]]) -> ModelReply:
        result = self._client.chat(messages)
        if isinstance(result, ModelReply):
            return result
        return ModelReply(
            content=str(getattr(result, "content", "") or ""),
            model=str(getattr(result, "model", "") or ""),
            finish_reason=str(getattr(result, "finish_reason", "") or ""),
            usage=dict(getattr(result, "usage", None) or {}),
            latency_ms=int(getattr(result, "latency_ms", 0) or 0),
        )


def as_model_client(client: Any) -> ChatClientAdapter:
    """统一入口：已经是适配器就原样返回，避免套两层。"""
    return client if isinstance(client, ChatClientAdapter) else ChatClientAdapter(client)


def settings_for(client: Any, **overrides: Any) -> ModelClientSettings:
    """从客户端或覆盖项推出审查侧参数。

    优先用调用方显式给的；其次读客户端自带的 `model` 属性；都没有就留空
    ——空字符串比编一个假模型名诚实（报告里能看出「不知道是哪个模型」）。
    """
    base = {
        "model": str(getattr(client, "model", "") or ""),
    }
    base.update({k: v for k, v in overrides.items() if v is not None})
    known = {f.name for f in dataclasses.fields(ModelClientSettings)}
    return ModelClientSettings(**{k: v for k, v in base.items() if k in known})
