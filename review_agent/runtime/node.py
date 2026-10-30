# -*- coding: utf-8 -*-
"""Agent Runtime v1 的最小骨架：状态、节点协议、阶段推进。

这个文件**不实现调度器**（Planner / Executor / Supervisor 的并发、重试、超时
在阶段 2 落地）。它只固定三件必须先定下来的事：

    1. RunState 长什么样 —— 阶段、typed 结论集合、阶段产物，全部不可变
    2. 节点契约长什么样 —— 一个节点只跑一个阶段，只返回自己产出的东西
    3. 阶段怎么推进 —— 只能逐阶段前进；失败不得推进；产物缺失不得推进

为什么先定这三件事：
    旧实现里 run_one 是一个 900 行函数，S1~S6 之间靠局部变量和 Session 的
    mutable 字段传递状态，「某一步失败后已产出的东西是否可信」没有统一判断。
    把契约先钉死，阶段 2 才能安全地把 S1~S6 逐段包成节点，而不是边写边改契约。

与 docs/AGENT_RUNTIME.md 的对应关系：
    Node / NodeContext / NodeResult  → 六个专业节点（Document Analyzer…Decision Engine）
    RunState                        → Agent State
    advance()                       → Supervisor 的单步推进（含前置校验）
    Workflow                        → 阶段编排（哪个阶段跑哪个节点）
"""
from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ..domain import (
    STAGE_ARTIFACTS,
    STAGE_ORDER,
    Decision,
    Dispute,
    Evidence,
    Finding,
    InvalidTransition,
    InvariantViolation,
    ReviewProfile,
    Stage,
    ensure_stage_advance,
    next_stage,
)
from ..domain import ids as id_tools

# 这些产物由 typed 字段承载，不要求在 artifacts 里再放一份
_TYPED_ARTIFACTS = frozenset({"review_id", "findings", "evidence", "disputes", "decision"})


def _freeze(mapping: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True, slots=True)
class RunState:
    """一次执行的完整状态（不可变）。

    artifacts 放「阶段产物」：target_text / target_meta / plan / evidence_dropped / report …
    键名由 domain.transitions.STAGE_ARTIFACTS 登记，不在登记表里的键不允许写入
    （允许出错比允许随意塞好排查：写错键名会立刻报错，而不是半年后没人知道 report 在哪）。
    """

    review_id: str
    run_id: str
    stage: Stage
    profile: ReviewProfile
    completed_stages: tuple[Stage, ...] = ()
    findings: tuple[Finding, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    disputes: tuple[Dispute, ...] = ()
    decision: Decision | None = None
    artifacts: Mapping[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    attempt: int = 1

    def __post_init__(self) -> None:
        id_tools.parse_review_id(self.review_id)
        run_review, _ = id_tools.parse_run_id(self.run_id)
        if run_review != self.review_id:
            raise InvariantViolation(
                f"run_id {self.run_id} 与 review_id {self.review_id} 不匹配")
        expected = self._expected_stage()
        if self.stage is not expected:
            raise InvariantViolation(
                f"state.stage={self.stage}，但按已完成阶段推断应为 {expected}")
        if self.attempt < 1:
            raise InvariantViolation("attempt 从 1 开始")
        object.__setattr__(self, "artifacts", _freeze(self.artifacts))

    def _expected_stage(self) -> Stage:
        done = len(self.completed_stages)
        if done > len(STAGE_ORDER):
            raise InvariantViolation(f"completed_stages 超过 {len(STAGE_ORDER)} 个阶段")
        if tuple(self.completed_stages) != STAGE_ORDER[:done]:
            raise InvariantViolation(
                "completed_stages 必须是从 INTAKE 开始的连续前缀，"
                f"实际 {[s.value for s in self.completed_stages]}")
        return STAGE_ORDER[done] if done < len(STAGE_ORDER) else Stage.FINALIZE

    def missing_artifacts(self, stage: Stage | None = None) -> tuple[str, ...]:
        """当前阶段还缺哪些产物（用于推进前的校验与排障）。"""
        stage = stage or self.stage
        return tuple(
            key for key in STAGE_ARTIFACTS[stage]
            if key not in _TYPED_ARTIFACTS and key not in self.artifacts
        )


@dataclass(frozen=True, slots=True)
class NodeContext:
    """节点入参：只给节点它该看到的东西。

    注意没有 `session`、没有全局可写对象：节点拿不到别的阶段的数据，
    也就无法「顺手改一下上游结论」——这正是旧实现最常出问题的地方。

    `next_finding_seq`：本节点如果要**新建**发现，必须从这个序号起号
    （`ids.format_finding_id(ctx.next_finding_seq + i)`）。编号是 Run 级资源，
    由编排器按「已有发现的最大号 + 1」发放；同阶段两个节点各从 ISS-0001 起号
    会被 `merge_results()` 直接拦住。节点自己不许猜，也不许重排已有编号。
    """

    state: RunState
    deadline: datetime.datetime | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    next_finding_seq: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "config", _freeze(self.config))
        if self.next_finding_seq < 1:
            raise InvariantViolation("next_finding_seq 从 1 开始")


@dataclass(frozen=True, slots=True)
class NodeResult:
    """节点出参：成功/失败 + 本阶段产物 + 已定型的领域对象。

    success=False 必须带 errors：空错误的失败等于「不知道为什么挂了」，
    恢复逻辑没法据此决定重试还是终止。

    `revisions` 是本阶段对**上游已有发现**的修订（例如交叉复核给冲突双方降权）。
    为什么允许：发现不可变，但真实流程里「复核之后结论被调整」是客观存在的；
    与其让节点偷改上游对象（旧实现就是这么把 0.75 改成 0.70 且查不出原因的），
    不如把修订摆到台面上——带 id、带新值，由 `advance()` 按 id 替换，
    同一阶段既新增又修订同一个 id 会被直接判错。
    """

    success: bool
    output: Mapping[str, Any] = field(default_factory=dict)
    findings: tuple[Finding, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    disputes: tuple[Dispute, ...] = ()
    decision: Decision | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    retryable: bool = False
    revisions: tuple[Finding, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", _freeze(self.output))
        if not self.success and not self.errors:
            raise InvariantViolation("失败的 NodeResult 必须说明原因（errors 不能为空）")

    @classmethod
    def ok(cls, **kwargs: Any) -> "NodeResult":
        return cls(success=True, **kwargs)

    @classmethod
    def fail(cls, *errors: str, retryable: bool = False) -> "NodeResult":
        return cls(success=False, errors=tuple(errors), retryable=retryable)


@runtime_checkable
class Node(Protocol):
    """一个阶段节点。一个节点只负责一个阶段，只产出自己声明的产物。"""

    name: str
    stage: Stage
    produces: frozenset[str]

    async def run(self, ctx: NodeContext) -> NodeResult:
        ...


@dataclass(frozen=True, slots=True)
class Workflow:
    """阶段 → 节点 的编排表。

    约束（故意比「一个阶段一个节点」松，比「随便排」紧）：

    * 同一个阶段**可以**挂多个节点，按声明顺序执行——analysis 就是这种情况：
      规则审（确定性）与语义审（模型）是两件独立的事，合并成一个节点会让
      「哪一半没跑成」说不清。
    * 同一阶段的节点必须连续出现（中间不许插别的阶段），否则「阶段内顺序」
      就没有意义了。
    * 阶段之间只能按 STAGE_ORDER 前进。
    * 节点重名、同阶段内重复声明同一节点都直接失败。
    * 允许只编排部分阶段（测试、灰度接管用）；没编排到的阶段由 Orchestrator
      报「未覆盖」，不允许悄悄跳过。
    """

    nodes: tuple[Node, ...]

    def __post_init__(self) -> None:
        highest = -1
        seen_stage: Stage | None = None
        closed: set[Stage] = set()
        names: set[str] = set()
        for node in self.nodes:
            if node.name in names:
                raise InvariantViolation(f"节点重名：{node.name}")
            names.add(node.name)
            if node.stage is not seen_stage:
                if node.stage in closed:
                    raise InvariantViolation(
                        f"阶段 {node.stage} 的节点必须连续出现，不允许被其它阶段隔开")
                if node.stage.order < highest:
                    raise InvariantViolation(
                        f"节点顺序与阶段顺序不一致：{node.stage} 排在更晚的阶段之后")
                if seen_stage is not None:
                    closed.add(seen_stage)
                highest = node.stage.order
                seen_stage = node.stage

    def nodes_for(self, stage: Stage) -> tuple[Node, ...]:
        return tuple(n for n in self.nodes if n.stage is stage)

    def node_for(self, stage: Stage) -> Node | None:
        nodes = self.nodes_for(stage)
        return nodes[0] if nodes else None

    @property
    def stages(self) -> tuple[Stage, ...]:
        """按出现顺序去重的阶段列表。"""
        out: list[Stage] = []
        for node in self.nodes:
            if not out or out[-1] is not node.stage:
                out.append(node.stage)
        return tuple(out)


def surviving_findings(state: RunState) -> tuple[Finding, ...]:
    """本次执行里「仍然算数」的发现。

    evidence 阶段的产物 `evidence_dropped` 列出了引用无法回查原文的发现——
    它们必须移出报告，也不能参与交叉复核与裁定。状态本身是只增的（发现不可变），
    所以"删除"表现为：后续阶段按这份清单把它们排除掉。
    """
    dropped = {item.get("finding_id")
               for item in (state.artifacts.get("evidence_dropped") or ())
               if isinstance(item, Mapping)}
    if not dropped:
        return state.findings
    return tuple(f for f in state.findings if f.id not in dropped)


def stage_requirements(nodes: Sequence[Node]) -> frozenset[str]:
    """这一阶段的全部节点合起来必须产出哪些**非 typed** 产物。

    typed 产物（findings / evidence / disputes / decision / review_id）走
    NodeResult 的字段，不在 artifacts 里重复存一份。
    """
    required = {key for node in nodes for key in node.produces}
    return frozenset(key for key in required if key not in _TYPED_ARTIFACTS)


def merge_results(results: Sequence[NodeResult]) -> NodeResult:
    """把同一阶段多个节点的结果合成一个，交给 advance() 推进一次。

    为什么必须合成：阶段的「完成」是一次状态迁移，不是每个节点各推一次；
    否则 completed_stages 会出现重复项，阶段也就不再是「连续前缀」。
    """
    if not results:
        raise InvariantViolation("没有任何节点结果，无法推进阶段")
    failed = [r for r in results if not r.success]
    if failed:
        raise InvalidTransition(
            "存在失败的节点结果，不得推进："
            + "；".join(e for r in failed for e in r.errors))
    decisions = [r for r in results if r.decision is not None]
    if len(decisions) > 1:
        raise InvariantViolation("同一阶段出现了多个裁定：一个阶段只能有一个 Decision")

    output: dict[str, Any] = {}
    for result in results:
        for key, value in result.output.items():
            if key in output:
                raise InvariantViolation(
                    f"同一阶段的两个节点都产出了 {key!r}：产物键必须唯一，"
                    "否则谁覆盖谁取决于声明顺序")
            output[key] = value

    findings = tuple(f for r in results for f in r.findings)
    seen: set[str] = set()
    for finding in findings:
        if finding.id in seen:
            raise InvariantViolation(
                f"同一阶段产出重复的发现编号 {finding.id}："
                "节点必须从同一个编号分配器取号（见 AGENT_RUNTIME §11 开放问题）")
        seen.add(finding.id)

    return NodeResult(
        success=True,
        output=output,
        findings=findings,
        evidence=tuple(e for r in results for e in r.evidence),
        disputes=tuple(d for r in results for d in r.disputes),
        decision=decisions[0].decision if decisions else None,
        warnings=tuple(w for r in results for w in r.warnings),
        revisions=tuple(rev for r in results for rev in r.revisions),
    )


def advance(state: RunState, stage: Stage, result: NodeResult, *,
            required: frozenset[str] | None = None) -> RunState:
    """推进一个阶段。

    校验顺序刻意如此：
        1. 阶段对不对（防止节点串台）
        2. 结果成不成功（失败绝不推进）
        3. 声明的产物齐不齐（防止「成功了但什么都没产出」）
        4. run_id 一致（防止把别的执行的结论并进来）
    任何一条不过都抛异常，而不是记一条 warning 后继续 —— 半成品状态比失败更难查。

    `required`：这一阶段实际必须产出哪些非 typed 产物。
    默认按 `STAGE_ARTIFACTS[stage]` 全集要求；一阶段多节点时由 Orchestrator
    传入「这几个节点 produces 的并集」，否则单挂 Rule Analyzer 的 analysis 阶段
    会被要求提供语义审才有的产物。`STAGE_ARTIFACTS` 仍然是**允许写入**的白名单，
    不因为部分编排而放宽。
    """
    if stage is not state.stage:
        raise InvalidTransition(
            f"当前应执行 {state.stage}，但收到了 {stage} 的结果")
    if not result.success:
        raise InvalidTransition(
            f"阶段 {stage} 失败，不得推进：{'；'.join(result.errors)}")
    ensure_stage_advance(state.stage, stage)
    unknown = [key for key in result.output if key not in STAGE_ARTIFACTS[stage]]
    if unknown:
        raise InvariantViolation(
            f"阶段 {stage} 产出了未登记的产物：{unknown}；"
            f"允许的键：{list(STAGE_ARTIFACTS[stage])}")
    needed = required if required is not None else frozenset(
        key for key in STAGE_ARTIFACTS[stage] if key not in _TYPED_ARTIFACTS)
    for key in needed:
        if key in _TYPED_ARTIFACTS:
            continue
        if key not in state.artifacts and key not in result.output:
            raise InvariantViolation(
                f"阶段 {stage} 缺少必需产物 {key!r}（节点声明或 STAGE_ARTIFACTS 规定）")
    for name in ("findings", "evidence", "disputes"):
        for item in getattr(result, name):
            if item.run_id != state.run_id:
                raise InvariantViolation(
                    f"{item.id} 属于 run {item.run_id}，不能并入 {state.run_id}")
    # 证据必须指向本次执行里真实存在的发现：悬空引用比没有引用更难查
    # （报告上看起来「有证据」，点进去是一条不存在的 ISS 号）。
    known = {f.id for f in (*state.findings, *result.findings)}
    for ev in result.evidence:
        for fid in ev.finding_ids:
            if fid not in known:
                raise InvariantViolation(
                    f"证据 {ev.id} 指向不存在的发现 {fid}")
    if result.decision is not None and result.decision.run_id != state.run_id:
        raise InvariantViolation(
            f"Decision {result.decision.id} 属于 run {result.decision.run_id}")
    # 修订只能改「上游已有的发现」：同一阶段既新增又修订同一 id，
    # 说明节点对同一条结论给了两版，必须先在节点内部收敛。
    added = {f.id for f in result.findings}
    upstream = {f.id for f in state.findings}
    for rev in result.revisions:
        if rev.run_id != state.run_id:
            raise InvariantViolation(
                f"修订 {rev.id} 属于 run {rev.run_id}，不能并入 {state.run_id}")
        if rev.id in added:
            raise InvariantViolation(
                f"{rev.id} 在同一阶段既被新增又被修订：节点须先收敛成一条")
        if rev.id not in upstream:
            raise InvariantViolation(
                f"修订了不存在的发现 {rev.id}：只能修订上游已产出的结论")

    done = state.completed_stages + (stage,)
    nxt = next_stage(stage) if stage is not Stage.FINALIZE else Stage.FINALIZE
    return replace(
        state,
        stage=nxt,
        completed_stages=done,
        findings=_apply_revisions(state.findings, result.revisions) + result.findings,
        evidence=state.evidence + result.evidence,
        disputes=state.disputes + result.disputes,
        decision=result.decision or state.decision,
        artifacts={**state.artifacts, **result.output},
        warnings=state.warnings + result.warnings,
    )


def _apply_revisions(findings: tuple[Finding, ...],
                     revisions: tuple[Finding, ...]) -> tuple[Finding, ...]:
    """按 id 替换，保持原顺序（顺序变了，报告的条目次序就变了）。"""
    by_id = {rev.id: rev for rev in revisions}
    return tuple(by_id.get(f.id, f) for f in findings)
