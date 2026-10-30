# -*- coding: utf-8 -*-
"""语义审节点（Semantic Reviewer）。

它是六个节点里唯一**必须**调用模型的：分块、按维度送审、把模型给的候选变成发现。
这里接管的不是 prompt（那在 `llm_review` 里），而是三件容易出错、又完全可测的事：

    1. **跑哪些（维度 × 块）**：顺序固定，不按完成时间拼（否则两次跑出不同报告）
    2. **覆盖了多少原文**：成功块的区间并集，块间重叠只算一次
    3. **哪些块失败了**：任何 (维度, 块) 失败都必须让 `complete=False`——
       旧实现把失败块 `continue` 掉后仍报「全覆盖」，202 份历史报告里
       `complete=False` 一次都没出现过，就是这个原因

调用模型的部分通过 `SemanticExecutor` 端口注入（参数是 step 与起始编号）。
这样节点可以用假执行器做完整测试：不需要网络、不需要 prompt、不产生随机输出。

与旧实现的等价边界：
    * 覆盖率口径（含 `max_input_chars` 截断）与 `semantic_review` 逐字一致
    * 去重 / 语境豁免 / `make_issue` 仍由执行器适配层负责——它们是 Session 语义，
      等阶段 3 的能力层整理再搬
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from ...domain import Finding, Stage, ids
from ..node import NodeContext, NodeResult

NAME: Final = "semantic-reviewer"
DEFAULT_MAX_INPUT_CHARS: Final = 12000


@dataclass(frozen=True, slots=True)
class SemanticStep:
    """一步语义审：某个维度的一段原文。"""

    dimension: str
    chunk_index: int
    start: int
    end: int
    text: str

    def __post_init__(self) -> None:
        if not str(self.dimension or "").strip():
            raise ValueError("SemanticStep.dimension 不能为空")
        if self.chunk_index < 1:
            raise ValueError("chunk_index 从 1 开始")
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"非法区间：{self.start}..{self.end}")


def plan_semantic_steps(
    dimensions: Sequence[str],
    spans: Sequence[tuple[int, int]],
    text: str,
) -> tuple[SemanticStep, ...]:
    """维度 × 块 的执行计划。

    顺序是「维度声明顺序 → 块顺序」，与旧实现的双层循环一致。
    合并顺序必须确定：按完成时间排序会让同一份输入两次跑出不同的报告顺序。
    """
    text = text or ""
    steps: list[SemanticStep] = []
    for dimension in dimensions:
        for index, (start, end) in enumerate(spans, 1):
            steps.append(SemanticStep(
                dimension=str(dimension), chunk_index=index,
                start=int(start), end=int(end), text=text[int(start):int(end)],
            ))
    return tuple(steps)


def covered_chars(spans: Sequence[tuple[int, int]]) -> int:
    """若干 [start, end) 区间的并集长度（块间重叠只算一次）。"""
    total = 0
    cur_start = cur_end = None
    for start, end in sorted(spans):
        if end <= start:
            continue
        if cur_start is None:
            cur_start, cur_end = start, end
        elif start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    if cur_start is not None:
        total += cur_end - cur_start
    return total


@dataclass(frozen=True, slots=True)
class SemanticStepOutcome:
    """一步语义审的结果。失败必须带 error，成功才计入覆盖。"""

    findings: tuple[Finding, ...] = ()
    candidates: int = 0
    dropped: int = 0
    retracted: tuple[Mapping[str, Any], ...] = ()
    cached: bool = False
    usage: Mapping[str, int] = field(default_factory=dict)
    truncated: bool = False
    output_truncated: bool = False
    finish_reason: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# (step, 起始编号) -> 这一步的产出
SemanticExecutor = Callable[[SemanticStep, int], SemanticStepOutcome]


def build_meta(
    steps: Sequence[SemanticStep],
    outcomes: Sequence[SemanticStepOutcome],
    *,
    chars_total: int,
    max_input_chars: int,
) -> dict[str, Any]:
    """把逐步结果汇总成旧 `llm_meta` 同形状的字典。

    形状必须一致：`coverage_from_meta()` 是同一份推导逻辑，报告里的覆盖率、
    limitations、控制台都依赖这些键名。
    """
    reviewed_spans: list[tuple[int, int]] = []
    failed: list[dict[str, Any]] = []
    retracted: list[Mapping[str, Any]] = []
    input_truncated: list[str] = []
    output_truncated: list[str] = []
    attempted = succeeded = 0
    cache_hits = 0
    tokens = {"input": 0, "output": 0}
    candidates = dropped = 0

    for step, outcome in zip(steps, outcomes):
        attempted += 1
        if not outcome.ok:
            failed.append({"dim": step.dimension, "chunk": step.chunk_index,
                           "start": step.start, "end": step.end,
                           "error": str(outcome.error)[:200]})
            continue
        succeeded += 1
        # 单块模式（未分块）下模型只吃前 max_input_chars 个字符，
        # 覆盖区间必须同步截到那里；分块模式下这是恒等变换。
        reviewed_spans.append((step.start, min(step.end,
                                               step.start + max(0, max_input_chars))))
        if outcome.cached:
            cache_hits += 1
        if outcome.truncated and step.dimension not in input_truncated:
            input_truncated.append(step.dimension)
        if outcome.output_truncated and step.dimension not in output_truncated:
            output_truncated.append(step.dimension)
        retracted.extend(outcome.retracted)
        candidates += int(outcome.candidates or 0)
        dropped += int(outcome.dropped or 0)
        for key in ("input", "output"):
            tokens[key] += int((outcome.usage or {}).get(key, 0) or 0)

    return {
        "chars_total": int(chars_total),
        "chars_reviewed": covered_chars(reviewed_spans),
        "chunks": max((s.chunk_index for s in steps), default=1),
        "chunked": max((s.chunk_index for s in steps), default=1) > 1,
        "chunks_attempted": attempted,
        "chunks_succeeded": succeeded,
        "cache_hits": cache_hits,
        "failed_chunks": failed,
        "input_truncated_dimensions": input_truncated,
        "output_truncated_dimensions": output_truncated,
        "candidates": candidates,
        "dropped": dropped,
        "retracted": retracted,
        "tokens": tokens,
        "steps": len(steps),
    }


def coverage_from_meta(meta: Mapping[str, Any] | None, chars_total: int) -> dict[str, Any]:
    """由语义审 meta 推导真实文本覆盖与「是否完整」（纯函数，便于单测）。

    complete 旧口径只看输入/输出截断，块级调用失败被 `continue` 掉后仍报
    「全覆盖」——202 份历史报告里 complete=False 一次没出现过，根因就在这里。
    现在多一个硬条件：任何 (dimension, chunk) 失败都使 complete=False。
    """
    meta = meta or {}
    try:
        chars_total = max(0, int(chars_total or 0))
    except (TypeError, ValueError):
        chars_total = 0

    in_trunc = list(meta.get("input_truncated_dimensions") or [])
    out_trunc = list(meta.get("output_truncated_dimensions") or [])
    failed = []
    for item in meta.get("failed_chunks") or []:
        if not isinstance(item, Mapping):
            continue
        failed.append({
            "dim": item.get("dim"),
            "chunk": item.get("chunk"),
            "start": item.get("start"),
            "end": item.get("end"),
        })

    # 兼容纯规则模式 / 旧 meta：没有 chars_reviewed 时按整篇算——没有调用模型
    # 不算"失败"。但不能用 `or chars_total`：全块失败时 0 是有效值，
    # falsy 回退会把 0 变成全文，正是假全覆盖的来源。
    if meta.get("chars_reviewed") is None:
        reviewed = 0 if failed else chars_total
    else:
        try:
            reviewed = int(meta.get("chars_reviewed"))
        except (TypeError, ValueError):
            reviewed = 0
    reviewed = max(0, min(reviewed, chars_total))

    try:
        attempted = max(0, int(meta.get("chunks_attempted") or 0))
    except (TypeError, ValueError):
        attempted = 0
    try:
        succeeded = max(0, int(meta.get("chunks_succeeded") or 0))
    except (TypeError, ValueError):
        succeeded = 0

    complete = not in_trunc and not out_trunc and not failed
    # 防御：定位/切分异常导致实际覆盖不足时也不能称"全覆盖"。
    if reviewed < chars_total:
        complete = False
    return {
        "chars_total": chars_total,
        "chars_reviewed": reviewed,
        "text_coverage_pct": (round(reviewed / chars_total * 100)
                              if chars_total else 100),
        "chunks_attempted": attempted,
        "chunks_succeeded": succeeded,
        "failed_chunks": failed,
        "input_truncated_dimensions": in_trunc,
        "output_truncated_dimensions": out_trunc,
        "complete": complete,
    }


class SemanticReviewerNode:
    """analysis 阶段（模型部分）节点：按计划送审、按区间记覆盖。"""

    name: Final = NAME
    stage: Final = Stage.ANALYSIS
    produces: Final = frozenset({"findings", "semantic_stats"})

    def __init__(self, executor: SemanticExecutor,
                 max_input_chars: int = DEFAULT_MAX_INPUT_CHARS) -> None:
        self.executor = executor
        self.max_input_chars = int(max_input_chars)

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.ANALYSIS:
            return NodeResult.fail(
                f"{NAME} 只处理 analysis 阶段，当前状态在 {state.stage.value}")
        text = state.artifacts.get("target_text")
        if not isinstance(text, str):
            return NodeResult.fail(
                "analysis 阶段缺少 artifacts['target_text']：没有原文无从送审")

        dimensions = tuple(ctx.config.get("dimensions") or state.profile.dimensions)
        if not dimensions:
            return NodeResult.fail(
                "没有可送审的维度：Profile 未声明 dimensions，config 也没给")
        spans = tuple(ctx.config.get("chunks") or ((0, len(text)),))
        steps = plan_semantic_steps(dimensions, spans, text)

        outcomes: list[SemanticStepOutcome] = []
        findings: list[Finding] = []
        seq = int(ctx.next_finding_seq)
        for step in steps:
            try:
                outcome = self.executor(step, seq)
            except Exception as e:                      # noqa: BLE001 - 边界统一转失败
                outcome = SemanticStepOutcome(
                    error=f"{type(e).__name__}: {e}")
            outcomes.append(outcome)
            findings.extend(outcome.findings)
            seq += len(outcome.findings)

        if steps and all(not o.ok for o in outcomes):
            return NodeResult.fail(
                *[f"{steps[i].dimension}#{steps[i].chunk_index}: {o.error}"
                  for i, o in enumerate(outcomes) if not o.ok][:5],
                "全部语义审调用失败：不允许以「未发现问题」出报告",
            )

        meta = build_meta(steps, outcomes, chars_total=len(text),
                          max_input_chars=self.max_input_chars)
        coverage = coverage_from_meta(meta, len(text))
        failed = meta["failed_chunks"]
        warnings: tuple[str, ...] = ()
        if failed:
            warnings = (f"{len(failed)} 个「维度×块」语义审失败，"
                        f"相关段落未完成模型复核（{meta['chars_reviewed']}"
                        f"/{len(text)} 字符已审）",)
        return NodeResult.ok(
            findings=tuple(findings),
            output={"semantic_stats": {"coverage": coverage, "meta": meta}},
            warnings=warnings,
        )
