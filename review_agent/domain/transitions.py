# -*- coding: utf-8 -*-
"""状态机：谁可以迁移到谁，以及每个阶段必须产出什么。

为什么需要显式状态机：
    旧实现的「状态」是若干布尔组合（有没有 report？有没有 verdict？job 是否在跑？），
    于是出现「报告已写、ledger 还没写」「job 死了但状态是 running」这类中间态，
    server 侧靠 `_recover_jobs` 试探着修。显式迁移表把「合法的中间态」定死，
    恢复逻辑才有依据。

八个阶段（Review Workflow v1）与旧 S1~S6 的映射见 enums.Stage 的文档字符串。
阶段**只能前进**：`ensure_stage_advance` 允许原地重试（target == current），
不允许回退——回退意味着「重跑上一阶段」，那应该产生新的 Run，而不是改旧 Run 的阶段。
"""
from __future__ import annotations

from collections.abc import Mapping

from .enums import (
    DisputeStatus,
    FindingStatus,
    ReviewStatus,
    RunStatus,
    Stage,
)
from .errors import InvalidTransition

RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    # CREATED 允许直接 → RUNNING：同步入口（`TaskService.start_review`）不强迫先入队。
    # 队列是**可选能力**，不是必由之路——把同步路径也改成两步只会多一次状态写入，
    # 换不来任何可观测性。
    RunStatus.CREATED: frozenset({
        RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.ABORTED,
    }),
    RunStatus.QUEUED: frozenset({
        RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.ABORTED,
    }),
    RunStatus.RUNNING: frozenset({
        RunStatus.AWAITING_DECISION, RunStatus.COMPLETED,
        RunStatus.FAILED, RunStatus.ABORTED, RunStatus.CANCELLED,
    }),
    RunStatus.AWAITING_DECISION: frozenset({
        RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ABORTED,
        RunStatus.CANCELLED,
    }),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.ABORTED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

REVIEW_TRANSITIONS: Mapping[ReviewStatus, frozenset[ReviewStatus]] = {
    ReviewStatus.CREATED: frozenset({ReviewStatus.IN_PROGRESS, ReviewStatus.ARCHIVED}),
    ReviewStatus.IN_PROGRESS: frozenset({ReviewStatus.COMPLETED, ReviewStatus.ARCHIVED}),
    ReviewStatus.COMPLETED: frozenset({ReviewStatus.ARCHIVED}),
    ReviewStatus.ARCHIVED: frozenset(),
}

FINDING_TRANSITIONS: Mapping[FindingStatus, frozenset[FindingStatus]] = {
    FindingStatus.PROPOSED: frozenset({
        FindingStatus.NEEDS_CONFIRMATION, FindingStatus.CONFIRMED,
        FindingStatus.REJECTED, FindingStatus.SUPPRESSED,
    }),
    FindingStatus.NEEDS_CONFIRMATION: frozenset({
        FindingStatus.CONFIRMED, FindingStatus.REJECTED, FindingStatus.SUPPRESSED,
    }),
    FindingStatus.CONFIRMED: frozenset(),
    FindingStatus.REJECTED: frozenset(),
    FindingStatus.SUPPRESSED: frozenset(),
}

DISPUTE_TRANSITIONS: Mapping[DisputeStatus, frozenset[DisputeStatus]] = {
    DisputeStatus.OPEN: frozenset({DisputeStatus.RESOLVED, DisputeStatus.ESCALATED}),
    DisputeStatus.ESCALATED: frozenset({DisputeStatus.RESOLVED}),
    DisputeStatus.RESOLVED: frozenset(),
}

# 阶段完成的证据：没有产出就不允许推进。取的是「状态里必须存在的键」，
# 具体类型由 runtime 的 RunState 定义；这里是协议层面的约定。
#
# `analysis` 多了一个 `research`：库外事实核验（FactCheckNode）在 analysis 阶段
# 产出**结论块**（`{"enabled": true, "refuted": [...], ...}`），不能塞进
# `analysis_stats`（那是规则审的统计）也不能塞进 typed 字段（它不是发现也不是证据）。
# 这一项只是**允许写入**：是否必填由节点自己声明（`Node.produces`），
# 所以没开 `--research` 时不会因为缺它而推进失败。
STAGE_ARTIFACTS: Mapping[Stage, tuple[str, ...]] = {
    Stage.INTAKE: ("review_id", "target_hash"),
    Stage.INGESTION: ("target_text", "target_meta"),
    Stage.PLANNING: ("plan",),
    Stage.ANALYSIS: ("findings", "analysis_stats", "semantic_stats", "research"),
    Stage.EVIDENCE: ("evidence", "evidence_dropped"),
    Stage.CROSS_REVIEW: ("disputes",),
    Stage.DECISION: ("decision",),
    Stage.FINALIZE: ("report", "audit_export"),
}

STAGE_ORDER: tuple[Stage, ...] = (
    Stage.INTAKE, Stage.INGESTION, Stage.PLANNING, Stage.ANALYSIS,
    Stage.EVIDENCE, Stage.CROSS_REVIEW, Stage.DECISION, Stage.FINALIZE,
)


def _ensure(kind: str, current, target, table: Mapping) -> None:
    allowed = table.get(current)
    if allowed is None:
        raise InvalidTransition(f"未知的 {kind} 状态：{current!r}")
    if target not in allowed:
        names = "、".join(sorted(str(x) for x in allowed)) or "（终态，无处可去）"
        raise InvalidTransition(
            f"{kind} 不允许从 {current} 迁移到 {target}；允许的去向：{names}")


def can_run_transition(current: RunStatus, target: RunStatus) -> bool:
    return target in RUN_TRANSITIONS.get(current, frozenset())


def ensure_run_transition(current: RunStatus, target: RunStatus) -> None:
    _ensure("Run", current, target, RUN_TRANSITIONS)


def ensure_review_transition(current: ReviewStatus, target: ReviewStatus) -> None:
    _ensure("Review", current, target, REVIEW_TRANSITIONS)


def ensure_finding_transition(current: FindingStatus, target: FindingStatus) -> None:
    _ensure("Finding", current, target, FINDING_TRANSITIONS)


def ensure_dispute_transition(current: DisputeStatus, target: DisputeStatus) -> None:
    _ensure("Dispute", current, target, DISPUTE_TRANSITIONS)


def next_stage(stage: Stage) -> Stage:
    idx = STAGE_ORDER.index(stage)
    if idx == len(STAGE_ORDER) - 1:
        raise InvalidTransition("FINALIZE 已是最后阶段，没有下一阶段")
    return STAGE_ORDER[idx + 1]


def ensure_stage_advance(current: Stage, target: Stage) -> None:
    """允许原地重试（target == current），不允许跳级或回退。

    跳级要禁止：跳过 EVIDENCE 直接 CROSS_REVIEW，等于「没校验证据的结论参与了冲突判定」。
    """
    if target.order < current.order:
        raise InvalidTransition(
            f"阶段不能回退：{current} → {target}（重跑请新建 Run）")
    if target.order > current.order + 1:
        raise InvalidTransition(
            f"阶段不能跳级：{current} → {target}（必须逐阶段推进）")


def recoverable(status: RunStatus | str, *, has_checkpoint: bool,
                has_live_executor: bool) -> bool:
    """这个任务还能不能接着跑：**判断**，不是状态。

    三个条件缺一不可：
      · 状态是 `running`（`queued` 还没开始，不需要"续"，直接开始就行；
        终态没什么可续，重审要新建 Run）；
      · 有检查点（没有检查点就不知道从哪个阶段继续，只能整单重跑）；
      · 没有活着的执行者（进程还在跑时"恢复"会变成两个执行者写同一份状态）。

    为什么不做成 `RunStatus.RECOVERABLE`：状态是**事实**，可恢复性是**判断**。
    一旦写成状态，就会出现「记录说可恢复，但检查点已被 retention 清掉」这种
    两套真相，而恢复逻辑不知道信谁。判断可以随时按当前事实重算，状态不行。
    """
    try:
        current = RunStatus(status)
    except ValueError:
        return False
    return (current is RunStatus.RUNNING
            and bool(has_checkpoint)
            and not has_live_executor)
