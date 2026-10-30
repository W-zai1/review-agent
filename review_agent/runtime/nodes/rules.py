# -*- coding: utf-8 -*-
"""规则执行节点（Rule Analyzer）。

职责：决定**跑哪些检查、按什么顺序跑、每步产出什么**，并把结果收敛成 `Finding` 集合。
检查的实现不在这个文件里——它在能力层（`tools/` 与 Agent 自带的 `checks.py`），
通过 `RuleExecutor` 端口注入。这样做的原因很现实：

    检查函数今天统统是 `handler(session, text, rules, ctx)`，副作用写进 Session。
    把二十多个处理器一次性改成纯函数，等于停掉主流程一个迭代。
    节点先把**调度**接过来（调度是行为，会出错、必须可测），
    处理器改造留给阶段 3 的 Tool Registry。

调度顺序（与旧实现逐条一致）：
    1. 先按 manifest 的维度声明顺序，跑每个维度绑定的处理器
    2. 再跑 manifest 的附加检查（`checks`）
    3. 附加检查沿用「最后一个维度」的上下文（旧实现就是这么传的；
       改成「无维度」会让只认 ctx["dimension"] 的处理器行为变化）

为什么单独成节点：
    「这次到底跑了哪些检查」旧实现只写在一条日志里（`执行的检查` 字段），
    跑漏一类检查时报告看起来仍然正常。节点把每个处理器变成一条 `RuleStepOutcome`，
    `analysis_stats` 产物里能看到每步命中多少条——漏跑会立刻显形。

配置（`ctx.config`，都是 manifest 的投影，可 JSON 序列化）：
    handlers : Mapping[str, str]   维度 → 处理器名（缺省表示该维只由语义审负责）
    checks   : Sequence[str]       附加检查处理器名

本节点不做的：语义审（需要模型与网络），它是 analysis 阶段的另一半，后续单独接管。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import inspect
from typing import Any, Final, Protocol

from ...domain import Finding, Stage
from ..node import NodeContext, NodeResult

NAME: Final = "rule-analyzer"


@dataclass(frozen=True, slots=True)
class RuleStep:
    """一步检查：跑哪个处理器、在哪个维度上下文里跑。"""

    handler: str
    dimension: str | None = None

    def __post_init__(self) -> None:
        if not str(self.handler or "").strip():
            raise ValueError("RuleStep.handler 不能为空（静默跳过等于少跑一类检查）")


@dataclass(frozen=True, slots=True)
class RuleStepOutcome:
    step: RuleStep
    findings: tuple[Finding, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class RuleAnalysisResult:
    outcomes: tuple[RuleStepOutcome, ...] = ()

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(f for outcome in self.outcomes for f in outcome.findings)

    @property
    def executed(self) -> tuple[str, ...]:
        """按执行顺序返回处理器名（写进 S3 日志的那个列表）。"""
        return tuple(o.step.handler for o in self.outcomes)

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(o.error for o in self.outcomes if o.error)

    def stats(self) -> Mapping[str, Any]:
        return {
            "steps": [
                {
                    "handler": o.step.handler,
                    "dimension": o.step.dimension,
                    "findings": len(o.findings),
                    **({"error": o.error} if o.error else {}),
                }
                for o in self.outcomes
            ],
            "total_findings": len(self.findings),
            "executed": list(self.executed),
        }


class RuleExecutor(Protocol):
    """执行一步检查，返回它新产出的发现。"""

    def __call__(self, step: RuleStep) -> Sequence[Finding]: ...


def plan_rule_steps(
    dimensions: Sequence[str],
    handlers: Mapping[str, str],
    checks: Sequence[str] = (),
) -> tuple[RuleStep, ...]:
    """按「维度处理器（声明顺序）→ 附加检查」生成执行计划。

    纯函数：只排列，不执行。它是「这次审了哪些维度、按什么顺序」的唯一实现，
    旧实现把这段顺序写在 `run_agent_steps` 的循环里，改一处就得读二十行。
    """
    steps: list[RuleStep] = []
    for dimension in dimensions:
        key = str(handlers.get(dimension) or "").strip()
        if not key:
            continue                      # 该维度没有本地处理器 = 只由语义审负责
        steps.append(RuleStep(handler=key, dimension=dimension))
    for key in checks:
        key = str(key or "").strip()
        if not key:
            continue
        steps.append(RuleStep(handler=key, dimension=None))
    return tuple(steps)


async def run_rule_plan(
    steps: Sequence[RuleStep],
    execute: RuleExecutor,
    *,
    on_error: str = "raise",
) -> RuleAnalysisResult:
    """按计划执行，收集每步产出。

    `execute` 允许是同步函数，也允许返回 awaitable（工具注册表里的 `execute`
    是 async 的）。为什么统一成 async 函数：调用方（节点）本来就跑在事件循环里，
    如果这里只肯同步调用，工具层就永远接不进来——只能另开一条同步通道，
    等于承认「工具执行」有两套语义。

    `on_error`：
      * "raise"  —— 一步失败就抛（旧主流程的口径：检查炸了要立刻暴露）
      * "record" —— 记下失败继续（节点的口径：单维度失败不该拖垮整次审查，
                    但全失败必须由调用方判为失败，不能当成「没问题」）
    """
    if on_error not in ("raise", "record"):
        raise ValueError(f"on_error 只能是 raise / record，实际 {on_error!r}")
    outcomes: list[RuleStepOutcome] = []
    for step in steps:
        try:
            produced = execute(step)
            if inspect.isawaitable(produced):
                produced = await produced
            outcomes.append(RuleStepOutcome(step=step, findings=tuple(produced)))
        except Exception as e:                      # noqa: BLE001 - 边界统一转 outcome
            if on_error == "raise":
                raise
            outcomes.append(RuleStepOutcome(
                step=step, error=f"{step.handler}: {type(e).__name__}: {e}"))
    return RuleAnalysisResult(outcomes=tuple(outcomes))


class RuleAnalyzerNode:
    """analysis 阶段（确定性部分）节点：调度检查、收敛发现。"""

    name: Final = NAME
    stage: Final = Stage.ANALYSIS
    produces: Final = frozenset({"findings", "analysis_stats"})

    def __init__(self, executor: RuleExecutor) -> None:
        self.executor = executor

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.ANALYSIS:
            return NodeResult.fail(
                f"{NAME} 只处理 analysis 阶段，当前状态在 {state.stage.value}")
        if not isinstance(state.artifacts.get("target_text"), str):
            return NodeResult.fail(
                "analysis 阶段缺少 artifacts['target_text']：没有原文无从执行规则")

        handlers = ctx.config.get("handlers") or {}
        if not isinstance(handlers, Mapping):
            return NodeResult.fail("config['handlers'] 必须是 mapping（维度 → 处理器名）")
        steps = plan_rule_steps(
            state.profile.dimensions, handlers, ctx.config.get("checks") or ())
        if not steps:
            # 没有可执行的检查 = 这次没做规则审。不能当成「干净」出报告。
            return NodeResult.fail(
                "没有可执行的规则处理器：profile.dimensions 与 manifest 的 handlers 都对不上")

        result = await run_rule_plan(steps, self.executor, on_error="record")
        if result.outcomes and all(not o.ok for o in result.outcomes):
            return NodeResult.fail(
                *result.errors,
                "全部规则处理器失败：不允许以「未发现问题」出报告",
            )
        warnings: tuple[str, ...] = ()
        if result.errors:
            warnings = (f"{len(result.errors)} 个规则处理器失败，相关维度未完成检查："
                        + "；".join(result.errors[:3])[:300],)
        return NodeResult.ok(
            findings=result.findings,
            output={"analysis_stats": dict(result.stats())},
            warnings=warnings,
        )
