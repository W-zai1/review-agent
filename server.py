#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 审查 Agent 本地 Web 控制台（零依赖，仅标准库）

提供：
  GET  /                          -> web/index.html
  GET  /static/<path>             -> web/ 下的静态资源
  GET  /api/targets               -> 列出 targets/ 与 targets/uploads/ 下待审文件
  GET  /api/reports               -> 列出 outputs/ 下历史报告（按用户自定义顺序）
  GET  /api/report?id=R-xxx       -> 读取单份报告 JSON
  DELETE /api/report?id=R-xxx     -> 删除单份报告（须 body 传 confirm=id 二次确认）
  GET  /api/integrity             -> 校验 ledger 链 + 每份报告自洽
  GET  /api/rules                 -> 规则库列表（含启用状态）
  GET  /api/logs?id=R-xxx         -> 读取单次审查的四类日志
  GET  /api/review/status?job=x   -> 查询异步审查任务的进度与报告
  GET  /api/llm/status            -> 大模型配置状态（key 仅 masked）
  GET  /api/llm/providers         -> 常见服务商 / 本地模型预设
  POST /api/rules/toggle          -> 启用/停用某条规则
  POST /api/upload?name=x.txt     -> 上传 UTF-8 文本到 targets/uploads/
  POST /api/review/start          -> 启动异步审查（带 S1~S6 进度）
  POST /api/reports/order         -> 保存历史报告的拖拽排序结果
  POST /api/llm/config            -> 保存前端配置到 config/llm.local.json
  POST /api/llm/test              -> 用草稿配置试连一次（不落盘）
  POST /api/llm/reset             -> 清除本地覆盖，回到 YAML/环境变量
  POST /api/review  {file, llm}   -> 同步运行 review.py，返回最新报告

运行：
  python server.py [--port 8000]
"""
import sys
import os
import re
import json
import glob
import time
import uuid
import shutil
import threading
import subprocess
from dataclasses import replace
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
TARGETS = ROOT / "targets"
UPLOADS = TARGETS / "uploads"
OUTPUTS = ROOT / "outputs"
LEDGER = ROOT / "ledger"
LOG_DIR = ROOT / "logs"
REPORT_ORDER = ROOT / "config" / "report_order.json"   # 历史报告的拖拽排序
TRASH = OUTPUTS / ".trash"                              # 已删报告回收区（可恢复）
LLM_HISTORY = ROOT / "config" / "llm_history.json"     # LLM 配置记忆（最近用过的）
LLM_HISTORY_MAX = 12

UPLOADS.mkdir(parents=True, exist_ok=True)
UPLOAD_MAX_BYTES = 2 * 1024 * 1024
UPLOAD_MAX_BINARY_BYTES = 20 * 1024 * 1024      # PDF / Word / 图片 / 压缩包的上限
UPLOAD_ALLOWED = {
    # 文本 / 代码（可直接审查）
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".json", ".yaml", ".yml", ".html", ".css", ".csv", ".log", ".xml",
    ".go", ".java", ".cpp", ".c",
    # 二进制文档（摄取成文本后审查）
    ".pdf", ".docx", ".docm", ".doc", ".rtf",
    # 图片（未做 OCR，仅登记）
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic",
    # 压缩包（递归展开）
    ".zip",
}
# 需要走摄取流程（二进制）的扩展名
UPLOAD_BINARY_EXTS = {
    ".pdf", ".docx", ".docm", ".doc", ".rtf",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic",
    ".zip",
}
# 可选对象类型随后续 import 一并计算（见下方 TARGET_TYPES）
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

JOBS_DIR = LOG_DIR / ".jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
JOBS = {}
JOBS_LOCK = threading.Lock()

def resolve_python():
    """定位运行 review.py 的解释器：环境变量 > 项目 .venv > 继承当前解释器。

    不再硬编码某台机器上的绝对路径。
    """
    env = os.environ.get("REVIEW_AGENT_PY")
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    for cand in (
        ROOT / ".venv" / "Scripts" / "python.exe",  # Windows venv
        ROOT / ".venv" / "bin" / "python",          # macOS / Linux venv
    ):
        if cand.exists():
            return cand
    return Path(sys.executable)


PY = resolve_python()

sys.path.insert(0, str(ROOT / "tools"))
import integrity  # noqa: E402
import ingest  # noqa: E402
import agent_spec  # noqa: E402
import llm_client  # noqa: E402
import rules_store  # noqa: E402

# 可选对象类型 = 所有 Agent 声明过的类型并集。
# 写死的四个值意味着「加一个场景就得改服务端」，这正是这次要拆掉的东西。
TARGET_TYPES = set(agent_spec.all_target_types()) or {
    "llm_response", "prompt", "contract", "code"}

SEV_LABEL = {"blocker": "阻断", "major": "严重", "minor": "轻微", "nit": "提示"}
def _all_dim_labels():
    """维度标签 = 各 Agent 声明的并集（新维度不会在规则表里显示成英文 id）。"""
    out = {"fact": "事实", "logic": "逻辑", "safety": "安全",
           "compliance": "合规", "style": "风格"}
    for a in agent_spec.list_agents():
        for d in a.get("dimensions") or []:
            out.setdefault(d["id"], d["label"])
    return out


DIM_LABEL = _all_dim_labels()


def _ctype(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }.get(ext, "application/octet-stream")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_llm_history():
    """读取 LLM 配置记忆（最近用过的端点 / 模型）。"""
    try:
        data = json.loads(LLM_HISTORY.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _remember_llm(base_url, model, api_key_set=False):
    """把这次用过的配置记进历史，方便下次一键切回。

    只记端点与模型，**不记 API Key 明文**（只记是否设置过）。
    """
    base_url = str(base_url or "").strip().rstrip("/")
    model = str(model or "").strip()
    if not base_url or not model:
        return
    items = [h for h in _load_llm_history()
             if not (h.get("base_url") == base_url and h.get("model") == model)]
    items.insert(0, {
        "base_url": base_url,
        "model": model,
        "api_key_set": bool(api_key_set),
        "last_used_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    LLM_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    tmp = LLM_HISTORY.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[:LLM_HISTORY_MAX], ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(str(tmp), str(LLM_HISTORY))


WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.I)


def _safe_upload_name(raw):
    """只保留 basename，去掉路径分隔符与 Windows 非法字符。

    还要拒绝 Windows 保留设备名：写 `CON.txt` 不会报错，但文件根本不存在，
    接口却返回成功——用户以为传上去了，实际待审列表里没有（第三轮 F3）。
    """
    name = str(raw or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        return ""
    stem, ext = os.path.splitext(name)
    stem = stem[:80] or "upload"
    if WINDOWS_RESERVED.match(stem):
        stem = f"_{stem}"
    return f"{stem}{ext[:10]}"


def _unique_upload_path(name, base=None):
    base = base or UPLOADS
    path = base / name
    if not path.exists():
        return path
    stem, ext = os.path.splitext(name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i in range(1, 100):
        suffix = "" if i == 1 else f"-{i}"
        candidate = base / f"{stem}-{stamp}{suffix}{ext}"
        if not candidate.exists():
            return candidate
    return base / f"{stem}-{int(time.time() * 1000)}{ext}"


def _resolve_target(file):
    """只允许审查 targets/ 下的真实文件，同步/异步接口共用。"""
    if not file:
        return None, "缺少 file 参数"
    target = (ROOT / str(file)).resolve()
    try:
        target.relative_to(TARGETS.resolve())
    except ValueError:
        return None, "只能审查 targets/ 下的文件"
    if not target.is_file():
        return None, f"文件不存在：{file}"
    return target, None


def _hostname_from_header(value):
    value = str(value or "").strip().lower()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if ":" in value:
        return value.rsplit(":", 1)[0]
    return value


def _origin_host(origin):
    try:
        return (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "ReviewAgentConsole/0.1"

    # ----- 通用响应 -----
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        if not path.exists() or not str(path.resolve()).startswith(str(WEB.resolve())):
            return self._send(404, {"error": "not found"})
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _ctype(path))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _guard_api(self):
        """拒绝非本机 Host、跨站 Origin 与 cross-site 请求（CSRF / DNS rebinding）。"""
        host = _hostname_from_header(self.headers.get("Host"))
        if host and host not in LOCAL_HOSTS:
            self._send(403, {"ok": False, "error": "Host 不被允许"})
            return False
        origin = self.headers.get("Origin")
        if origin and _origin_host(origin) not in LOCAL_HOSTS:
            self._send(403, {"ok": False, "error": "跨站请求已被拒绝"})
            return False
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            self._send(403, {"ok": False, "error": "跨站请求已被拒绝"})
            return False
        return True

    def log_message(self, fmt, *args):  # 安静日志
        pass

    # ----- GET -----
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p.startswith("/api/") and not self._guard_api():
            return
        if p in ("/", "/index.html"):
            return self._send_file(WEB / "index.html")
        if p.startswith("/static/"):
            fp = (WEB / p[len("/static/"):]).resolve()
            return self._send_file(fp)
        if p == "/api/targets":
            return self._send(200, self._targets())
        if p == "/api/reports":
            return self._send(200, self._reports())
        if p == "/api/integrity":
            return self._send(200, self._integrity())
        if p == "/api/llm/status":
            return self._send(200, self._llm_status())
        if p == "/api/llm/providers":
            return self._send(200, {"ok": True, "items": llm_client.list_providers()})
        if p == "/api/llm/history":
            return self._send(200, {"ok": True, "items": _load_llm_history()})
        if p == "/api/report":
            q = parse_qs(u.query)
            rid = q.get("id", [None])[0]
            return self._send(200, self._report(rid))
        if p == "/api/agents":
            return self._send(200, {"ok": True, "items": agent_spec.list_agents()})
        if p == "/api/rules":
            return self._send(200, self._rules())
        if p == "/api/logs":
            q = parse_qs(u.query)
            rid = q.get("id", [None])[0]
            return self._send(200, self._logs(rid))
        if p == "/api/review/status":
            q = parse_qs(u.query)
            job_id = q.get("job", [None])[0]
            return self._send(200, self._review_status(job_id))
        return self._send(404, {"error": "not found"})

    # ----- POST -----
    def do_POST(self):
        u = urlparse(self.path)
        if not self._guard_api():
            return
        if u.path == "/api/upload":
            return self._send(200, self._upload(u))
        data = self._json_body()
        if data is None:            # 超大 body：响应已发出（413），直接结束
            return
        if u.path == "/api/review":
            return self._send(200, self._run_review(data))
        if u.path == "/api/review/start":
            return self._send(200, self._start_review(data))
        if u.path == "/api/rules/toggle":
            return self._send(200, self._toggle_rule(data))
        if u.path == "/api/llm/config":
            return self._send(200, self._save_llm_config(data))
        if u.path == "/api/llm/test":
            return self._send(200, self._test_llm(data))
        if u.path == "/api/llm/reset":
            return self._send(200, self._reset_llm_config())
        if u.path == "/api/reports/order":
            return self._send(200, self._save_report_order(data))
        if u.path == "/api/llm/history/restore":
            return self._send(200, self._restore_llm_history(data))
        return self._send(404, {"error": "not found"})

    # ----- DELETE -----
    def do_DELETE(self):
        u = urlparse(self.path)
        if not self._guard_api():
            return
        if u.path == "/api/report":
            rid = parse_qs(u.query).get("id", [None])[0]
            data = self._json_body()
            return self._send(200, self._delete_report(rid, data))
        return self._send(404, {"error": "not found"})

    MAX_JSON_BODY = 1 * 1024 * 1024        # JSON 接口 1MB 上限（与文本 2MB / 二进制 20MB 对称）

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > self.MAX_JSON_BODY:
            # 读入前就拒，避免把超大 body 全量读进内存
            self._send(413, {"ok": False,
                             "error": f"请求体超过 {self.MAX_JSON_BODY // 1024} KB 上限"})
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ----- 业务 -----
    def _targets(self):
        items = []
        paths = [p for p in sorted(TARGETS.glob("*")) if p.is_file()]
        paths += [p for p in sorted(UPLOADS.glob("*")) if p.is_file()]
        for p in paths:
            st = p.stat()
            items.append({
                "name": p.name,
                "rel": p.relative_to(ROOT).as_posix(),
                "size": st.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "uploaded": p.parent == UPLOADS,
            })
        return {"items": items}

    def _upload(self, u):
        """接收原始字节上传：只收 UTF-8 文本/代码，落到 targets/uploads/。"""
        q = parse_qs(u.query)
        name = _safe_upload_name((q.get("name") or [""])[0])
        if not name:
            return {"ok": False, "error": "文件名无效"}
        ext = os.path.splitext(name)[1].lower()
        if ext not in UPLOAD_ALLOWED:
            return {"ok": False, "error": f"不支持的文件类型：{ext or '无扩展名'}"}
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {"ok": False, "error": "文件为空"}
        is_binary = ext in UPLOAD_BINARY_EXTS
        limit = UPLOAD_MAX_BINARY_BYTES if is_binary else UPLOAD_MAX_BYTES
        if length > limit:
            self.close_connection = True
            return {"ok": False, "error": f"文件超过 {limit // (1024 * 1024)} MB 限制"}
        data = self.rfile.read(length)

        if not is_binary:
            # —— 文本路径：严格校验（UTF-8 / 拒 NUL / 剥 BOM）——
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return {"ok": False, "error": "仅支持 UTF-8 编码的文本文件"}
            if chr(0) in text:
                return {"ok": False, "error": "疑似二进制文件，已拒绝"}
            if text.startswith(chr(0xFEFF)):
                text = text[1:]
            payload = text.encode("utf-8")
            target = _unique_upload_path(name)
            try:
                target.write_bytes(payload)
            except Exception as e:
                return {"ok": False, "error": f"保存失败：{e}"}
            return {"ok": True, "kind": "text", "file": {
                "name": target.name,
                "rel": target.relative_to(ROOT).as_posix(),
                "size": len(payload),
            }}

        # ---------- 二进制路径：归档原件 + 摄取成文本 ----------
        originals = UPLOADS / "_originals"
        originals.mkdir(parents=True, exist_ok=True)
        original = _unique_upload_path(name, base=originals)
        try:
            original.write_bytes(data)
        except Exception as e:
            return {"ok": False, "error": f"保存失败：{e}"}

        work = UPLOADS / "_work"
        try:
            result = ingest.ingest_file(original, work_dir=work)
        except ingest.IngestError as e:
            return {"ok": False, "error": str(e),
                    "original": original.relative_to(ROOT).as_posix()}
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "error": f"文档解析失败：{e}",
                    "original": original.relative_to(ROOT).as_posix()}
        finally:
            shutil.rmtree(work, ignore_errors=True)

        meta_dir = UPLOADS / "_meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        extracted = []
        for out in result["outputs"]:
            # 压缩包成员可能带目录层级，压平后落盘，保证都能出现在待审列表里
            flat = re.sub(r"[\\/]+", "-", out["name"]).lstrip("-")
            target = _unique_upload_path(flat)
            try:
                target.write_text(out["text"], encoding="utf-8")
            except Exception as e:                    # noqa: BLE001
                return {"ok": False, "error": f"派生文本保存失败：{e}"}
            ometa = out.get("meta") or {}
            (meta_dir / f"{target.name}.json").write_text(
                json.dumps(ometa, ensure_ascii=False, indent=2), encoding="utf-8")
            extracted.append({
                "name": target.name,
                "rel": target.relative_to(ROOT).as_posix(),
                "chars": len(out["text"]),
                "from": ometa.get("source_name"),
                "archive": ometa.get("archive"),
            })

        return {
            "ok": True,
            "kind": result["kind"],
            "file": extracted[0] if extracted else None,
            "extracted": extracted,
            "warnings": list(result["meta"].get("warnings") or []),
            "extractor": result["meta"].get("extractor"),
            "original": original.relative_to(ROOT).as_posix(),
        }

    def _report_order(self):
        """读取用户自定义的历史报告顺序（拖拽排序结果）。"""
        try:
            data = json.loads(REPORT_ORDER.read_text(encoding="utf-8"))
        except Exception:
            return []
        order = data.get("order") if isinstance(data, dict) else data
        if not isinstance(order, list):
            return []
        return [str(x) for x in order]

    def _reports(self):
        items = []
        for p in sorted(OUTPUTS.glob("R-*.json"), reverse=True):
            try:
                r = _read_json(p)
            except Exception:
                continue
            inc = r.get("integrity", {})
            items.append({
                "id": r.get("review_id"),
                "verdict": r.get("verdict"),
                "target_type": r.get("target_type"),
                "target_path": r.get("target_path"),
                "target_name": Path(r.get("target_path") or "").name,
                "review_mode": r.get("review_mode"),
                "timestamp": r.get("timestamp"),
                "issues": len(r.get("issues", [])),
                "stats": r.get("stats", {}),
                "report_hash": (inc.get("report_hash") or "")[:16],
                "mtime": p.stat().st_mtime,
            })
        # 默认按 mtime 倒序；用户拖拽排序后以自定义顺序优先
        items.sort(key=lambda x: x.get("mtime", 0), reverse=True)
        order = self._report_order()
        rank = {rid: i for i, rid in enumerate(order)}
        items.sort(key=lambda x: (rank.get(x.get("id"), len(rank)), -(x.get("mtime") or 0)))
        return {"items": items, "order": order}

    def _save_report_order(self, data):
        """保存历史报告的拖拽排序结果（只接受仍然存在的报告 ID）。"""
        raw = (data or {}).get("order")
        if not isinstance(raw, list):
            return {"ok": False, "error": "order 必须是数组"}
        existing = {p.stem for p in OUTPUTS.glob("R-*.json")}
        cleaned = []
        for x in raw:
            rid = str(x or "")
            if re.fullmatch(r"R-\d{8}-\d{3}", rid) and rid in existing and rid not in cleaned:
                cleaned.append(rid)
        # 未出现在列表里但仍存在的报告按 mtime 倒序补到末尾，避免新报告“消失”
        rest = sorted((p for p in OUTPUTS.glob("R-*.json") if p.stem not in cleaned),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        cleaned += [p.stem for p in rest]
        REPORT_ORDER.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "order": cleaned,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = REPORT_ORDER.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(REPORT_ORDER))
        return {"ok": True, "order": cleaned, "count": len(cleaned)}

    def _delete_report(self, rid, data):
        """删除单份历史报告。

        二次确认：body 必须带 confirm=<报告ID>，与服务端要求完全一致才执行。
        审计链：ledger 对应行原位打上 op=delete 墓碑，seq / report_hash / prev_hash
                全部保持不变——链的衔接分毫不动，删除痕迹留在链上可审计。
        文件不硬删：移入 outputs/.trash/，误删可自行恢复。
        """
        if not rid:
            return {"ok": False, "error": "缺少 id"}
        if not re.fullmatch(r"R-\d{8}-\d{3}", str(rid)):
            return {"ok": False, "error": "报告 ID 格式不合法"}
        hit = OUTPUTS / f"{rid}.json"
        if not hit.exists():
            return {"ok": False, "error": f"未找到报告 {rid}"}

        confirm = str((data or {}).get("confirm") or "")
        if confirm != rid:
            return {"ok": False, "need_confirm": True,
                    "error": "删除需要二次确认：请提交 confirm=<报告ID>"}

        TRASH.mkdir(parents=True, exist_ok=True)
        moved = []
        for src in (OUTPUTS / f"{rid}.json", OUTPUTS / f"{rid}.md"):
            if src.exists():
                shutil.move(str(src), str(TRASH / src.name))
                moved.append(src.name)
        log_src = LOG_DIR / rid
        if log_src.is_dir():
            dst = TRASH / f"{rid}_logs"
            if dst.exists():
                shutil.rmtree(str(dst), ignore_errors=True)
            shutil.move(str(log_src), str(dst))
            moved.append(f"logs/{rid}/")

        tombstoned = self._mark_ledger_deleted(rid)
        # 顺序表里摘掉这个 id
        rest_order = [x for x in self._report_order() if x != rid]
        if rest_order != self._report_order():
            self._save_report_order({"order": rest_order})
        return {
            "ok": True,
            "id": rid,
            "moved": moved,
            "trash": "outputs/.trash/",
            "ledger_tombstone": tombstoned,
        }

    def _mark_ledger_deleted(self, rid):
        """在 ledger 对应行原位写墓碑；不改动 seq / report_hash / prev_hash。"""
        ledger = LEDGER / "chain.log"
        if not ledger.exists():
            return False
        lines = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        hit = False
        out = []
        for raw in lines:
            try:
                row = json.loads(raw)
            except Exception:
                out.append(raw)
                continue
            if row.get("review_id") == rid and row.get("op") != "delete":
                row["op"] = "delete"
                row["deleted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                row["deleted_hash"] = row.get("report_hash", "")
                hit = True
            out.append(json.dumps(row, ensure_ascii=False))
        if hit:
            tmp = ledger.with_suffix(".tmp")
            tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
            os.replace(str(tmp), str(ledger))
        return hit

    def _report(self, rid):
        if not rid:
            return {"ok": False, "error": "缺少 id"}
        if not re.fullmatch(r"R-\d{8}-\d{3}", str(rid)):
            return {"ok": False, "error": "报告 ID 格式不合法"}
        cand = OUTPUTS.glob(f"{rid}.json")
        hit = next(cand, None)
        if not hit:
            return {"ok": False, "error": f"未找到报告 {rid}"}
        return {"ok": True, "report": _read_json(hit)}

    def _rules(self):
        state = rules_store.load_state()
        order = {"fact": 0, "safety": 1, "compliance": 2, "style": 3}
        all_rules = sorted(
            rules_store.load_all_rules(),
            key=lambda x: (order.get(x.get("dimension"), 9), x.get("rule_id", "")),
        )
        items = []
        for r in all_rules:
            items.append({
                "rule_id": r["rule_id"],
                "dimension": r.get("dimension", ""),
                "desc": r.get("desc", ""),
                "severity": r.get("severity", "minor"),
                "method": r.get("method", ""),
                "source": r.get("source", ""),
                "file": r.get("file", ""),
                "enabled": state.get(r["rule_id"], True),
            })
        dims = {}
        for item in items:
            dims[item["dimension"]] = dims.get(item["dimension"], 0) + 1
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "enabled": sum(1 for i in items if i["enabled"]),
            "dimensions": dims,
        }

    def _toggle_rule(self, data):
        rule_id = str((data or {}).get("rule_id") or "").strip()
        if not rule_id:
            return {"ok": False, "error": "缺少 rule_id"}
        if "enabled" not in (data or {}):
            return {"ok": False, "error": "缺少 enabled"}
        value = data.get("enabled")
        if isinstance(value, str):
            enabled = value.strip().lower() not in ("0", "false", "no", "off")
        else:
            enabled = bool(value)
        try:
            rules_store.set_enabled(rule_id, enabled)
        except KeyError:
            return {"ok": False, "error": f"规则不存在：{rule_id}"}
        except Exception as e:
            return {"ok": False, "error": f"保存失败：{e}"}
        active = rules_store.load_active_rules()
        return {
            "ok": True,
            "rule_id": rule_id,
            "enabled": enabled,
            "enabled_count": len(active),
            "total": len(rules_store.load_all_rules()),
        }

    def _logs(self, rid):
        dirs = sorted(
            [p for p in LOG_DIR.glob("R-*") if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if rid:
            if not re.fullmatch(r"R-\d{8}-\d{3}", str(rid)):
                return {"ok": False, "error": "日志 ID 格式不合法"}
            folder = LOG_DIR / rid
        else:
            folder = dirs[0] if dirs else None
        if not folder or not folder.is_dir():
            # 区分「报告被删除（连同日志移入回收区）」与「日志从未存在」
            if rid and (TRASH / f"{rid}_logs").is_dir():
                return {"ok": False, "deleted": True,
                        "error": "该报告已被删除，日志已随报告移入 outputs/.trash/（可恢复）"}
            if rid and not (OUTPUTS / f"{rid}.json").exists():
                return {"ok": False, "deleted": True,
                        "error": "该报告已不存在（可能被删除或从未生成）"}
            return {"ok": False, "error": "未找到该次审查的日志"}

        def read_json(name):
            path = folder / name
            if not path.exists():
                return {}
            try:
                return _read_json(path)
            except Exception:
                return {}

        def read_jsonl(name):
            path = folder / name
            if not path.exists():
                return []
            rows = []
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
            except Exception:
                return []
            return rows

        return {
            "ok": True,
            "id": folder.name,
            "task": read_json("task.json"),
            "decisions": read_jsonl("decisions.jsonl"),
            "tools": read_jsonl("tools.jsonl"),
            "cost": read_json("cost.json"),
        }

    def _review_status(self, job_id):
        if not job_id:
            return {"ok": False, "error": "缺少 job 参数"}
        with JOBS_LOCK:
            job = dict(JOBS.get(job_id) or {})
        if not job:
            return {"ok": False, "error": "任务不存在或已过期"}
        steps = []
        progress = JOBS_DIR / f"{job_id}.jsonl"
        if progress.exists():
            try:
                for line in progress.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        steps.append(json.loads(line))
                    except Exception:
                        continue
            except Exception:
                pass
        report = None
        if job.get("status") == "done" and job.get("report_id"):
            path = OUTPUTS / f"{job['report_id']}.json"
            if path.exists():
                try:
                    report = _read_json(path)
                except Exception:
                    report = None
        return {"ok": True, "job": job, "steps": steps, "report": report}

    def _start_review(self, data):
        file = str((data or {}).get("file") or "").strip()
        target, error = _resolve_target(file)
        if error:
            return {"ok": False, "error": error}
        llm_opt = (data or {}).get("llm")
        if llm_opt not in (True, False, None):
            llm_opt = None
        type_opt = (data or {}).get("type")
        if type_opt not in TARGET_TYPES:
            type_opt = None
        agent_opt, agent_err = _resolve_agent((data or {}).get("agent"))
        if agent_err:
            return {"ok": False, "error": agent_err}
        with JOBS_LOCK:
            now = time.time()
            for old_id, old in list(JOBS.items()):
                if old.get("status") == "running":
                    return {"ok": False, "error": "已有审查任务进行中，请稍候"}
                if old.get("finished") and now - old["finished"] > 3600:
                    JOBS.pop(old_id, None)
                    try:
                        (JOBS_DIR / f"{old_id}.jsonl").unlink()
                    except OSError:
                        pass
            job_id = uuid.uuid4().hex[:12]
            progress = JOBS_DIR / f"{job_id}.jsonl"
            try:
                progress.unlink()
            except OSError:
                pass
            JOBS[job_id] = {
                "id": job_id,
                "file": file,
                "status": "running",
                "created": now,
                "finished": None,
                "report_id": None,
                "error": None,
                "llm": llm_opt,
                "type": type_opt,
                "agent": agent_opt,
            }
        threading.Thread(
            target=_run_review_job,
            args=(job_id, file, llm_opt, progress, type_opt, agent_opt),
            daemon=True,
        ).start()
        return {"ok": True, "job_id": job_id}

    def _integrity(self):
        ledger = LEDGER / "chain.log"
        if not ledger.exists():
            return {"ok": False, "chain_ok": False, "entries": [],
                    "reports": [], "detail": "ledger 尚未生成（运行一次审查后才有）"}
        lines = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        chain_ok = True
        prev = "0" * 64
        seq = 1
        broken_at = None
        entries = []
        for raw in lines:
            try:
                row = json.loads(raw)
            except Exception:
                chain_ok = False
                broken_at = broken_at or seq
                break
            row_ok = row.get("seq") == seq and row.get("prev_hash") == prev
            if not row_ok:
                chain_ok = False
                if broken_at is None:
                    broken_at = seq
            entries.append({
                "seq": row.get("seq"),
                "review_id": row.get("review_id"),
                "report_hash": row.get("report_hash", ""),
                "report_hash_short": (row.get("report_hash") or "")[:12],
                "prev_hash": row.get("prev_hash", ""),
                "prev_hash_short": (row.get("prev_hash") or "")[:12],
                "ts": row.get("ts", ""),
                "ok": row_ok,
                # op=delete 为墓碑行：报告已被用户删除，链位保留、痕迹可审计
                "op": row.get("op", "report"),
                "deleted": row.get("op") == "delete",
                "deleted_at": row.get("deleted_at"),
            })
            prev = row.get("report_hash", "")
            seq += 1

        reports = []
        cur_rules_ver = integrity.rules_version()
        for p in sorted(OUTPUTS.glob("R-*.json")):
            try:
                r = _read_json(p)
                stored = r.get("integrity", {}).get("report_hash")
                # 复用 CLI 的校验实现：两边各写一套必然漂移——
                # 实测控制台只查了 rules_dir_hash，于是"CLI 报 87 份 stale、
                # 控制台全绿"，用户看到的和实际状态相反。
                ok, detail, stale = integrity.verify_report(str(p), str(ROOT))
                reports.append({
                    "id": r.get("review_id"),
                    "ok": ok,
                    "rules_ok": ok,     # 兼容旧前端字段：自洽通过即视为规则可接受
                    "stale": stale,
                    "rules_version": r.get("integrity", {}).get("rules_version"),
                    "verdict": r.get("verdict"),
                    "target_path": r.get("target_path"),
                    "hash_short": (stored or "")[:12],
                    "detail": "" if ok else (detail if isinstance(detail, str)
                                             else "；".join(detail)),
                })
            except Exception as e:
                reports.append({"id": p.name, "ok": False, "error": str(e)})
        # 报告内容被改（自洽失败）时，把对应链节点也标红
        report_state = {r.get("id"): r.get("ok") for r in reports}
        for entry in entries:
            if report_state.get(entry.get("review_id")) is False:
                entry["ok"] = False
        ok = chain_ok and all(x.get("ok") for x in reports)
        stale_count = sum(1 for x in reports if x.get("stale"))
        tampered = [x.get("id") for x in reports if not x.get("ok")]
        detail = ""
        if not ok:
            detail = ("审计链断裂（断点 seq %s）" % broken_at) if not chain_ok else \
                     ("报告校验失败：%s" % "、".join(tampered[:5]))
        return {
            "ok": ok,
            "chain_ok": chain_ok,
            "entries": entries,
            "reports": reports,
            "broken_at": broken_at,
            "stale_count": stale_count,
            "rules_version": cur_rules_ver,
            "detail": detail,
        }

    def _llm_status(self):
        """只读暴露 LLM 配置状态；密钥始终 masked。"""
        try:
            cfg = llm_client.load_config()
            data = cfg.public_dict()
            data["ok"] = True
            return data
        except Exception as e:
            return {"ok": False, "available": False, "error": str(e)}

    def _save_llm_config(self, data):
        """保存前端配置；只写 config/llm.local.json，不改 YAML。"""
        patch = {}
        errors = []

        if "base_url" in data:
            base_url = str(data.get("base_url") or "").strip().rstrip("/")
            if not (base_url.startswith("http://") or base_url.startswith("https://")):
                errors.append("Base URL 必须以 http:// 或 https:// 开头")
            elif len(base_url) > 300:
                errors.append("Base URL 过长")
            else:
                patch["base_url"] = base_url

        if "model" in data:
            model = str(data.get("model") or "").strip()
            if not model or len(model) > 120:
                errors.append("模型名不能为空，且不超过 120 字符")
            else:
                patch["model"] = model

        if data.get("clear_key"):
            patch["api_key"] = None
        else:
            api_key = str(data.get("api_key") or "").strip()
            if api_key:
                if len(api_key) > 500:
                    errors.append("API Key 过长")
                else:
                    patch["api_key"] = api_key

        if "enabled" in data:
            value = data.get("enabled")
            if isinstance(value, str):
                patch["enabled"] = value.strip().lower() not in ("0", "false", "no", "off")
            else:
                patch["enabled"] = bool(value)

        for field, low, high, label in (
            ("timeout", 1, 600, "超时"),
            ("max_tokens", 16, 8192, "max_tokens"),
        ):
            if field not in data:
                continue
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                errors.append(f"{label} 必须是整数")
                continue
            if not low <= value <= high:
                errors.append(f"{label} 需在 {low}-{high} 之间")
            else:
                patch[field] = value

        # 防止把旧服务商的 Key 发给新 host
        try:
            old = llm_client.load_config()
        except Exception:
            old = None
        if old is not None and old.api_key:
            new_base = patch.get("base_url", old.base_url)
            old_host = (urlsplit(old.base_url).hostname or "").lower()
            new_host = (urlsplit(new_base).hostname or "").lower()
            key_supplied = "api_key" in patch and patch["api_key"] not in (None, "")
            clearing = bool(data.get("clear_key"))
            if new_host != old_host and not key_supplied and not clearing:
                errors.append(
                    "切换服务商后请重新填写 API Key，或勾选「清除已保存的 Key」")

        if errors:
            return {"ok": False, "error": "；".join(errors)}
        if not patch:
            return {"ok": True, "saved": False,
                    "status": llm_client.load_config().public_dict()}
        try:
            llm_client.save_local_config(patch)
        except Exception as e:
            return {"ok": False, "error": f"保存失败：{e}"}
        # 记入配置记忆：下次打开弹窗能一键切回
        try:
            _remember_llm(
                patch.get("base_url") or (old.base_url if old else ""),
                patch.get("model") or (old.model if old else ""),
                api_key_set=bool(llm_client.load_config().api_key))
        except Exception:
            pass
        return {"ok": True, "saved": True,
                "status": llm_client.load_config().public_dict()}

    def _restore_llm_history(self, data):
        """从配置记忆切回某个端点/模型。"""
        base_url = str((data or {}).get("base_url") or "").strip().rstrip("/")
        model = str((data or {}).get("model") or "").strip()
        if not base_url or not model:
            return {"ok": False, "error": "缺少 base_url 或 model"}
        if not base_url.startswith(("http://", "https://")):
            return {"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}
        try:
            old = llm_client.load_config()
        except Exception:
            old = None
        key = str((data or {}).get("api_key") or "").strip()
        if old is not None and old.api_key and not key:
            old_host = (urlsplit(old.base_url or "").hostname or "").lower()
            new_host = (urlsplit(base_url).hostname or "").lower()
            if old_host != new_host:
                return {"ok": False,
                        "error": "该端点需要 API Key，请先在表单里填写后再切换"}
        patch = {"base_url": base_url, "model": model}
        if key:
            patch["api_key"] = key
        try:
            llm_client.save_local_config(patch)
        except Exception as e:
            return {"ok": False, "error": f"保存失败：{e}"}
        _remember_llm(base_url, model,
                      api_key_set=bool(key) or bool(old and old.api_key))
        return {"ok": True, "status": llm_client.load_config().public_dict()}

    def _test_llm(self, data):
        """用前端草稿配置试连，不落盘；Key 为空时沿用已保存的 Key。"""
        try:
            base = llm_client.load_config()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
            return {"ok": False, "error": "Base URL 必须以 http:// 或 https:// 开头"}

        # 草稿优先：用户刚填的超时与 token 预算必须生效。
        # 思考型模型(deepseek-r1 / qwen3 系列)思考链会先吃满 max_tokens，
        # 且思考本身耗时可达数百秒——沿用已保存配置里的旧超时会让「测试连接」
        # 必然超时，而用户看到的现象是「连上了却又提示超时」，很难自查。
        try:
            draft_timeout = float(data.get("timeout") or base.timeout or 40)
        except (TypeError, ValueError):
            draft_timeout = float(base.timeout or 40)
        draft_timeout = max(1.0, min(draft_timeout, 600.0))
        cfg = replace(base, enabled=True, timeout=draft_timeout, max_retries=0)
        try:
            draft_max_tokens = int(data.get("max_tokens") or base.max_tokens)
        except (TypeError, ValueError):
            draft_max_tokens = int(base.max_tokens)
        if draft_max_tokens:
            cfg.max_tokens = draft_max_tokens
        if base_url:
            cfg.base_url = base_url
        model = str(data.get("model") or "").strip()
        if model:
            cfg.model = model
        if data.get("clear_key"):
            cfg.api_key = ""
        else:
            key = str(data.get("api_key") or "").strip()
            if key:
                cfg.api_key = key

        if not cfg.available:
            return {"ok": False, "error": "缺少 API Key（且不是本地端点），无法测试"}

        client = llm_client.LLMClient(cfg)
        try:
            # 探针预算必须大于思考链长度（deepseek-r1 约 525 token），
            # 否则思考型模型永远返回空正文，用户会误以为"连上了却没反应"。
            probe_tokens = max(1024, min(int(cfg.max_tokens or 1024), 2048))
            res = client.chat(
                [{"role": "user", "content": "只回复两个字：可用"}],
                temperature=0, max_tokens=probe_tokens)
        except llm_client.LLMError as e:
            msg = str(e)
            if "timed out" in msg or "超时" in msg:
                msg = (f"请求超时（{int(cfg.timeout)}s 内未返回）——"
                       "思考型模型思考链可能很长，请调大「超时（秒）」后重试")
            return {"ok": False, "error": msg, "model": cfg.model,
                    "endpoint": cfg.endpoint, "timeout": cfg.timeout}
        reply = (res.content or "").strip()
        # 连通性验证通过也算「用过」：记入配置记忆，下次可一键切回
        try:
            _remember_llm(cfg.base_url, cfg.model, api_key_set=bool(cfg.api_key))
        except Exception:
            pass
        return {
            "ok": True,
            "model": res.model,
            "endpoint": cfg.endpoint,
            "latency_ms": res.latency_ms,
            "usage": res.usage,
            "reply": reply[:200],
            "timeout": cfg.timeout,
            "max_tokens": cfg.max_tokens,
            "note": (None if reply else
                     "端点连通但正文为空：思考型模型在 max_tokens 内未产出最终回答，"
                     "请调大 max_tokens 或改用非思考模型"),
        }

    def _reset_llm_config(self):
        try:
            llm_client.clear_local_config()
            return {"ok": True, "status": llm_client.load_config().public_dict()}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _run_review(self, data):
        file = str((data or {}).get("file") or "").strip()
        target, error = _resolve_target(file)
        if error:
            return {"ok": False, "error": error}
        with JOBS_LOCK:
            if any(j.get("status") == "running" for j in JOBS.values()):
                return {"ok": False, "error": "已有审查任务进行中，请稍候"}
        cmd = [str(PY), str(ROOT / "review.py"), file]
        llm_opt = (data or {}).get("llm")
        if llm_opt is True:
            cmd.append("--llm")
        elif llm_opt is False:
            cmd.append("--no-llm")
        type_opt = (data or {}).get("type")
        if type_opt in TARGET_TYPES:
            cmd.extend(["--type", type_opt])
        agent_opt, agent_err = _resolve_agent((data or {}).get("agent"))
        if agent_err:
            return {"ok": False, "error": agent_err}
        if agent_opt:
            cmd.extend(["--agent", agent_opt])
        before = {p.name for p in OUTPUTS.glob("R-*.json")}
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=300,
            )
        except Exception as e:
            return {"ok": False, "error": f"运行失败：{e}"}
        elapsed = round(time.time() - t0, 1)
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout)[-2500:],
                    "returncode": proc.returncode}
        after = {p.name for p in OUTPUTS.glob("R-*.json")}
        new = after - before
        if not new:
            return {"ok": False, "error": "审查完成但未生成报告"}
        # 取最新生成的报告
        latest = max(OUTPUTS.glob("R-*.json"), key=lambda p: p.stat().st_mtime)
        report = _read_json(latest)
        return {"ok": True, "report": report, "id": report.get("review_id"),
                "elapsed": elapsed}


def _resolve_agent(raw):
    """校验前端传来的 agent id；空值表示走自动路由。"""
    if raw in (None, "", "auto"):
        return None, None
    known = {a["id"] for a in agent_spec.list_agents()}
    if raw not in known:
        return None, f"未知 Agent：{raw}；可选：{'、'.join(sorted(known))}"
    return str(raw), None


def _run_review_job(job_id, file, llm_opt, progress_path, type_opt=None,
                    agent_opt=None):
    """后台线程：跑 review.py，进度事件写入 progress_path。"""
    before = {p.stem for p in OUTPUTS.glob("R-*.json")}
    cmd = [str(PY), str(ROOT / "review.py"), file,
           "--progress", str(progress_path)]
    if llm_opt is True:
        cmd.append("--llm")
    elif llm_opt is False:
        cmd.append("--no-llm")
    if type_opt:
        cmd.extend(["--type", type_opt])
    if agent_opt:
        cmd.extend(["--agent", agent_opt])
    started = time.time()
    status = "done"
    error = None
    report_id = None
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(ROOT), timeout=300,
        )
        if proc.returncode != 0:
            status = "error"
            error = (proc.stderr or proc.stdout or f"退出码 {proc.returncode}")[-2000:]
        else:
            after = {p.stem for p in OUTPUTS.glob("R-*.json")}
            new = sorted(after - before)
            if not new:
                status = "error"
                error = "审查完成但未生成报告"
            else:
                report_id = max(
                    new, key=lambda rid: (OUTPUTS / f"{rid}.json").stat().st_mtime)
    except subprocess.TimeoutExpired:
        status = "error"
        error = "审查超时（300s）"
    except Exception as e:
        status = "error"
        error = str(e)[:2000]
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update({
                "status": status,
                "error": error,
                "report_id": report_id,
                "finished": time.time(),
                "elapsed": round(time.time() - started, 1),
            })


def main():
    port = 8000
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except Exception:
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"审查 Agent 控制台已启动： http://127.0.0.1:{port}")
    print(f"  根目录：{ROOT}")
    print(f"  解释器：{PY}")
    print("  按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
