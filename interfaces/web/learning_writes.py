"""server 上仅有的两个「学习」写入口。

标注与候选审核都只写学习数据（annotations / candidates），**不写规则库**；
规则库的写入仍然只存在于 tools/rule_promote.py 且需要审批。把这两个入口
从 HTTP Handler 拆出来后，Token 鉴权顺序、错误文案和返回结构都集中在一处。
"""

from __future__ import annotations

import os


class LearningWrites:
    def __init__(self, *, rule_learning, token_env, headers, env=None):
        self._rule_learning = rule_learning
        self._token_env = token_env
        self._headers = headers
        self._env = os.environ if env is None else env

    def _token_required(self) -> str:
        return str(self._env.get(self._token_env, "") or "").strip()

    def _authorized(self) -> bool:
        need = self._token_required()
        if not need:
            return True
        return self._headers.get("X-Review-Token", "") == need

    # ------------------------------------------------------------------
    # 人工标注
    # ------------------------------------------------------------------
    def add_annotation(self, data):
        """记录人工标注（可选鉴权 + 作者白名单）。"""
        if not self._authorized():
            # 开放部署时才启用；单机默认不设这个变量，控制台照常可用。
            # 注意：即使鉴权通过，能否作为学习种子还取决于 config 里的作者白名单。
            return {"ok": False,
                    "error": "标注需要 X-Review-Token 头（服务端已启用标注鉴权）"}
        return self.add_annotation_inner(data)

    def add_annotation_inner(self, data):
        """记录人工标注。

        这是整个 HTTP 面上唯一与「学习」有关的写操作，而且它**只能写标注**：
        规则库的写入只存在于 tools/rule_promote.py 且需要审批。
        被审文档因此没有任何路径能影响自己的审查规则。
        """
        d = data or {}
        try:
            item = self._rule_learning.add_annotation(
                kind=str(d.get("kind") or ""),
                by=str(d.get("by") or "console"),
                target_path=d.get("target_path"),
                quote=d.get("quote"),
                dimension=d.get("dimension"),
                report_id=d.get("report_id"),
                issue_id=d.get("issue_id"),
                note=str(d.get("note") or ""),
            )
        except self._rule_learning.LearningError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "annotation": item}

    # ------------------------------------------------------------------
    # 候选审核
    # ------------------------------------------------------------------
    def decide_candidate(self, data):
        """人工审核决定：**只写候选文件的 human_review，不动规则库**。

        这是 HTTP 面上第二个与「学习」有关的写操作（第一个是写标注）。
        写规则库依然只存在于 tools/rule_promote.py —— 控制台没有那条路径。
        """
        if not self._authorized():
            return {"ok": False,
                    "error": "审核决定需要 X-Review-Token 头（服务端已启用鉴权）"}
        d = data or {}
        try:
            r = self._rule_learning.decide_candidate(
                d.get("candidate_id"),
                str(d.get("decision") or ""),
                note=str(d.get("note") or ""),
                by=str(d.get("by") or "console"),
            )
        except self._rule_learning.LearningError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **r}
