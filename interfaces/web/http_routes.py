"""HTTP 路由分发：只决定「这个请求交给谁」，不承载业务判断。

`server.Handler` 只保留传输层（`_send` / `_send_file` / `_guard_api` /
`_json_body`），路径匹配、查询参数提取和 DELETE 的异常兜底集中在这里。
所有业务对象都从构造参数注入：模块不 import server，测试可以用假 Handler
直接验证路由结果。
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

_GET_SIMPLE = {
    "/api/targets": "_targets",
    "/api/reports": "_reports",
    "/api/reports/trash": "_trash_reports",
    "/api/integrity": "_integrity",
    "/api/llm/status": "_llm_status",
    "/api/rules": "_rules",
}

_POST_SIMPLE = {
    "/api/review": "_run_review",
    "/api/review/start": "_start_review",
    "/api/review/cancel": "_cancel_review",
    "/api/annotations": "_add_annotation",
    "/api/candidates/decide": "_decide_candidate",
    "/api/rules/toggle": "_toggle_rule",
    "/api/llm/config": "_save_llm_config",
    "/api/llm/test": "_test_llm",
    "/api/llm/reset": "_reset_llm_config",
    "/api/reports/favorite": "_set_favorite",
    "/api/reports/favorites": "_set_favorites",
    "/api/reports/trash/restore": "_restore_report",
    "/api/target/restore": "_restore_target",
    "/api/llm/history/restore": "_restore_llm_history",
}


def _query(u, name):
    return parse_qs(u.query).get(name, [None])[0]


class HttpRoutes:
    def __init__(self, *, web, root, fingerprint, llm_client,
                 load_llm_history, rule_learning, agent_spec, scenario,
                 format_spec, list_purpose_records, delete_purpose_record):
        self._web = web
        self._root = root
        self._fingerprint = fingerprint
        self._llm_client = llm_client
        self._load_llm_history = load_llm_history
        self._rule_learning = rule_learning
        self._agent_spec = agent_spec
        self._scenario = scenario
        self._format_spec = format_spec
        self._list_purpose_records = list_purpose_records
        self._delete_purpose_record = delete_purpose_record

    def get(self, h):
        u = urlparse(h.path)
        p = u.path
        if p.startswith("/api/") and not h._guard_api():
            return
        if p in ("/", "/index.html"):
            return h._send_file(self._web / "index.html")
        if p.startswith("/static/"):
            fp = (self._web / p[len("/static/"):]).resolve()
            return h._send_file(fp)
        if p == "/api/health":
            return h._send(200, {
                "ok": True,
                "service": "review-agent",
                "pid": os.getpid(),
                "root": str(self._root),
                "fingerprint": self._fingerprint,
            })
        name = _GET_SIMPLE.get(p)
        if name:
            return h._send(200, getattr(h, name)())
        if p == "/api/llm/providers":
            return h._send(200, {"ok": True,
                                 "items": self._llm_client.list_providers()})
        if p == "/api/llm/history":
            return h._send(200, {"ok": True,
                                 "items": self._load_llm_history()})
        if p == "/api/report":
            return h._send(200, h._report(_query(u, "id")))
        if p == "/api/learning":
            # 只读：控制台能看到候选与配额，但**没有任何接口能写规则库**
            return h._send(200, {"ok": True, **self._rule_learning.summary()})
        if p == "/api/candidates":
            # 待人工审核的规则候选（口径与 CLI 的 rule_review.py 完全一致）
            items = self._rule_learning.console_candidates()
            return h._send(200, {
                "ok": True, "count": len(items),
                "pending": sum(1 for i in items if i["pending"]),
                "items": items,
            })
        if p == "/api/annotations":
            rid = _query(u, "id")
            items = [a for a in self._rule_learning.load_annotations()
                     if not rid or a.get("report_id") == rid]
            return h._send(200, {"ok": True, "items": items[-200:]})
        if p == "/api/agents":
            return h._send(200, {"ok": True,
                                 "items": self._agent_spec.list_agents()})
        if p == "/api/scenarios":
            # 配置坏了要让前端看见（400 语义），不能返回空列表：
            # 空列表会被当成"没有情境"，用户在下拉里看不到任何选项也不知道为什么
            try:
                return h._send(200, self._scenario.public_list())
            except self._scenario.ScenarioError as e:
                return h._send(200, {"ok": False, "error": str(e), "items": []})
        if p == "/api/format-specs":
            # 格式规格（docx 版式按哪套标准判）。同 scenarios：配置坏了要报错而不是空列表
            try:
                return h._send(200, self._format_spec.public_list())
            except self._format_spec.FormatSpecError as e:
                return h._send(200, {"ok": False, "error": str(e), "items": []})
        if p == "/api/purpose/files":
            # 暂存的目的文件（说明材料，不是待审对象）；不含原文，只给预览
            return h._send(200, {"ok": True,
                                 "items": self._list_purpose_records()})
        if p == "/api/logs":
            return h._send(200, h._logs(_query(u, "id")))
        if p == "/api/review/status":
            return h._send(200, h._review_status(_query(u, "job")))
        return h._send(404, {"error": "not found"})

    def post(self, h):
        u = urlparse(h.path)
        if not h._guard_api():
            return
        if u.path == "/api/upload":
            return h._send(200, h._upload(u))
        if u.path == "/api/purpose/upload":
            return h._send(200, h._purpose_upload(u))
        data = h._json_body()
        if data is None:            # 超大 body：响应已发出（413），直接结束
            return
        if u.path == "/api/llm/reset":
            # 重置不需要请求体；历史 Handler 方法也是零参签名。
            return h._send(200, h._reset_llm_config())
        name = _POST_SIMPLE.get(u.path)
        if name:
            return h._send(200, getattr(h, name)(data))
        return h._send(404, {"error": "not found"})

    def delete(self, h):
        u = urlparse(h.path)
        if not h._guard_api():
            return
        if u.path == "/api/report":
            rid = _query(u, "id")
            data = h._json_body()
            # 删除是「移文件 + 追加墓碑」两步：任何一步抛异常都要回结构化错误，
            # 不能让异常掀掉连接（旧实现的台账整文件重写在 Windows 上会 PermissionError）。
            try:
                return h._send(200, h._delete_report(rid, data))
            except Exception as exc:                    # noqa: BLE001 - HTTP 边界
                return h._send(500, {"ok": False,
                                     "error": f"删除失败：{type(exc).__name__}: {exc}"})
        if u.path == "/api/target":
            rel = _query(u, "rel")
            try:
                return h._send(200, h._delete_target(rel))
            except Exception as exc:                    # noqa: BLE001 - HTTP 边界
                return h._send(500, {"ok": False,
                                     "error": f"删除失败：{type(exc).__name__}: {exc}"})
        if u.path == "/api/reports/batch":
            data = h._json_body()
            try:
                return h._send(200, h._delete_reports(data))
            except Exception as exc:                    # noqa: BLE001 - HTTP 边界
                return h._send(500, {"ok": False,
                                     "error": f"批量删除失败：{type(exc).__name__}: {exc}"})
        if u.path == "/api/reports/trash":
            rid = _query(u, "id")
            data = h._json_body()
            try:
                result = (h._purge_trash_report(rid, data) if rid
                          else h._empty_trash(data))
                return h._send(200, result)
            except Exception as exc:                    # noqa: BLE001 - HTTP 边界
                return h._send(500, {"ok": False,
                                     "error": f"彻底删除失败：{type(exc).__name__}: {exc}"})
        if u.path == "/api/purpose/files":
            pid = _query(u, "id")
            ok = self._delete_purpose_record(pid)
            if not ok:
                return h._send(200, {"ok": False,
                                     "error": f"未找到目的文件：{pid or '（空 id）'}"})
            return h._send(200, {"ok": True, "id": pid,
                                 "items": self._list_purpose_records()})
        return h._send(404, {"error": "not found"})
