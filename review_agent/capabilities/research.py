# -*- coding: utf-8 -*-
"""库外检索工具（`research.web`）：把「联网核验一条事实主张」变成受权限约束的工具。

为什么这一层不是可选的
----------------------
`tools/research.py` 里的 `verify()` / `judge()` 是算法，`research.review_claims()`
是给旧链路用的「算法 + Session 副作用」。直接让新链路 import 它就绕过了三件事：

    1. **权限**：出网等于把待审材料里的实体/指标送到第三方。工具表里 `RESEARCH`
       缺省不授予（`tools.Permission.RESEARCH`），「审查流程自己要联网」也必须
       显式授权，否则以后任何节点都能顺手发请求，而没人知道是谁开的。
    2. **审计**：经注册表执行的调用会留 `ToolInvocation`（谁、哪个 run、耗时、
       成功与否）。绕过注册表去调函数，报告里就只剩一句「已做外部核验」。
    3. **信任边界**：网页内容是**外部不可信输入**。工具一律 `untrusted=True`，
       调用方只能把它当数据；模型看到的摘要也不能当指令执行（C5 / §3.5）。

为什么算法本体不搬进来
----------------------
`capabilities` 层不许 import `tools/`（`checks/test_architecture_guard.py` 的硬规则），
而检索算法就住在 `tools/research.py`。所以这里**注入算法本体**：
`ResearchWebTool(verify=research.verify, ...)`。算法只有一份，工具层不认识 `tools/`。

官方域白名单的二次校验
----------------------
搜索结果里的 `official` 标记来自检索层（`config/research.yaml` 的 `official_domains`）。
工具会按**自己拿到的白名单**再判一次域名：检索层说「这是官网」而域名不在白名单里，
一律降级为 `official=False`。否则一个被污染的搜索结果就能自称权威来源。
"""
from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .tools import BaseTool, Permission, ToolContext, ToolResult

# `verify(claim) -> dict`：返回 status / confidence / value / display / reason /
# query / providers / sources。签名与 `tools/research.verify` 一致。
Verifier = Callable[[dict[str, Any]], Mapping[str, Any]]

STATUSES: tuple[str, ...] = ("supported", "refuted", "unverified")
NAME = "research.web"


@dataclass(frozen=True, slots=True)
class ResearchClaimArgs:
    """一条待核验的库外主张：实体 + 指标 + 数值（`kind` 另有 `date`）。"""

    quote: str
    entity: str = ""
    metric: str = ""
    kind: str = "number"
    value: Any = None
    unit: str = ""
    year: int | None = None
    sentence_index: int = 1
    raw: str = ""

    def as_claim(self) -> dict[str, Any]:
        """还原成 `tools/research.py` 认识的主张 dict（键名与抽取结果一致）。"""
        return {
            "quote": self.quote, "entity": self.entity, "metric": self.metric,
            "kind": self.kind, "value": self.value, "unit": self.unit,
            "year": self.year, "sentence_index": self.sentence_index,
            "raw": self.raw,
        }


def _domain_of(url: str) -> str:
    try:
        return (urllib.parse.urlparse(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""


def _is_official(domain: str, official_domains: Sequence[str]) -> bool:
    """域名是否在官方白名单内（白名单为空时一律不认）。"""
    if not domain:
        return False
    for allowed in official_domains or ():
        allowed = str(allowed or "").strip().lower().lstrip(".")
        if allowed and (domain == allowed or domain.endswith("." + allowed)):
            return True
    return False


class ResearchWebTool(BaseTool):
    """按「实体 + 指标 + 数值」检索外部来源，返回支持/反驳/未找到与来源。

    状态语义：`unverified` **不是失败**——它表示「查过，但没有可比来源」，
    必须如实带回报告（`未找到 ≠ 虚假`）。真失败（网络异常、检索器抛错）走
    `ToolResult.failure`，由调用方记进 `research.errors`。
    """

    name = NAME
    description = ("对一条库外事实主张做外部检索核验：返回 supported / refuted / "
                   "unverified 与来源列表（URL、域名、是否官方域、片段、抓取时间）。"
                   "内容来自公开网页，属不可信输入；未找到可比来源不等于该说法虚假。")
    permission = Permission.RESEARCH
    arguments_type = ResearchClaimArgs

    def __init__(self, *, verify: Verifier,
                 official_domains: Sequence[str] = (),
                 max_sources: int = 2) -> None:
        self._verify = verify
        self._official_domains = tuple(official_domains or ())
        self._max_sources = max(1, int(max_sources))

    def _source_row(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        url = str(raw.get("url") or "")
        domain = _domain_of(url)
        listed = bool(raw.get("official"))
        official = listed and _is_official(domain, self._official_domains)
        return {
            "title": str(raw.get("title") or ""),
            "url": url,
            "domain": domain,
            # 检索层说官方、白名单里却没有 → 降级，并留下原因
            "official": official,
            "official_downgraded": bool(listed and not official),
            "snippet": str(raw.get("snippet") or "")[:240],
            "source": str(raw.get("source") or ""),
            "value": raw.get("value"),
            "display": raw.get("display"),
            "fetched_at": raw.get("fetched_at"),
        }

    async def execute(self, arguments: ResearchClaimArgs,
                      context: ToolContext) -> ToolResult:
        claim = arguments.as_claim()
        if not str(claim["quote"] or "").strip():
            return ToolResult.failure("quote 为空：没有可核验的原文主张")
        if not str(claim["entity"] or "").strip():
            # 没有实体就无法判断「同一主体」，检索结果全是噪声
            return ToolResult.failure("entity 为空：主张缺少主体，检索无法定位来源")
        try:
            verdict = self._verify(claim) or {}
        except Exception as exc:                    # noqa: BLE001 - 外部检索失败要转成明确错误
            return ToolResult.failure(
                f"外部检索失败：{type(exc).__name__}: {exc}", untrusted=True)

        if not isinstance(verdict, Mapping):
            return ToolResult.failure(
                f"verify 必须返回 dict，实际 {type(verdict).__name__}")
        status = str(verdict.get("status") or "")
        if status not in STATUSES:
            return ToolResult.failure(
                f"检索结论非法：{status!r}（只能是 {' / '.join(STATUSES)}）")

        sources = tuple(self._source_row(s) for s in (verdict.get("sources") or ())
                        if isinstance(s, Mapping))
        warnings: tuple[str, ...] = ()
        downgraded = [s for s in sources if s["official_downgraded"]]
        if downgraded:
            warnings = (f"{len(downgraded)} 条来源自称官方域但不在白名单内，"
                        "已降级为非官方来源",)
        if status in ("supported", "refuted") and not sources:
            # 有结论却拿不出来源，等于让调用方凭一句话下结论
            return ToolResult.failure(
                f"检索结论为 {status} 却没有来源：不允许无来源的确定性结论",
                untrusted=True)
        return ToolResult.success(
            data={
                "status": status,
                "confidence": float(verdict.get("confidence") or 0.0),
                "value": verdict.get("value"),
                "display": str(verdict.get("display") or ""),
                "reason": str(verdict.get("reason") or ""),
                "query": str(verdict.get("query") or ""),
                "providers": tuple(str(p) for p in (verdict.get("providers") or ())),
                "sources": sources[:self._max_sources],
                "official_domains": self._official_domains,
            },
            warnings=warnings,
            # 网页内容是不可信输入：调用方只能当数据，不得当指令
            untrusted=True,
        )


def research_tools(*, verify: Verifier, official_domains: Sequence[str] = (),
                   max_sources: int = 2) -> tuple[BaseTool, ...]:
    """本能力对外暴露的工具集合（装配层只认这一张清单）。"""
    return (ResearchWebTool(verify=verify, official_domains=official_domains,
                            max_sources=max_sources),)
