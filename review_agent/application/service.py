# -*- coding: utf-8 -*-
"""Application 层：把「一次审查」组装成可执行、可保存、可恢复的服务。

它不产生结论（结论属于节点），也不读文件（读取经 `reader` 端口注入），只做四件事：

    1. 组装：把六个固定节点 + 可选的语义审 / 库外核验拼成一条 Workflow
    2. 执行：交给 Orchestrator，按阶段推进、失败停住、可重试才重试
    3. 留痕：可选写 `FileRunStore`（状态）与检查点（阶段 + 哈希）
    4. 恢复：按 run_id 读回状态，从断点阶段继续

为什么放到 application 而不是继续写在 `review.py` 里：
    CLI、server、测试三条入口都需要同一套「组装 + 保存 + 恢复」逻辑。
    塞回旧脚本的后果是每加一个入口就复制一遍，恢复语义各写一份。

边界（v1 明确不做）：
    * 不重写报告写出（哈希链 / ledger / Markdown 仍由旧 `run_one` 负责）
    * 不做请求持久化：`resume()` 需要调用方用同一份请求重建 ports（agent / 规则集）
    * 语义审的入队（去重 / 语境豁免 / make_issue）仍在旧链路，这里只提供节点位
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..domain import ReviewProfile, Stage
from ..runtime import (
    FileRunStore,
    Node,
    Orchestrator,
    RunOutcome,
    RunState,
    Workflow,
    state_to_dict,
)
from ..runtime.nodes import (
    CrossReviewerNode,
    DecisionEngineNode,
    DocumentAnalyzerNode,
    EvidenceAnalyzerNode,
    EvidenceAssessorNode,
    FactCheckNode,
    PlannerNode,
    RuleAnalyzerNode,
    SemanticReviewerNode,
)
from ..runtime.nodes.document import Reader
from ..runtime.nodes.research import ResearchExecutor
from ..runtime.nodes.rules import RuleExecutor
from ..runtime.nodes.semantic import SemanticExecutor


@dataclass(frozen=True, slots=True)
class PipelinePorts:
    """一次执行需要的全部外部能力（都从外面注入，Application 不直接读文件/调模型）。"""

    reader: Reader
    rule_executor: RuleExecutor
    finalize_node: Node
    semantic_executor: SemanticExecutor | None = None
    # 库外事实核验（联网）：None = 本次不做（与 `--no-research` 等价）
    research_executor: ResearchExecutor | None = None


class _StatePersistingCheckpoints:
    """在每次阶段检查点写入前保存完整 RunState，供进程重启后恢复。"""

    def __init__(self, inner, store: FileRunStore) -> None:
        self._inner = inner
        self._store = store

    def append(self, checkpoint) -> None:
        self._inner.append(checkpoint)

    def for_run(self, run_id: str):
        return self._inner.for_run(run_id)

    def save_state(self, state: RunState) -> None:
        self._store.save(state)


class ReviewApplication:
    """组装 + 执行 + 保存 + 恢复。"""

    def __init__(self, *, store: FileRunStore | None = None,
                 checkpoints=None, max_attempts: int = 1,
                 node_timeout: float | None = None,
                 token=None) -> None:
        self.store = store
        self.checkpoints = checkpoints
        self.max_attempts = max(1, int(max_attempts))
        # 单节点上限；None = 不限制（默认不改变既有行为，由入口显式打开）
        self.node_timeout = node_timeout
        # 取消令牌（`runtime.cancel.CancellationToken`）：None = 不可取消，与改造前一致。
        # 取消只在阶段/节点边界生效，理由见 runtime/cancel.py 的模块说明。
        self.token = token

    # ---- 组装 ----
    def workflow(self, ports: PipelinePorts) -> Workflow:
        """固定节点 + 两个可选节点（语义审、库外核验）。

        顺序不可颠倒（两次都是「先有上游产物、才有下游判断」）：

            analysis：规则审 → 语义审 → 库外核验
                · 库外核验要看到语义审已经报过什么（同一句既被模型报过、又被
                  外部来源反驳时，先有的那条才有编号，避免同句两条）
                · 反过来把核验放在语义审之前，语义审就会重复报「这句缺出处」

            evidence：引用锚定 → 强度评估 → 裁定
                锚定只看 finding.quote，外部来源证据（EXTERNAL_SOURCE）不参与
                逐字回查——它的引用来自别人网页，不是待审原文。
        """
        nodes: list[Node] = [
            DocumentAnalyzerNode(reader=ports.reader),
            PlannerNode(),
            RuleAnalyzerNode(executor=ports.rule_executor),
        ]
        if ports.semantic_executor is not None:
            nodes.append(SemanticReviewerNode(executor=ports.semantic_executor))
        if ports.research_executor is not None:
            nodes.append(FactCheckNode(executor=ports.research_executor))
        nodes += [
            EvidenceAnalyzerNode(),
            EvidenceAssessorNode(),
            CrossReviewerNode(),
            DecisionEngineNode(),
            ports.finalize_node,
        ]
        return Workflow(tuple(nodes))

    # ---- 状态 ----
    def initial_state(self, *, review_id: str, run_id: str,
                      profile: ReviewProfile,
                      target_hash: str | None = None) -> RunState:
        """从 intake 之后开始：`review_id` / `target_hash` 由调用方先算好。

        为什么不让节点做 intake：intake 的产物是「身份 + 输入指纹」，
        没有解析动作，塞成节点只会多一层没有行为的包装。
        """
        artifacts: dict[str, Any] = {}
        if target_hash:
            artifacts["target_hash"] = target_hash
        return RunState(
            review_id=review_id, run_id=run_id, stage=Stage.INGESTION,
            profile=profile, completed_stages=(Stage.INTAKE,),
            artifacts=artifacts,
        )

    # ---- 执行 / 恢复 ----
    async def run(self, state: RunState, ports: PipelinePorts,
                  *, config: Mapping[str, Any] | None = None) -> RunOutcome:
        workflow = self.workflow(ports)
        checkpoints = self._checkpoint_store()
        outcome = await Orchestrator(
            workflow, checkpoints=checkpoints,
            max_attempts=self.max_attempts, config=config or {},
            timeout=self.node_timeout, token=self.token,
        ).run(state)
        self._save(outcome.state)
        return outcome

    async def resume(self, run_id: str, ports: PipelinePorts,
                     *, config: Mapping[str, Any] | None = None) -> RunOutcome | None:
        """按 run_id 读回状态并从断点阶段继续；没有记录时返回 None。"""
        state = self.state_of(run_id)
        if state is None:
            return None
        return await self.run(state, ports, config=config)

    def state_of(self, run_id: str) -> RunState | None:
        return self.store.load(run_id) if self.store is not None else None

    def snapshot(self, state: RunState) -> dict[str, Any]:
        """状态的可序列化视图（给 server / 审计用，不落盘）。"""
        return state_to_dict(state)

    def _save(self, state: RunState) -> None:
        if self.store is not None:
            self.store.save(state)

    def _checkpoint_store(self):
        if self.checkpoints is None or self.store is None:
            return self.checkpoints
        if callable(getattr(self.checkpoints, "save_state", None)):
            return self.checkpoints
        return _StatePersistingCheckpoints(self.checkpoints, self.store)
