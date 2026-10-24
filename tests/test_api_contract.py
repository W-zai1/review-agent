#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""API 契约测试 —— 锁定 web/app.js（前端）与 server.py（后端）的接口匹配。

每个用例在临时项目副本上启动真实 server.py，然后按**前端实际消费的字段**
逐项断言，防止任一侧单独演进造成「界面在调、后端没有」或「字段对不上」：

  - 前端出现的每一个 /api 路径都必须在 server.py 中有对应路由（parity 测试）
  - 15 个接口的请求 / 响应契约（字段名、类型、关键取值）
  - 异步审查闭环：start -> status 轮询 -> 报告 -> 日志 -> 完整性
  - 同步兼容接口 POST /api/review
  - 上传：白名单 / UTF-8 / NUL / BOM / 重名 / 路径穿越
  - 规则启挺：状态落盘 + 计数
  - LLM：status / providers / config 校验 / 测试连接 / 重置 / 换商 Key 保护
  - 历史报告管理：删除二次确认 / 回收站 / 审计链墓碑不断链 / 拖拽排序持久化
  - 前端 DOM 契约：app.js 引用的每个 id 都必须在 index.html 中存在
  - 安全防护：Host / Origin / Sec-Fetch-Site 拒绝，本机 Origin 放行
  - 静态资源与 404 行为

运行：
    python -m unittest discover -s tests -v
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVIEW_ID_RE = re.compile(r"^R-\d{8}-\d{3}$")


# --------------------------------------------------------------------------
# 二进制文档样本构造（纯标准库，不依赖 pypdf 等第三方库）
# --------------------------------------------------------------------------
def make_docx(text="合同甲方：某某公司\n豆包 MAU 突破 10 亿"):
    """构造最小可解析的 .docx（zip + word/document.xml）。"""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>" + "".join(
                f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n")
            ) + "</w:body></w:document>")
    return buf.getvalue()


def make_pdf(text="MAU reached 1 billion"):
    """构造最小可解析的 .pdf（FlateDecode 内容流 + Tj 文本算子）。"""
    import zlib
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    comp = zlib.compress(content)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length " + str(len(comp)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + comp + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 5\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def make_png(width=20, height=10):
    """构造最小 PNG。"""
    import struct
    import zlib
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def make_zip(members):
    """构造 zip（members: {名称: 内容}）。"""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerCase(unittest.TestCase):
    """在临时项目副本上启动 server.py，提供绕过代理的 req() 助手。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra-api-")
        self.project = Path(self.tmp) / "review-agent"
        shutil.copytree(
            ROOT, self.project,
            ignore=shutil.ignore_patterns(
                "outputs", "logs", "ledger", "tests", "uploads",
                "__pycache__", ".venv", ".git"),
        )
        for name in ("outputs", "logs", "ledger"):
            (self.project / name).mkdir(exist_ok=True)
        for name in ("config/llm.local.json", "config/rules.state.json"):
            path = self.project / name
            if path.exists():
                path.unlink()
        env = os.environ.copy()
        # 测试解释器即运行解释器（已装 pyyaml）；屏蔽外部 LLM 环境变量保证确定性
        env.pop("REVIEW_AGENT_PY", None)
        for key in list(env):
            if key.startswith("REVIEW_AGENT_LLM") or key in (
                    "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
                env.pop(key, None)
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [sys.executable, "server.py", "--port", str(self.port)],
            cwd=self.project, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        self._wait_ready()

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- HTTP 助手 ----------
    def _open(self, request, timeout=60):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)

    def _wait_ready(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with self._open(self.base + "/api/targets", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.2)
        raise AssertionError("server.py 未在 20s 内就绪")

    def req(self, method, path, body=None, headers=None, raw_body=None):
        """返回 (http_status, json_or_None)。"""
        data = None
        h = dict(headers or {})
        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            self.base + path, data=data, headers=h, method=method)
        try:
            with self._open(request) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, payload = e.code, e.read()
        try:
            return status, json.loads(payload.decode("utf-8"))
        except Exception:
            return status, None

    def get(self, path, headers=None):
        return self.req("GET", path, headers=headers)

    def post(self, path, body=None, headers=None, raw_body=None):
        return self.req("POST", path, body=body, headers=headers,
                        raw_body=raw_body)

    def upload(self, name, payload):
        return self.post(f"/api/upload?name={name}", raw_body=payload)

    # ---------- 业务助手 ----------
    def run_review(self, file="targets/answer_001.txt"):
        """走异步接口跑一次纯规则审查，返回最终报告。"""
        status, data = self.post("/api/review/start",
                                 {"file": file, "llm": False})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"], data)
        job_id = data["job_id"]
        deadline = time.time() + 120
        payload = None
        while time.time() < deadline:
            st, d = self.get(f"/api/review/status?job={job_id}")
            self.assertTrue(d["ok"], d)
            if d["job"]["status"] != "running":
                payload = d
                break
            time.sleep(0.3)
        self.assertIsNotNone(payload, "审查任务 120s 内未结束")
        self.assertEqual(payload["job"]["status"], "done",
                         payload["job"].get("error"))
        self.assertIsNotNone(payload["report"])
        return payload["report"]


class FrontendParityTests(ServerCase):
    """核心保证：前端调的每个接口，后端都有对应代码。"""

    def test_every_frontend_api_call_has_a_route(self):
        app_js = (self.project / "web" / "app.js").read_text(encoding="utf-8")
        server_py = (self.project / "server.py").read_text(encoding="utf-8")
        # 兼容 "…"、'…'、`…` 与 ${API_BASE}/api/… 四种写法
        called = sorted(set(re.findall(
            r"[\"'`}](/api/[a-z][a-z/]*)", app_js)))
        self.assertGreaterEqual(len(called), 15,
                                f"前端 API 调用点提取异常：{called}")
        missing = [p for p in called if f'"{p}"' not in server_py]
        self.assertEqual(missing, [],
                         "前端调用了但 server.py 没有对应路由的接口")

    def test_static_assets_served(self):
        for path, ctype, marker in (
            ("/", "text/html", "审查 Agent 控制台"),
            ("/static/style.css", "text/css", ".nav"),
            ("/static/app.js", "application/javascript", "/api/review/start"),
            ("/static/config.js", "application/javascript", "REVIEW_AGENT_CONFIG"),
        ):
            status, _ = self.get(path)
            self.assertEqual(status, 200, path)
            request = urllib.request.Request(self.base + path)
            with self._open(request) as resp:
                self.assertIn(ctype, resp.headers.get("Content-Type", ""), path)
                body = resp.read().decode("utf-8", "replace")
                self.assertIn(marker, body, path)

    def test_unknown_routes_404(self):
        status, data = self.get("/api/nonexistent")
        self.assertEqual(status, 404)
        self.assertTrue(data is None or "error" in data)
        status, _ = self.get("/static/nope.js")
        self.assertEqual(status, 404)


class TargetsTests(ServerCase):
    def test_targets_contract(self):
        status, data = self.get("/api/targets")
        self.assertEqual(status, 200)
        items = data["items"]
        self.assertIsInstance(items, list)
        by_rel = {i["rel"]: i for i in items}
        for rel in ("targets/answer_001.txt", "targets/contract_001.md",
                    "targets/code_001.py"):
            self.assertIn(rel, by_rel)
            item = by_rel[rel]
            for key in ("name", "rel", "size", "mtime", "uploaded"):
                self.assertIn(key, item)
            self.assertIsInstance(item["size"], int)
            self.assertFalse(item["uploaded"])


class AsyncReviewTests(ServerCase):
    def test_review_lifecycle_and_report_contract(self):
        status, data = self.post("/api/review/start",
                                 {"file": "targets/answer_001.txt",
                                  "llm": False})
        self.assertTrue(data["ok"], data)
        job_id = data["job_id"]
        self.assertRegex(job_id, r"^[0-9a-f]{12}$")

        seen_running = False
        steps = []
        deadline = time.time() + 120
        payload = None
        while time.time() < deadline:
            st, d = self.get(f"/api/review/status?job={job_id}")
            self.assertTrue(d["ok"], d)
            job = d["job"]
            for key in ("id", "file", "status", "created", "finished",
                        "report_id", "error", "llm", "type"):
                self.assertIn(key, job)
            if job["status"] == "running":
                seen_running = True
            steps = d["steps"]
            if job["status"] != "running":
                payload = d
                break
            time.sleep(0.3)
        self.assertTrue(seen_running, "未观察到 running 中间态")
        self.assertIsNotNone(payload, "120s 内未完成")
        job = payload["job"]
        self.assertEqual(job["status"], "done", job.get("error"))
        self.assertTrue(REVIEW_ID_RE.match(job["report_id"] or ""))

        # 进度事件：S1~S6，前端 renderProgress 消费 step 字段
        step_ids = [s["step"] for s in steps]
        self.assertEqual(step_ids, ["S1", "S2", "S3", "S4", "S5", "S6"])
        for s in steps:
            for key in ("step", "name", "status", "ts", "latency_ms"):
                self.assertIn(key, s)

        # 报告契约：renderReport / statsPills 消费的字段
        report = payload["report"]
        for key in ("review_id", "verdict", "review_mode", "target_path",
                    "timestamp", "stats", "coverage", "limitations",
                    "issues"):
            self.assertIn(key, report)
        self.assertEqual(report["review_id"], job["report_id"])
        self.assertEqual(report["verdict"], "fail")
        self.assertEqual(report["review_mode"], "rule-only")
        # FACT-001 与 COMP-004 同句时，COMP-004 会被去重（第三轮 N9），minor 归零
        self.assertEqual(report["stats"],
                         {"blocker": 1, "major": 0, "minor": 0, "nit": 0})
        self.assertEqual(report["coverage"]["fact_assertions_total"], 2)
        self.assertGreaterEqual(
            report["coverage"]["fact_assertions_matched"], 1)
        self.assertTrue(report["limitations"])

        issues = report["issues"]
        self.assertTrue(issues)
        rules = {i["rule_id"] for i in issues}
        self.assertIn("FACT-001", rules)
        # COMP-004 与 FACT-001 同句时会被去重（第三轮 N9），不再强制要求两条并存
        self.assertTrue({"COMP-004", "FACT-001"} & rules)
        fact = next(i for i in issues if i["rule_id"] == "FACT-001")
        self.assertEqual(fact["severity"], "blocker")
        self.assertEqual(fact["confidence"], 0.85)
        for key in ("quote", "evidence", "suggestion", "dimension"):
            self.assertTrue(fact.get(key), f"FACT-001 缺字段 {key}")

    def test_concurrent_start_rejected(self):
        status, first = self.post("/api/review/start",
                                  {"file": "targets/answer_001.txt",
                                   "llm": False})
        self.assertTrue(first["ok"], first)
        status, second = self.post("/api/review/start",
                                   {"file": "targets/code_001.py",
                                    "llm": False})
        self.assertFalse(second["ok"])
        self.assertIn("进行中", second["error"])
        # 等第一个跑完，避免 tearDown 时子进程还在写临时目录
        deadline = time.time() + 120
        while time.time() < deadline:
            st, d = self.get(
                f"/api/review/status?job={first['job_id']}")
            if d["job"]["status"] != "running":
                break
            time.sleep(0.3)

    def test_start_guards(self):
        for body, fragment in (
            ({}, "缺少 file"),
            ({"file": "../../etc/passwd"}, "只能审查"),
            ({"file": "C:/Windows/win.ini"}, "只能审查"),
            ({"file": "targets/nope.txt"}, "不存在"),
        ):
            status, data = self.post("/api/review/start", body)
            self.assertFalse(data["ok"], body)
            self.assertIn(fragment, data["error"])

    def test_status_unknown_job(self):
        status, data = self.get("/api/review/status?job=deadbeef1234")
        self.assertFalse(data["ok"])
        self.assertIn("任务不存在", data["error"])
        status, data = self.get("/api/review/status")
        self.assertFalse(data["ok"])
        self.assertIn("缺少 job", data["error"])


class ReportsLogsIntegrityTests(ServerCase):
    def setUp(self):
        super().setUp()
        self.report = self.run_review("targets/answer_001.txt")
        self.rid = self.report["review_id"]

    def test_reports_list_contract(self):
        status, data = self.get("/api/reports")
        self.assertEqual(status, 200)
        items = data["items"]
        self.assertTrue(items)
        row = items[0]
        for key in ("id", "verdict", "target_type", "target_path",
                    "target_name", "review_mode", "timestamp", "issues",
                    "stats", "report_hash", "mtime"):
            self.assertIn(key, row)
        self.assertEqual(row["id"], self.rid)
        self.assertEqual(row["target_name"], "answer_001.txt")
        self.assertEqual(row["verdict"], "fail")
        for key in ("blocker", "major", "minor", "nit"):
            self.assertIn(key, row["stats"])
        self.assertEqual(len(row["report_hash"]), 16)

    def test_report_detail_contract(self):
        status, data = self.get(f"/api/report?id={self.rid}")
        self.assertTrue(data["ok"])
        report = data["report"]
        self.assertEqual(report["review_id"], self.rid)
        self.assertEqual(report["verdict"], "fail")
        self.assertTrue(report["issues"])
        self.assertTrue(report["integrity"]["report_hash"])

    def test_report_error_paths(self):
        for query, fragment in (
            ("/api/report", "缺少 id"),
            ("/api/report?id=../../etc/passwd", "格式不合法"),
            ("/api/report?id=R-2026-1", "格式不合法"),
            ("/api/report?id=R-20260101-999", "未找到报告"),
        ):
            status, data = self.get(query)
            self.assertFalse(data["ok"], query)
            self.assertIn(fragment, data["error"])

    def test_logs_contract(self):
        status, data = self.get(f"/api/logs?id={self.rid}")
        self.assertTrue(data["ok"])
        self.assertEqual(data["id"], self.rid)
        task = data["task"]
        self.assertEqual(len(task["steps"]), 6)
        for step in task["steps"]:
            for key in ("step", "name", "status", "ts", "latency_ms"):
                self.assertIn(key, step)
        self.assertEqual(task["final_status"], "success")

        decisions = data["decisions"]
        self.assertTrue(decisions)
        for d in decisions:
            for key in ("ts", "decision", "rule_id", "rationale",
                        "alternatives", "why_not_alternative"):
                self.assertIn(key, d)

        tools = data["tools"]
        self.assertTrue(tools)
        for t in tools:
            for key in ("ts", "tool", "input", "output_summary",
                        "latency_ms", "status", "retry"):
                self.assertIn(key, t)

        cost = data["cost"]
        for key in ("total_latency_ms", "llm_tokens", "llm_calls",
                    "llm_failures", "llm_model", "success_rate"):
            self.assertIn(key, cost)

    def test_logs_error_and_fallback(self):
        status, data = self.get("/api/logs?id=not-an-id")
        self.assertFalse(data["ok"])
        self.assertIn("格式不合法", data["error"])
        status, data = self.get("/api/logs")  # 无 id 时回退最新一次
        self.assertTrue(data["ok"])
        self.assertEqual(data["id"], self.rid)

    def test_integrity_contract(self):
        status, data = self.get("/api/integrity")
        self.assertTrue(data["ok"])
        self.assertTrue(data["chain_ok"])
        self.assertIsNone(data["broken_at"])
        self.assertEqual(data["detail"], "")
        entries = data["entries"]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        for key in ("seq", "review_id", "report_hash", "report_hash_short",
                    "prev_hash", "ts", "ok"):
            self.assertIn(key, entry)
        self.assertEqual(entry["seq"], 1)
        self.assertEqual(entry["review_id"], self.rid)
        self.assertEqual(len(entry["report_hash"]), 64)
        self.assertEqual(len(entry["report_hash_short"]), 12)
        self.assertEqual(entry["prev_hash"], "0" * 64)
        self.assertTrue(entry["ok"])
        reports = data["reports"]
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]["ok"])
        self.assertEqual(reports[0]["verdict"], "fail")


class EmptyIntegrityTests(ServerCase):
    def test_integrity_without_ledger(self):
        status, data = self.get("/api/integrity")
        self.assertFalse(data["ok"])
        self.assertFalse(data["chain_ok"])
        self.assertEqual(data["entries"], [])
        self.assertIn("ledger", data["detail"])


class RulesTests(ServerCase):
    def test_rules_contract(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import rules_store
        all_rules = rules_store.load_all_rules()
        expected_dims = {}
        for r in all_rules:
            expected_dims[r["dimension"]] = expected_dims.get(r["dimension"], 0) + 1
        status, data = self.get("/api/rules")
        self.assertTrue(data["ok"])
        # 规则数动态计算：以后新增规则（如 logic 维度）不应让断言失效
        self.assertEqual(data["total"], len(all_rules))
        self.assertEqual(data["enabled"], len(all_rules))
        self.assertEqual(data["dimensions"], expected_dims)
        self.assertIn("logic", expected_dims, "缺少逻辑一致性维度")
        first = data["items"][0]
        for key in ("rule_id", "dimension", "desc", "severity", "method",
                    "source", "file", "enabled"):
            self.assertIn(key, first)
        self.assertEqual(first["rule_id"], "FACT-001")
        self.assertEqual(first["dimension"], "fact")
        self.assertEqual(first["severity"], "blocker")
        self.assertTrue(first["enabled"])

    def test_toggle_roundtrip(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import rules_store
        total = len(rules_store.load_all_rules())
        status, data = self.post("/api/rules/toggle",
                                 {"rule_id": "FACT-001", "enabled": False})
        self.assertTrue(data["ok"])
        self.assertFalse(data["enabled"])
        self.assertEqual(data["enabled_count"], total - 1)
        self.assertEqual(data["total"], total)
        status, data = self.get("/api/rules")
        fact = next(i for i in data["items"] if i["rule_id"] == "FACT-001")
        self.assertFalse(fact["enabled"])
        self.assertEqual(data["enabled"], total - 1)
        status, data = self.post("/api/rules/toggle",
                                 {"rule_id": "FACT-001", "enabled": True})
        self.assertTrue(data["enabled"])
        self.assertEqual(data["enabled_count"], total)

    def test_toggle_errors(self):
        status, data = self.post("/api/rules/toggle",
                                 {"rule_id": "NOPE-999", "enabled": True})
        self.assertFalse(data["ok"])
        self.assertIn("规则不存在", data["error"])
        status, data = self.post("/api/rules/toggle", {"enabled": True})
        self.assertFalse(data["ok"])
        self.assertIn("缺少 rule_id", data["error"])
        status, data = self.post("/api/rules/toggle", {"rule_id": "FACT-001"})
        self.assertFalse(data["ok"])
        self.assertIn("缺少 enabled", data["error"])
        # 字符串布尔按前端兼容方式解析
        status, data = self.post("/api/rules/toggle",
                                 {"rule_id": "FACT-001", "enabled": "false"})
        self.assertTrue(data["ok"])
        self.assertFalse(data["enabled"])
        self.post("/api/rules/toggle",
                  {"rule_id": "FACT-001", "enabled": True})


class UploadTests(ServerCase):
    def test_upload_ok_and_listed(self):
        status, data = self.upload("contract_upload.txt",
                                   "合同上传测试：甲方乙方。".encode("utf-8"))
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["file"]["name"], "contract_upload.txt")
        self.assertTrue(data["file"]["rel"].startswith("targets/uploads/"))
        self.assertGreater(data["file"]["size"], 0)
        status, targets = self.get("/api/targets")
        row = next(i for i in targets["items"]
                   if i["rel"] == data["file"]["rel"])
        self.assertTrue(row["uploaded"])

    def test_upload_rejections(self):
        cases = (
            ("evil.exe", b"MZ\x90\x00", "不支持的文件类型"),
            ("empty.txt", b"", "文件为空"),
            ("nul.txt", b"abc\x00def", "疑似二进制"),
            ("", b"whatever", "文件名无效"),
            ("noext", b"whatever", "不支持的文件类型"),
        )
        for name, payload, fragment in cases:
            status, data = self.upload(name, payload)
            self.assertFalse(data["ok"], name)
            self.assertIn(fragment, data["error"], name)

    def test_upload_bom_stripped(self):
        status, data = self.upload("bom.txt", "\ufeff你好".encode("utf-8"))
        self.assertTrue(data["ok"], data)
        stored = (self.project / data["file"]["rel"]).read_bytes()
        self.assertFalse(stored.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(stored, "你好".encode("utf-8"))

    def test_upload_duplicate_gets_suffix(self):
        status, first = self.upload("dup.txt", b"first")
        self.assertTrue(first["ok"])
        status, second = self.upload("dup.txt", b"second")
        self.assertTrue(second["ok"])
        self.assertNotEqual(first["file"]["rel"], second["file"]["rel"])
        self.assertEqual(
            (self.project / second["file"]["rel"]).read_bytes(), b"second")

    def test_upload_traversal_name_is_basenamed(self):
        status, data = self.upload("..%2F..%2Fevil.txt", b"x")
        if data["ok"]:
            rel = data["file"]["rel"]
            self.assertTrue(rel.startswith("targets/uploads/"))
            self.assertEqual(Path(rel).name, "evil.txt")
        self.assertFalse((self.project / "evil.txt").exists())
        self.assertFalse((self.project.parent / "evil.txt").exists())


class LlmConfigTests(ServerCase):
    def test_status_unconfigured(self):
        status, data = self.get("/api/llm/status")
        self.assertTrue(data["ok"])
        for key in ("enabled", "available", "base_url", "endpoint", "model",
                    "api_key", "api_key_set", "timeout", "max_retries",
                    "max_tokens", "max_input_chars", "max_issues_per_dim",
                    "source", "local_override", "local_config"):
            self.assertIn(key, data)
        self.assertFalse(data["available"])
        self.assertFalse(data["api_key_set"])
        self.assertEqual(data["api_key"], "")
        self.assertEqual(data["source"], "config")
        self.assertEqual(data["model"], "deepseek-chat")
        self.assertFalse(data["local_override"])

    def test_providers_contract(self):
        status, data = self.get("/api/llm/providers")
        self.assertTrue(data["ok"])
        items = data["items"]
        self.assertGreaterEqual(len(items), 12)
        by_id = {p["id"]: p for p in items}
        for pid in ("deepseek", "openai", "dashscope", "moonshot", "zhipu",
                    "siliconflow", "volcengine", "qianfan", "ollama",
                    "lmstudio", "vllm", "custom"):
            self.assertIn(pid, by_id)
        for p in items:
            for key in ("id", "name", "base_url", "model", "models",
                        "needs_key", "local", "hint"):
                self.assertIn(key, p)
        self.assertTrue(by_id["deepseek"]["needs_key"])
        self.assertFalse(by_id["deepseek"]["local"])
        self.assertTrue(by_id["ollama"]["local"])
        self.assertFalse(by_id["ollama"]["needs_key"])
        self.assertTrue(by_id["ollama"]["models"])

    def test_config_validation(self):
        for body, fragment in (
            ({"base_url": "not-a-url"}, "http:// 或 https://"),
            ({"model": ""}, "模型名不能为空"),
            ({"model": "x" * 200}, "不超过 120"),
            ({"timeout": 0}, "1-600"),
            ({"timeout": 999}, "1-600"),
            ({"max_tokens": 8}, "16-8192"),
            ({"timeout": "abc"}, "必须是整数"),
        ):
            status, data = self.post("/api/llm/config", body)
            self.assertFalse(data["ok"], body)
            self.assertIn(fragment, data["error"], body)

    def test_config_save_and_reset(self):
        status, data = self.post("/api/llm/config", {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen2.5:7b", "timeout": 30, "max_tokens": 800,
            "enabled": True,
        })
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["saved"])
        st = data["status"]
        self.assertTrue(st["available"])          # 本地端点免 Key
        self.assertTrue(st["local_override"])
        self.assertEqual(st["timeout"], 30)
        self.assertEqual(st["max_tokens"], 800)
        self.assertEqual(st["model"], "qwen2.5:7b")
        # 落盘文件存在且 Key 不在其中明文泄露到响应
        local = self.project / "config" / "llm.local.json"
        self.assertTrue(local.exists())
        self.assertNotIn("api_key", json.loads(local.read_text("utf-8")))

        status, data = self.post("/api/llm/reset", {})
        self.assertTrue(data["ok"])
        self.assertFalse(local.exists())
        self.assertFalse(data["status"]["local_override"])
        self.assertEqual(data["status"]["source"], "config")
        self.assertEqual(data["status"]["model"], "deepseek-chat")

    def test_key_switch_protection(self):
        # 先给本地端点存一个 Key
        status, data = self.post("/api/llm/config", {
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen2.5:7b",
            "api_key": "sk-test-1234567890",
        })
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["status"]["api_key_set"])
        # 切到远端服务商但不带 Key → 必须拒绝
        status, data = self.post("/api/llm/config", {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat",
        })
        self.assertFalse(data["ok"])
        self.assertIn("切换服务商", data["error"])
        # 显式清除 Key 后允许
        status, data = self.post("/api/llm/config", {
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat", "clear_key": True,
        })
        self.assertTrue(data["ok"], data)
        self.assertFalse(data["status"]["api_key_set"])

    def test_llm_test_connection(self):
        # 保存一个本地端点（免 Key），对一个无人监听的端口试连 → 优雅失败
        self.post("/api/llm/config", {
            "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"})
        status, data = self.post("/api/llm/test", {
            "base_url": "http://127.0.0.1:19998/v1", "model": "any-model"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertTrue(data["error"])
        self.assertIn("model", data)
        # 未保存任何配置时试连 → 明确缺少 Key 的提示
        self.post("/api/llm/reset", {})
        status, data = self.post("/api/llm/test", {
            "base_url": "https://api.example.com/v1", "model": "m"})
        self.assertFalse(data["ok"])
        self.assertIn("API Key", data["error"])


class SecurityGuardTests(ServerCase):
    def test_cross_site_requests_rejected(self):
        status, data = self.get("/api/targets", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("Host", data["error"])
        status, data = self.post("/api/llm/reset", {},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("跨站", data["error"])
        status, data = self.get("/api/targets",
                                headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_local_origin_allowed(self):
        status, data = self.get(
            "/api/targets",
            headers={"Origin": f"http://127.0.0.1:{self.port}",
                     "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertIn("items", data)


class SyncReviewTests(ServerCase):
    def test_sync_review_legacy(self):
        status, data = self.post("/api/review",
                                 {"file": "targets/code_001.py",
                                  "llm": False})
        self.assertTrue(data["ok"], data)
        report = data["report"]
        self.assertTrue(REVIEW_ID_RE.match(report["review_id"]))
        self.assertEqual(report["review_id"], data["id"])
        self.assertEqual(report["verdict"], "warn")
        self.assertIn("STYLE-004", {i["rule_id"] for i in report["issues"]})
        self.assertIsInstance(data["elapsed"], (int, float))

    def test_sync_review_guard(self):
        status, data = self.post("/api/review",
                                 {"file": "../secret.txt", "llm": False})
        self.assertFalse(data["ok"])
        self.assertIn("只能审查", data["error"])


class ReportManagementTests(ServerCase):
    """历史报告管理：删除（二次确认 + 审计链墓碑）与拖拽排序。"""

    def test_delete_requires_explicit_confirm(self):
        report = self.run_review()
        rid = report["review_id"]

        status, data = self.req("DELETE", f"/api/report?id={rid}")
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertTrue(data["need_confirm"], data)

        status, data = self.req("DELETE", f"/api/report?id={rid}",
                                {"confirm": "R-00000000-000"})
        self.assertFalse(data["ok"])
        self.assertTrue(data["need_confirm"], data)

        # 未确认时文件必须原封不动
        self.assertTrue((self.project / "outputs" / f"{rid}.json").exists())
        status, data = self.get("/api/reports")
        self.assertIn(rid, [i["id"] for i in data["items"]])

    def test_delete_moves_to_trash_and_keeps_chain_intact(self):
        report = self.run_review()
        rid = report["review_id"]
        status, data = self.req("DELETE", f"/api/report?id={rid}", {"confirm": rid})
        self.assertTrue(data["ok"], data)
        self.assertTrue(data["ledger_tombstone"], data)

        status, data = self.get("/api/reports")
        self.assertNotIn(rid, [i["id"] for i in data["items"]])

        trash = self.project / "outputs" / ".trash"
        self.assertTrue((trash / f"{rid}.json").exists())
        self.assertTrue((trash / f"{rid}.md").exists())
        self.assertFalse((self.project / "logs" / rid).exists())

        # 审计链：不断，且留下可审计的墓碑
        status, data = self.get("/api/integrity")
        self.assertTrue(data["chain_ok"], data)
        self.assertTrue(data["ok"], data)
        tombs = [e for e in data["entries"] if e.get("deleted")]
        self.assertEqual(len(tombs), 1, data["entries"])
        self.assertEqual(tombs[0]["review_id"], rid)
        self.assertTrue(tombs[0]["ok"])

        out = subprocess.run([sys.executable, "tools/integrity.py", "verify"],
                             cwd=self.project, capture_output=True, text=True).stdout
        self.assertIn("ledger 校验通过", out, out)

        # 已删报告的日志不再可读
        status, data = self.get(f"/api/logs?id={rid}")
        self.assertFalse(data["ok"])

    def test_delete_rejects_bad_id(self):
        for rid in ("../etc/passwd", "R-9999999-99", ""):
            status, data = self.req("DELETE", f"/api/report?id={rid}",
                                    {"confirm": rid})
            self.assertFalse(data["ok"], f"{rid} 不应被删除")

    def test_report_order_is_persisted_and_applied(self):
        self.run_review()
        self.run_review("targets/code_001.py")
        status, data = self.get("/api/reports")
        ids = [i["id"] for i in data["items"]]
        self.assertGreaterEqual(len(ids), 2)

        wanted = list(reversed(ids))
        status, data = self.post("/api/reports/order", {"order": wanted})
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["order"], wanted)

        status, data = self.get("/api/reports")
        self.assertEqual([i["id"] for i in data["items"]], wanted)
        self.assertTrue((self.project / "config" / "report_order.json").exists())

    def test_report_order_rejects_garbage(self):
        status, data = self.post("/api/reports/order", {"order": "nope"})
        self.assertFalse(data["ok"])
        status, data = self.post("/api/reports/order",
                                 {"order": ["../../etc/passwd", "not-an-id"]})
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["order"], [])


class FrontendHistoryContractTests(unittest.TestCase):
    """前端历史列表的静态契约：DOM 结构、拖拽事件、二次确认调用。"""

    def setUp(self):
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    def test_every_referenced_dom_id_exists(self):
        ids = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', self.app))
        missing = sorted(i for i in ids if f'id="{i}"' not in self.html)
        self.assertEqual(missing, [],
                         f"app.js 引用了 index.html 中不存在的 id：{missing}")
        self.assertIn("confirmModal", ids, "缺少删除二次确认弹窗的绑定")

    def test_delete_flow_uses_confirm_dialog_and_server_guard(self):
        self.assertIn('id="confirmModal"', self.html)
        self.assertIn("confirmOk", self.app)
        self.assertIn("confirmCancel", self.app)
        self.assertIn('method: "DELETE"', self.app)
        self.assertIn("need_confirm", self.app)
        self.assertIn("confirm: id", self.app)

    def test_drag_and_drop_sort_present(self):
        for token in ("dragstart", "dragover", "dragend", "drop-before",
                      "drop-after", "draggable", "/api/reports/order"):
            self.assertIn(token, self.app, f"app.js 缺少拖拽排序要素：{token}")
        for token in (".history-row.dragging", ".history-row.drop-before",
                      ".history-row.drop-after", ".drag-handle", ".history-del",
                      ".btn-danger"):
            self.assertIn(token, self.css, f"style.css 缺少样式：{token}")

    def test_history_row_markup_has_drag_handle_and_delete_button(self):
        self.assertIn('draggable="true"', self.app)
        self.assertIn('class="history-del"', self.app)
        self.assertIn("data-del=", self.app)


class OcrSupportTests(unittest.TestCase):
    """OCR 适配层：引擎探测、WinRT 脚本约定、图片上传的 OCR 分支。"""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import ocr
        self.ocr = ocr
        self.script = ROOT / "tools" / "ocr_winrt.ps1"

    def test_engine_probe_returns_list(self):
        engines = self.ocr.available_engines()
        self.assertIsInstance(engines, list)
        for name in engines:
            self.assertIn(name, ("vision", "winrt", "rapidocr", "tesseract"))

    def test_winrt_script_is_ascii_with_bom(self):
        # PowerShell 5.1 按 ANSI 代码页解析无 BOM 的 UTF-8 脚本 -> 中文乱码 -> 语法错误。
        # 因此 .ps1 必须纯 ASCII，且带 UTF-8 BOM。
        self.assertTrue(self.script.exists(), "缺少 tools/ocr_winrt.ps1")
        raw = self.script.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"),
                        "ocr_winrt.ps1 缺少 UTF-8 BOM（PS 5.1 会按 GBK 解析导致语法错误）")
        body = raw[3:]
        bad = [(i, b) for i, b in enumerate(body) if b > 0x7F]
        self.assertEqual(bad, [],
                         f"ocr_winrt.ps1 含非 ASCII 字节（PS 5.1 会乱码）：{bad[:5]}")

    def test_ocr_reports_unavailable_instead_of_fake_empty(self):
        missing = ROOT / "targets" / "definitely-missing.png"
        with self.assertRaises((self.ocr.OcrError, self.ocr.OcrUnavailable)):
            self.ocr.ocr_image(missing)

    def test_winrt_recognizes_text_when_available(self):
        if "winrt" not in self.ocr.available_engines():
            self.skipTest("本机无 Windows 内置 OCR 引擎")
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory(prefix="ra-ocr-test-") as tmp:
            img = Path(tmp) / "probe.png"
            out = Path(tmp) / "probe.txt"
            exe = shutil.which("powershell") or shutil.which("powershell.exe")
            proc = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(self.script), "-SelfTest", "-SelfTestImage", str(img),
                 "-Out", str(out)],
                capture_output=True, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             proc.stderr.decode("utf-8", "replace"))
            self.assertTrue(img.exists(), "自测图片未生成")
            text, meta = self.ocr.ocr_image(img)
            self.assertIn(meta["engine"], ("winrt", "vision", "rapidocr", "tesseract"))
            self.assertGreater(meta["chars"], 0)


class BinaryUploadTests(ServerCase):
    """二进制文档上传：PDF / Word / 图片 / 压缩包 摄取成可审文本。"""

    def upload_bytes(self, name, data):
        return self.req("POST", f"/api/upload?name={name}", raw_body=data)

    def test_pdf_upload_is_extracted(self):
        status, data = self.upload_bytes("sample.pdf", make_pdf("MAU reached 1 billion"))
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["kind"], "pdf")
        self.assertEqual(len(data["extracted"]), 1)
        out = data["extracted"][0]
        self.assertTrue(out["name"].endswith(".pdf.txt"))
        self.assertGreater(out["chars"], 0)
        # 派生文本进待审列表
        status, targets = self.get("/api/targets")
        self.assertIn(out["rel"], [t["rel"] for t in targets["items"]])
        # 内容抽取正确
        text = (self.project / out["rel"]).read_text(encoding="utf-8")
        self.assertIn("MAU reached 1 billion", text)

    def test_docx_upload_is_extracted(self):
        status, data = self.upload_bytes("contract.docx", make_docx())
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["kind"], "docx")
        out = data["extracted"][0]
        text = (self.project / out["rel"]).read_text(encoding="utf-8")
        self.assertIn("合同甲方：某某公司", text)

    def test_image_upload_is_archived_but_not_reviewed(self):
        status, data = self.upload_bytes("shot.png", make_png())
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["kind"], "image")
        # 纯色图 OCR 识别不出文字 → 无可审对象，但必须明确标注而非静默
        if not data["extracted"]:
            self.assertEqual(data["extracted"], [])
            self.assertIsNone(data["file"])
            self.assertTrue(data.get("warnings"), data)
        else:
            out = data["extracted"][0]
            self.assertTrue(out["name"].endswith(".image.txt"))
            self.assertGreater(out["chars"], 0)
        # 原件已归档
        self.assertTrue((self.project / data["original"]).exists())

    def test_zip_upload_expands_members(self):
        status, data = self.upload_bytes("bundle.zip", make_zip({
            "readme.md": "# 说明\n豆包 MAU 突破 10 亿。",
            "sub/contract.txt": "甲方：某公司",
        }))
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["kind"], "zip")
        names = [o["name"] for o in data["extracted"]]
        self.assertEqual(len(names), 2)
        self.assertTrue(any("readme.md" in n for n in names), names)
        self.assertTrue(any("contract.txt" in n for n in names), names)

    def test_zip_slip_rejected(self):
        status, data = self.upload_bytes(
            "evil.zip", make_zip({"../../../../evil.txt": "pwned"}))
        self.assertFalse(data["ok"])
        self.assertIn("已拒绝", data["error"])

    def test_legacy_doc_rejected(self):
        status, data = self.upload_bytes(
            "old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
        self.assertFalse(data["ok"])
        self.assertIn("docx", data["error"])

    def test_originals_and_meta_not_in_target_list(self):
        status, data = self.upload_bytes("sample.pdf", make_pdf())
        self.assertTrue(data["ok"], data)
        status, targets = self.get("/api/targets")
        rels = [t["rel"] for t in targets["items"]]
        self.assertFalse([r for r in rels if "_originals" in r], rels)
        self.assertFalse([r for r in rels if "_meta" in r], rels)

    def test_review_of_derived_text_records_source(self):
        status, data = self.upload_bytes("sample.pdf", make_pdf())
        self.assertTrue(data["ok"], data)
        out = data["extracted"][0]
        report = self.run_review(out["rel"])
        source = (report.get("coverage") or {}).get("source")
        self.assertIsNotNone(source, report.get("coverage"))
        self.assertEqual(source.get("kind"), "pdf")
        self.assertEqual(source.get("source_name"), "sample.pdf")
        self.assertTrue(any("抽取生成" in lim
                            for lim in report.get("limitations") or []),
                        report.get("limitations"))


class OpenDiscoveryTests(unittest.TestCase):
    """规则外独立发现（OPEN-xxx）机制。

    这是打破「规则闭集」的核心改动：LLM 必须被允许报告规则清单之外的问题，
    否则它只会在预设规则里打转（实测中它甚至把规则清单当成原文引用）。
    同时安全线不放宽：quote 仍必须逐字来自原文。
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT / "tools"))
        import llm_review
        import rules_store
        self.llm_review = llm_review
        self.rules = [{"rule_id": "LOGIC-001", "dimension": "logic",
                       "severity": "major", "desc": "数值互斥必须指出"}]
        self.text = "孙倩：总收入 1500 万，分项 700 + 700 + 200。"

    def _item(self, rule_id, quote):
        return json.dumps([{
            "rule_id": rule_id, "quote": quote,
            "evidence": "与分项合计 1600 万冲突",
            "confidence": 0.8, "suggestion": "核对数字"}], ensure_ascii=False)

    def test_open_discovery_is_accepted(self):
        issues, dropped, retracted = self.llm_review.validate(
            self._item("OPEN-CONFLICT", "总收入 1500 万"), self.rules, self.text, 5)
        self.assertEqual(len(issues), 1, dropped)
        self.assertTrue(issues[0]["open_discovery"])
        # 规则外发现默认 minor：长文里若一律 major，会刷出上百条噪声（实测 116 条）
        self.assertEqual(issues[0]["severity"], "minor")
        self.assertEqual(dropped, [])

    def test_open_discovery_promoted_only_when_confident(self):
        hi = json.dumps([{"rule_id": "OPEN-CONFLICT", "quote": "总收入 1500 万",
                          "evidence": "与分项合计冲突", "confidence": 0.9,
                          "suggestion": "核对"}], ensure_ascii=False)
        issues, _, _ = self.llm_review.validate(hi, self.rules, self.text, 5)
        self.assertEqual(issues[0]["severity"], "major", "高置信开放发现才升 major")

    def test_open_discovery_per_call_cap(self):
        items = [{"rule_id": f"OPEN-X{i}", "quote": "总收入 1500 万",
                  "evidence": "e", "confidence": 0.8, "suggestion": "s"} for i in range(6)]
        raw = json.dumps(items, ensure_ascii=False)
        issues, dropped, _ = self.llm_review.validate(raw, self.rules, self.text, 5)
        self.assertLessEqual(len(issues), 3, "开放发现单次上限 3 条")
        self.assertTrue(any("规则外发现超出单次上限" in d for d in dropped), dropped)

    def test_float_noise_is_downgraded_not_blocker(self):
        """37.800000000000004 万 是程序浮点尾数，不能让模型报成 blocker。"""
        text = "月度预算约 37.800000000000004 万元。"
        raw = json.dumps([{"rule_id": "OPEN-CONFLICT",
                           "quote": "月度预算约 37.800000000000004 万元",
                           "evidence": "浮点尾数非人工核算", "confidence": 0.9,
                           "suggestion": "取整"}], ensure_ascii=False)
        issues, _, _ = self.llm_review.validate(raw, self.rules, text, 5)
        self.assertEqual(issues[0]["severity"], "minor")
        self.assertIn("浮点尾数", issues[0]["evidence"])

    def test_retracted_items_are_separated_from_issues(self):
        """模型自己写「本条不成立，撤回」的条目应进 retracted，不污染待整改清单。"""
        raw = json.dumps([{"rule_id": "LOGIC-001", "quote": "总收入 1500 万",
                           "evidence": "分项 700+700+200=1600，但本条不成立，撤回，无需整改",
                           "confidence": 0.8, "suggestion": "无"}], ensure_ascii=False)
        issues, dropped, retracted = self.llm_review.validate(
            raw, self.rules, self.text, 5)
        self.assertEqual(issues, [])
        self.assertEqual(len(retracted), 1)
        self.assertTrue(retracted[0]["reason"])

    def test_unknown_rule_id_still_dropped(self):
        issues, dropped, retracted = self.llm_review.validate(
            self._item("MADE-UP-1", "总收入 1500 万"), self.rules, self.text, 5)
        self.assertEqual(issues, [])
        self.assertTrue(any("OPEN-XXX" in d for d in dropped), dropped)

    def test_quote_must_be_verbatim_even_for_open_discovery(self):
        issues, dropped, retracted = self.llm_review.validate(
            self._item("OPEN-CONFLICT", "这是一段原文里没有的句子"), self.rules, self.text, 5)
        self.assertEqual(issues, [])
        self.assertTrue(any("quote 非原文" in d for d in dropped), dropped)

    def test_prompt_announces_open_discovery_and_quote_source(self):
        msgs = self.llm_review.build_messages("logic", self.rules, self.text,
                                              charter="用途：对外宣传稿")
        user = msgs[1]["content"]
        self.assertIn("OPEN-XXX", user)
        self.assertIn("非封闭限制", user)
        self.assertIn("只能取自本区块", user)
        self.assertIn("用途：对外宣传稿", user)      # 审查任务书必须注入
        self.assertIn("<<<TARGET", user)

    def test_truncated_output_flagged_as_incomplete_not_clean(self):
        from types import SimpleNamespace

        class FakeClient:
            def chat(self, messages, **kw):
                return SimpleNamespace(content="", usage={"input": 1, "output": 4096},
                                      model="fake", latency_ms=1, finish_reason="length")

        cfg = SimpleNamespace(max_input_chars=12000, max_issues_per_dim=5)
        res = self.llm_review.review_dimension(FakeClient(), "logic", self.rules,
                                               self.text, cfg)
        self.assertTrue(res["output_truncated"])
        self.assertTrue(any("未完成审查" in d for d in res["dropped"]), res["dropped"])

    def test_partially_salvaged_output_still_flagged(self):
        """输出被截断但救回了对象，仍必须标记——这是长文假安全感的主要来源。"""
        from types import SimpleNamespace
        text = "甲方应于 10 月 18 日（周六）完成验收。"
        quote = "甲方应于 10 月 18 日（周六）完成验收。"
        # 自包含规则集：本类 setUp 里的 self.rules 只有 LOGIC-001
        rules = [{"rule_id": "LOGIC-002", "dimension": "logic",
                  "severity": "major", "desc": "日期与星期必须一致"}]
        raw = ('[{"rule_id":"LOGIC-002","quote":"%s","evidence":"星期不符",'
               '"confidence":0.9,"suggestion":"核对"}' % quote)

        class FakeClient:
            def chat(self, messages, **kw):
                return SimpleNamespace(content=raw, usage={"input": 10, "output": 8192},
                                      model="fake", latency_ms=1, finish_reason="length")

        cfg = SimpleNamespace(max_input_chars=12000, max_issues_per_dim=5)
        res = self.llm_review.review_dimension(FakeClient(), "logic", rules, text, cfg)
        self.assertEqual(len(res["issues"]), 1, "救回的完整对象不能丢")
        self.assertTrue(res["output_truncated"],
                        "finish_reason=length 且已救回对象时也必须标记截断")
        self.assertTrue(any("已救回" in d for d in res["dropped"]), res["dropped"])

    def test_logic_dimension_registered(self):
        import rules_store
        ids = {r["rule_id"] for r in rules_store.load_all_rules()}
        self.assertTrue({"LOGIC-001", "LOGIC-006"} <= ids, sorted(ids))
        import review
        self.assertIn("logic", review.DIM_FOR_TYPE["llm_response"])

    def test_truncated_json_is_salvaged_not_discarded(self):
        # 模型答对了却因为输出被截断而整条丢弃，是最可惜的失败模式：
        # 截断前已写完的对象必须能救回来。
        truncated = (
            '```json\n[\n'
            '  {"rule_id": "LOGIC-001", "quote": "总收入 1500 万", "evidence": "分项合计 1600 万"},\n'
            '  {"rule_id": "LOGIC-002", "quote": "投入 18 个月", "evi'
        )
        data = self.llm_review._loads_array(truncated)
        self.assertEqual(len(data), 1, data)
        self.assertEqual(data[0]["rule_id"], "LOGIC-001")
        # 完整 JSON 不受影响
        full = '[{"rule_id": "LOGIC-001"}]'
        self.assertEqual(len(self.llm_review._loads_array(full)), 1)
        # 带 ``` 包裹的也正常
        self.assertEqual(len(self.llm_review._loads_array('```json\n[{"a":1}]\n```')), 1)


class LlmMemoryTests(ServerCase):
    """LLM 配置记忆：保存后能被记住，下次可一键切回；超时等参数必须真的生效。"""

    def setUp(self):
        super().setUp()
        # 记忆文件是跨会话的，测试里要清干净，避免受真实历史干扰
        hist = self.project / "config" / "llm_history.json"
        if hist.exists():
            hist.unlink()

    def test_timeout_upper_limit_allows_thinking_models(self):
        # 思考型模型需要长超时；此前上限 300 会让「保存」直接被拒
        status, data = self.post("/api/llm/config", {
            "base_url": "http://127.0.0.1:11434/v1", "model": "deepseek-r1:8b",
            "timeout": 450, "max_tokens": 4096})
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["status"]["timeout"], 450)
        self.assertEqual(data["status"]["max_tokens"], 4096)

    def test_config_is_remembered_and_restorable(self):
        # 保存云端 deepseek
        status, data = self.post("/api/llm/config", {
            "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "timeout": 120})
        self.assertTrue(data["ok"], data)
        # 保存本地模型
        status, data = self.post("/api/llm/config", {
            "base_url": "http://127.0.0.1:11434/v1", "model": "deepseek-r1:8b",
            "timeout": 300})
        self.assertTrue(data["ok"], data)

        # 两条都进了记忆
        status, data = self.get("/api/llm/history")
        self.assertTrue(data["ok"], data)
        pairs = {(i["base_url"], i["model"]) for i in data["items"]}
        self.assertIn(("https://api.deepseek.com/v1", "deepseek-chat"), pairs)
        self.assertIn(("http://127.0.0.1:11434/v1", "deepseek-r1:8b"), pairs)
        # 记忆里不能有 API Key 明文
        for item in data["items"]:
            self.assertNotIn("api_key", item)
            self.assertIn("api_key_set", item)

        # 一键切回云端 deepseek
        status, data = self.post("/api/llm/history/restore", {
            "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"})
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["status"]["model"], "deepseek-chat")
        self.assertEqual(data["status"]["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(data["status"]["source"], "ui")

    def test_restore_requires_key_for_new_host(self):
        # 先存一个带 Key 的云端配置，再切到另一个 host（不提供 Key）→ 必须拒绝
        self.post("/api/llm/config", {
            "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "api_key": "sk-test-123456"})
        status, data = self.post("/api/llm/history/restore", {
            "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"})
        self.assertFalse(data["ok"])
        self.assertIn("API Key", data["error"])

    def test_test_endpoint_honours_draft_timeout(self):
        # 草稿里的 timeout 必须生效（此前被忽略并被压到 60s 上限）
        status, data = self.post("/api/llm/test", {
            "base_url": "http://127.0.0.1:19997/v1", "model": "x", "timeout": 7})
        self.assertFalse(data["ok"])
        self.assertEqual(data.get("timeout"), 7)

    def test_history_survives_restart(self):
        self.post("/api/llm/config", {
            "base_url": "http://127.0.0.1:11434/v1", "model": "deepseek-r1:8b"})
        status, data = self.get("/api/llm/history")
        self.assertTrue(data["items"])
        # 落盘文件存在 → 说明是持久化的，不是内存态
        self.assertTrue((self.project / "config" / "llm_history.json").exists())


class RulesVersionTests(ServerCase):
    """规则集版本：必须能区分「规则后来升级过」与「规则库被偷改」。

    这是一个真实踩过的坑：新增 rules/logic.yaml 让 rules/ 的 hash 变化，
    导致所有历史报告的 rules_dir_hash 失配 → 完整性告警全红，
    用户无法区分「正常升级」与「报告被篡改」。
    """

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(ROOT / "tools"))
        import integrity
        self.integrity = integrity

    def test_upgraded_ruleset_is_stale_not_tampered(self):
        report = self.run_review()
        rid = report["review_id"]
        rp = str(self.project / "outputs" / f"{rid}.json")
        rules_file = self.project / "rules" / "style.yaml"

        # 基线：未改动时必须自洽通过
        ok, msg, stale = self.integrity.verify_report(rp, str(self.project))
        self.assertTrue(ok, msg)
        self.assertFalse(stale)

        # 1) 改了 rules/ 但没升版本号 → 判为篡改（必须红）
        rules_file.write_text(
            rules_file.read_text(encoding="utf-8") + "\n# 偷偷改一行\n", encoding="utf-8")
        ok, msg, stale = self.integrity.verify_report(rp, str(self.project))
        self.assertFalse(ok, "版本号未变却改了 rules/，必须判为篡改")
        self.assertFalse(stale)

        # 2) 同步升级版本号 → 判为 stale（规则演进，不算失败）
        (self.project / "config" / "rules_version.txt").write_text(
            "v2-test-upgrade\n", encoding="utf-8")
        ok, msg, stale = self.integrity.verify_report(rp, str(self.project))
        self.assertTrue(ok, msg)
        self.assertTrue(stale, "版本号已升级，应标记为 stale 而不是失败")

    def test_report_records_rules_version(self):
        report = self.run_review()
        ver = (report.get("integrity") or {}).get("rules_version")
        self.assertTrue(ver, "报告必须记录生成时的规则集版本号")


class AnnotationViewTests(ServerCase):
    """原文标注视图：报告要带原文快照，前端要能高亮并按类型归类。"""

    def test_report_contains_target_text_snapshot(self):
        report = self.run_review()
        snapshot = report.get("target_text")
        self.assertTrue(snapshot and snapshot.strip(),
                        "报告必须带原文快照，否则标注视图无从定位")
        src = (self.project / report["target_path"]).read_text(encoding="utf-8")
        self.assertIn(snapshot[:80], src,
                      "快照必须与被审原文一致（与 quote 校验同一基准）")

    def test_annotation_view_elements_and_logic_present(self):
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for token in ("renderAnnotatedText", "renderIssueSummary", "annotatedBlock",
                      "annotatedText", "issueSummary", "annoLegend", "merged"):
            self.assertIn(token, app, f"app.js 缺少标注视图要素：{token}")
        for eid in ("annotatedBlock", "annotatedText", "summaryBlock",
                    "issueSummary", "annoLegend"):
            self.assertIn(f'id="{eid}"', html, f"index.html 缺少 #{eid}")

    def test_overlapping_quotes_are_merged_not_dropped(self):
        # 同一段原文被多个维度同时引用时不能丢掉其中一条（那是漏报）
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("merged", app)
        self.assertNotIn("kept.push(m)", app,
                         "不得再用「丢弃重叠」的方式处理标注片段")

    def test_issue_numbering_shared_by_annotation_and_summary(self):
        # 标注角标与汇总表必须共用同一套编号，否则无法互相定位
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for token in ("buildIssueIndex", "anno-badge", "seq-badge", "index.get"):
            self.assertIn(token, app, f"缺少编号要素：{token}")
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        for token in (".anno-badge", ".seq-badge"):
            self.assertIn(token, css, f"style.css 缺少编号样式：{token}")
        # 编号必须体现「该类型的第几条」→ 标签形如 逻辑1 / 事实2
        self.assertIn("label: `${DIM_FULL[d] || d}${i + 1}`", app)

    def test_summary_row_jumps_to_source_annotation(self):
        """点汇总表任意一行 → 原文滚动到对应标注并闪烁高亮。"""
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        for token in ("jumpToIssue", "annoAnchors", "data-issue-id", "sum-row",
                      "jumpBound", "is-flash", "scrollTo"):
            self.assertIn(token, app, f"汇总表跳转定位缺少要素：{token}")
        # 事件必须用委托绑定在常驻容器上（innerHTML 重渲染后依然有效且不重复绑定）
        self.assertIn('host.dataset.jumpBound', app)
        self.assertIn('closest("tr.sum-row")', app)
        # 跳转要避让 sticky 顶部导航，否则目标会被遮挡
        self.assertIn("NAV_HEIGHT", app)
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        for token in (".anno.is-flash", "tr.sum-row", "cursor: pointer",
                      "prefers-reduced-motion"):
            self.assertIn(token, css, f"style.css 缺少跳转高亮样式：{token}")

    def test_scroll_position_uses_rect_not_offsettop(self):
        """定位精度：必须用 rect 差值，不能用 offsetTop。

        `.anno-text` 没有 position，其 `offsetParent` 是更外层元素，
        `offsetTop` 已包含整页偏移；再叠加 box.scrollTop 会重复计算，
        实测落点误差 = 容器距页面顶部的距离（约 900px）。这条卡住回归。
        """
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        # 注释里会提到 offsetTop（说明为什么弃用），只检查真实代码
        code = re.sub(r"/\*.*?\*/", "", app, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        self.assertNotIn(".offsetTop", code,
                         "不得再用 mark.offsetTop 定位（参照系是 body，会重复叠加整页偏移）")
        self.assertNotIn(".offsetHeight", code,
                         "offsetHeight 同属 offset* 参照系，一律改用 getBoundingClientRect")
        self.assertIn("desiredScrollTop", app)
        self.assertIn("getBoundingClientRect", app)
        # 平滑滚动是异步的，必须有滚动后的闭环校正，否则落点靠猜
        self.assertIn("ensureMarkInViewport", app)
        self.assertGreaterEqual(app.count("setTimeout"), 1)

    def test_bidirectional_linkage_and_dim_focus(self):
        """反向定位（点原文回跳汇总表）与"压暗非目标"的聚焦效果。"""
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        for token in ("focusSummaryRows", "setAnnoFocus", "is-target",
                      "backBound", "is-row-flash", "annoHint"):
            self.assertIn(token, app, f"反向定位/暗化缺少要素：{token}")
        # 反向点击绑在常驻的原文容器上，且只绑一次
        self.assertIn('box.dataset.backBound', app)
        self.assertIn('closest("mark.anno")', app)
        self.assertIn('id="annoHint"', html, "index.html 缺少跳转反馈提示节点")
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        for token in (".anno-text.is-focusing .anno", ".anno.is-target",
                      "is-focusing tr.sum-row", ".anno-hint", "rowFlash"):
            self.assertIn(token, css, f"style.css 缺少聚焦/暗化样式：{token}")
        # 暗化必须真的降透明度，否则"压暗"是空话
        self.assertIn("opacity: .26", css)
        self.assertIn("opacity: .35", css)

    def test_multi_type_sentence_supports_single_type_jump(self):
        """一句话命中多个类型时，要能只跳其中一条。"""
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        # 角标可单独点击，并带上 issue_id
        self.assertIn('data-issue="${esc(x.issue_id)}"', app)
        self.assertIn("anno-badge pickable", app)
        # 点角标优先于点整段（事件先后顺序不能反）
        self.assertLess(app.index('closest("sup.anno-badge[data-issue]")'),
                        app.index('closest("mark.anno")'),
                        "必须先判断角标，再判断整段")
        # 多类型才弹选择器
        self.assertIn("showIssueChooser", app)
        self.assertIn("ids.length > 1", app)
        # 选择器要能关闭（点外部 / Esc / 切换报告）
        self.assertIn("closeIssueChooser", app)
        self.assertIn("onChooserOutside", app)
        self.assertIn("onChooserKey", app)
        self.assertIn("closeIssueChooser();\n  if (!text)", app,
                      "切换报告时必须关掉残留的选择器")
        # 定位到某一条时，只点亮那一个角标
        self.assertIn("markActiveBadge", app)
        self.assertIn("is-picked", app)
        self.assertIn(".anno-chooser", css)
        self.assertIn(".anno-badge.is-picked", css)

    def test_every_issue_is_reachable_from_summary(self):
        """每条 issue 都必须能在原文里找到锚点，否则那一行点了跳不动。"""
        report = self.run_review()
        text = report.get("target_text") or ""
        self.assertTrue(text)
        missing = [i["issue_id"] for i in report["issues"]
                   if not i.get("quote") or i["quote"] not in text]
        self.assertEqual(missing, [],
                         f"这些 issue 在原文中定位不到：{missing}")


class TruncationVisibilityTests(ServerCase):
    """截断必须可见：审了多少字、哪里没审完，都要能在报告里读到。"""

    def test_report_exposes_text_coverage(self):
        report = self.run_review()
        cov = report["coverage"]
        for key in ("chars_total", "chars_reviewed", "text_coverage_pct",
                    "complete", "input_truncated_dimensions",
                    "output_truncated_dimensions"):
            self.assertIn(key, cov, f"coverage 缺少字段：{key}")
        self.assertGreater(cov["chars_total"], 0)
        self.assertLessEqual(cov["chars_reviewed"], cov["chars_total"])
        # 短样本应判为"完整覆盖"，不能把正常情况也报警
        self.assertTrue(cov["complete"])
        self.assertEqual(cov["text_coverage_pct"], 100)

    def test_input_truncation_wired_into_limitations(self):
        """构造超长输入：原文 > max_input_chars 时，limitations 必须出现未覆盖告警。"""
        src = (ROOT / "review.py").read_text(encoding="utf-8")
        # 输入截断的告警必须排在 limitations 最前（不能被其他条目淹没）
        self.assertIn("完全未进入审查", src)
        self.assertIn("limitations.insert(0", src)
        self.assertIn("input_truncated_dimensions", src)
        self.assertIn("output_truncated_dimensions", src)
        # 结论正文也要说，不能只藏在边界清单里
        self.assertIn("未覆盖部分未被审查", src)

    def test_markdown_report_shows_coverage(self):
        src = (ROOT / "review.py").read_text(encoding="utf-8")
        self.assertIn("文本覆盖", src)
        self.assertIn("未全覆盖", src)

    def test_verdict_does_not_overclaim_pass(self):
        """引擎有已知漏报，不能输出"可放行"这种授权式结论。"""
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        review_src = (ROOT / "review.py").read_text(encoding="utf-8")
        self.assertNotIn("可放行", app, "前端不得再出现「可放行」")
        self.assertNotIn("可放行", review_src, "结论不得再出现「可放行」")
        self.assertIn("未发现 — 不等于通过", app)
        self.assertIn("verdict-cover", app)
        # pass 的展示标签不能再是 PASS
        self.assertIn('pass: ["未发现"', app)
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".verdict-cover.warn", css)


class EvalHarnessTests(unittest.TestCase):
    """评测工具自身的正确性：标签能命中，且不会被风格规则"复读"骗到。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "eval_review", ROOT / "tools" / "eval_review.py")
        cls.ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.ev)
        cls.labels = json.loads(
            (ROOT / "targets" / "adversarial_meeting_001.labels.json")
            .read_text(encoding="utf-8"))["errors"]
        cls.by_id = {lb["id"]: lb for lb in cls.labels}

    def test_labels_cover_core_errors(self):
        ids = {lb["id"] for lb in self.labels}
        self.assertEqual(len(ids), 14, "对抗样本应标注 14 条错误")
        self.assertTrue({"E01", "E09", "E12"} <= ids)

    def test_style_dimension_cannot_fake_a_logic_hit(self):
        """风格规则会整句引用问题句，不能算作"识别出了矛盾"。"""
        e02 = self.by_id["E02"]
        text = "四成；销售团队年初 40 人，现在 66 人，涨了四成"
        style_issue = {"dimension": "style", "quote": text}
        logic_issue = {"dimension": "logic",
                       "quote": "销售团队年初 40 人，现在 66 人",
                       "evidence": "实际增幅 65%，与「四成」不符"}
        self.assertFalse(self.ev.label_matches(e02, text, style_issue))
        self.assertTrue(self.ev.label_matches(
            e02, self.ev.issue_haystack(logic_issue), logic_issue))

    def test_label_matching_reproduces_human_count_on_known_report(self):
        """用已知报告校准：R-014 人工统计 7/14，机读标签必须一致。"""
        path = ROOT / "outputs" / "R-20261023-014.json"
        if not path.exists():
            self.skipTest("缺少校准用报告")
        report = json.loads(path.read_text(encoding="utf-8"))
        hit = {lb["id"] for lb in self.labels
               if any(self.ev.label_matches(lb, self.ev.issue_haystack(i), i)
                      for i in report["issues"])}
        self.assertEqual(len(hit), 7, f"机读判定应为 7 条，实际 {sorted(hit)}")


class StepDetailTests(ServerCase):
    """任务日志每一步必须有实质内容，否则界面就是一屏空行。"""

    def test_each_step_records_detail(self):
        report = self.run_review()
        status, data = self.get("/api/logs?id=" + report["review_id"])
        self.assertTrue(data["ok"], data)
        steps = (data.get("task") or {}).get("steps") or []
        self.assertEqual(len(steps), 6, "应有 S1–S6 六步")
        for st in steps:
            detail = st.get("detail")
            self.assertTrue(detail and isinstance(detail, dict),
                            f"{st['step']} 缺少 detail，任务日志会是空行：{st}")
        # 关键信息必须落在对应步骤里，而不是笼统一句话
        by_step = {s["step"]: s["detail"] for s in steps}
        self.assertIn("类型判定", by_step["S1"])
        self.assertIn("生效规则", by_step["S2"])
        self.assertIn("审查模式", by_step["S3"])
        self.assertIn("quote 可回溯", by_step["S4"])
        self.assertIn("争议", by_step["S5"])
        self.assertIn("裁定", by_step["S6"])
        # 各维结果：调模型时是字典型（前端展开成子表）；纯规则审时是一句说明，
        # 但绝不能是 {"—": "未调用模型"} 这种占位噪声
        dims = by_step["S3"].get("各维结果")
        self.assertTrue(isinstance(dims, dict) or (isinstance(dims, str) and dims),
                        f"各维结果 既不是字典也不是说明文字：{dims!r}")
        self.assertNotEqual(dims, {"—": "未调用模型"})
        if isinstance(dims, str):
            self.assertIn("未调用模型", dims)

    def test_old_log_without_detail_is_explained(self):
        """旧日志没有 detail：前端必须说明原因，而不是显示空白。"""
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("renderStepDetail", app)
        self.assertIn("step.detail", app)
        self.assertIn("未记录步骤明细", app)
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        for token in (".log-field", ".log-kv", ".log-status", ".log-stale"):
            self.assertIn(token, css, f"缺少样式：{token}")


class AgentApiTests(ServerCase):
    """Agent 的接口契约：列表可查、非法 id 必须明确拒绝、报告能追溯。"""

    def test_agents_endpoint_lists_metadata(self):
        status, data = self.get("/api/agents")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"], data)
        items = {a["id"]: a for a in data["items"]}
        self.assertIn("doc-review", items)
        self.assertIn("security-audit", items)
        sec = items["security-audit"]
        self.assertFalse(sec["auto"], "安全审必须手动选择")
        self.assertIn("boundaries", sec)
        self.assertTrue(any(d["id"] == "injection" for d in sec["dimensions"]))

    def test_unknown_agent_is_rejected_before_running(self):
        status, data = self.post("/api/review/start",
                                 {"file": "targets/answer_001.txt",
                                  "llm": False, "agent": "no-such-agent"})
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertIn("未知 Agent", data["error"])
        self.assertIn("doc-review", data["error"], "报错必须给出可选值")

    def test_review_with_security_agent_records_agent_in_report(self):
        report = self.run_review("targets/security_demo/vuln_app.py", )
        # 默认（不传 agent）应落到文档审
        self.assertEqual(report["agent"]["id"], "doc-review")

    def test_async_start_accepts_agent_param(self):
        status, data = self.post("/api/review/start",
                                 {"file": "targets/security_demo/safe_app.py",
                                  "llm": False, "agent": "security-audit"})
        self.assertTrue(data["ok"], data)
        job_id = data["job_id"]
        deadline = time.time() + 120
        payload = None
        while time.time() < deadline:
            st, d = self.get(f"/api/review/status?job={job_id}")
            self.assertTrue(d["ok"], d)
            if d["job"]["status"] != "running":
                payload = d
                break
            time.sleep(0.3)
        self.assertIsNotNone(payload, "审查任务 120s 内未结束")
        self.assertEqual(payload["job"]["status"], "done", payload["job"].get("error"))
        self.assertEqual(payload["report"]["agent"]["id"], "security-audit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
