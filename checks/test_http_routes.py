"""HTTP 路由分发只决定「交给谁」：路径、查询参数、异常兜底。"""

import unittest
from pathlib import Path

from interfaces.web import http_routes


class _Scenario:
    class ScenarioError(Exception):
        pass

    def public_list(self):
        return {"ok": True, "items": []}


class _FormatSpec:
    class FormatSpecError(Exception):
        pass

    def public_list(self):
        return {"ok": True, "items": []}


class _FakeHandler:
    def __init__(self, path, *, guarded=True):
        self.path = path
        self.guarded = guarded
        self.sent = []
        self.calls = []
        self.json_reads = 0

    def _guard_api(self):
        self.calls.append(("_guard_api", ()))
        return self.guarded

    def _send(self, code, body):
        self.sent.append((code, body))
        return body

    def _send_file(self, path):
        self.calls.append(("_send_file", (path,)))
        return True

    def _json_body(self):
        self.json_reads += 1
        return {}

    def _report(self, rid):
        self.calls.append(("_report", (rid,)))
        return {"ok": True, "id": rid}

    def _upload(self, u):
        self.calls.append(("_upload", (u.query,)))
        return {"ok": True}

    def _delete_report(self, rid, data):
        raise PermissionError("台账被占用")

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            return {"ok": True, "route": name}
        return call


def _routes():
    return http_routes.HttpRoutes(
        web=Path("web"),
        root=Path("root"),
        fingerprint="fp",
        llm_client=type("L", (), {"list_providers": lambda self: []})(),
        load_llm_history=lambda: [],
        rule_learning=type("R", (), {
            "summary": lambda self: {"ok": True},
            "console_candidates": lambda self: [],
            "load_annotations": lambda self: [],
        })(),
        agent_spec=type("A", (), {"list_agents": lambda self: []})(),
        scenario=_Scenario(),
        format_spec=_FormatSpec(),
        list_purpose_records=lambda: [],
        delete_purpose_record=lambda pid: True,
    )


class HttpRouteTests(unittest.TestCase):
    def test_get_health_uses_injected_context(self):
        h = _FakeHandler("/api/health")
        _routes().get(h)
        code, body = h.sent[0]
        self.assertEqual(code, 200)
        self.assertEqual(body["root"], "root")
        self.assertEqual(body["fingerprint"], "fp")

    def test_get_report_extracts_query_id(self):
        h = _FakeHandler("/api/report?id=R-1")
        _routes().get(h)
        self.assertEqual(h.sent[0][0], 200)
        self.assertIn(("_report", ("R-1",)), h.calls)

    def test_post_upload_does_not_parse_json_body(self):
        h = _FakeHandler("/api/upload?name=x.txt")
        _routes().post(h)
        self.assertEqual(h.sent[0][0], 200)
        self.assertEqual(h.json_reads, 0)

    def test_post_unknown_reads_json_then_404(self):
        h = _FakeHandler("/api/no-such-route")
        _routes().post(h)
        self.assertEqual(h.json_reads, 1)
        self.assertEqual(h.sent[0][0], 404)

    def test_delete_exception_becomes_structured_500(self):
        h = _FakeHandler("/api/report?id=R-1")
        _routes().delete(h)
        code, body = h.sent[0]
        self.assertEqual(code, 500)
        self.assertIn("删除失败", body["error"])
        self.assertIn("PermissionError", body["error"])

    def test_api_request_stops_when_guard_rejects(self):
        h = _FakeHandler("/api/targets", guarded=False)
        _routes().get(h)
        self.assertEqual(h.sent, [])


if __name__ == "__main__":
    unittest.main()
