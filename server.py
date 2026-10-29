#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 审查 Agent 本地 Web 控制台（零依赖，仅标准库）

提供：
  GET  /                          -> web/index.html
  GET  /static/<path>             -> web/ 下的静态资源
  GET  /api/targets               -> 列出 targets/ 与 targets/uploads/ 下待审文件
  GET  /api/reports               -> 列出 outputs/ 下历史报告（按时间倒序，最新在最前）
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
  POST /api/purpose/upload?name=x -> 上传「审查目的」说明文件（多格式，摄取成文本后暂存）
  GET  /api/purpose/files         -> 列出暂存的目的文件（不含原文）
  DELETE /api/purpose/files?id=x  -> 删除一个暂存的目的文件
  POST /api/review/start          -> 启动异步审查（带 S1~S6 进度）
  POST /api/reports/favorite      -> 收藏 / 取消收藏一份历史报告
  GET  /api/scenarios             -> 列出审查情境（不同用途关掉不同规则）

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
FAVORITES = ROOT / "config" / "favorites.json"         # 被收藏的报告 ID（顺序无意义）
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
import scenario  # noqa: E402
import format_spec  # noqa: E402
import rule_learning  # noqa: E402
import rule_exec  # noqa: E402
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


def _unlink_quiet(path) -> bool:
    """尽力而为的删除：环境级拦截（只读挂载/安全策略）不应拖垮 HTTP 响应。"""
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except BaseException:                        # noqa: BLE001
        return False



PURPOSE_MAX = 300

# ---- 「审查目的」上传文件暂存区 ----
# 放在 state/ 而不是 targets/：目的文件是**说明材料**，不是待审对象，
# 混进 targets/ 会污染待审列表（用户会以为要多审一份文件）。
PURPOSE_DIR = ROOT / "state" / "purpose_uploads"
PURPOSE_ORIGINALS = PURPOSE_DIR / "_originals"
PURPOSE_WORK = PURPOSE_DIR / "_work"
PURPOSE_FILE_MAX = 2000            # 单份目的文件参与解析的字数上限（与 purpose.py 一致）
PURPOSE_TOTAL_MAX = 6000           # 组装后目的总文本上限（手写 300 + 文件拼接）
PURPOSE_FILES_MAX = 5              # 一次审查最多附带几个目的文件
PURPOSE_STAGE_MAX = 20             # 暂存区容量上限（防只传不删攒爆磁盘）
for _d in (PURPOSE_DIR, PURPOSE_ORIGINALS):
    _d.mkdir(parents=True, exist_ok=True)


def _purpose_record_path(pid):
    """暂存记录路径：id 只允许 uuid hex，防路径穿越。"""
    if not re.fullmatch(r"[0-9a-f]{12}", str(pid or "")):
        return None
    return PURPOSE_DIR / f"{pid}.json"


def _load_purpose_record(pid):
    path = _purpose_record_path(pid)
    if not path or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("text") else None


def _list_purpose_records():
    """暂存列表（按上传时间倒序）；**不含原文**，只给预览。"""
    items = []
    for p in sorted(PURPOSE_DIR.glob("*.json"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(r, dict) or not r.get("id"):
            continue
        items.append({
            "id": r.get("id"),
            "name": r.get("name") or "",
            "kind": r.get("kind") or "",
            "chars": int(r.get("chars") or 0),
            "truncated": bool(r.get("truncated")),
            "created": r.get("created") or "",
            "preview": (r.get("text") or "")[:80],
        })
    return items


def _delete_purpose_record(pid):
    """删除暂存记录与归档原件；尽力而为，失败不拖垮接口。"""
    path = _purpose_record_path(pid)
    if not path or not path.is_file():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        record = {}
    original = str((record or {}).get("original") or "")
    if original:
        orig_path = (ROOT / original).resolve()
        try:
            orig_path.relative_to(PURPOSE_ORIGINALS.resolve())
            orig_path.unlink(missing_ok=True)
        except (ValueError, OSError):
            pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _clean_purpose(raw):
    """清洗「审查目的」：去控制字符、限长。空 → None。"""
    text = str(raw or "")
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
    text = text.replace("\x00", "").strip()
    return text[:PURPOSE_MAX] or None


# 目的块定界符：组装时写成「【目的文件：NAME】」。名字里出现这些字符，
# 会让下游（match_matrix.blocks）切块时把名字截断，或让截断说明的
# 「《名》超过 N 字」匹配不上——名字不改，改的是**放进块标记里的那份**。
PURPOSE_MARK_UNSAFE = {
    "【": "[", "】": "]", "《": "<", "》": ">", "（": "(", "）": ")",
}


def _marker_safe_name(name):
    """把文件名的定界符换成等价符号，返回 (安全名, 是否改过)。

    只在写进目的块标记时用；暂存记录里仍保留用户原始文件名。
    changed 只在**确实含定界符**时为 True：空名/纯空白是「未命名」保底，
    不是改名，不能触发「文件名含定界符」的说明（2026-10-26 暴力测试踩到）。
    """
    raw = str(name or "")
    safe = "".join(PURPOSE_MARK_UNSAFE.get(ch, ch) for ch in raw).strip()
    changed = any(ch in PURPOSE_MARK_UNSAFE for ch in raw) and safe != raw
    return (safe or "未命名"), changed


def _assemble_purpose(typed, purpose_files):
    """把手写目的 + 暂存的目的文件组装成一段目的文本。

    返回 (text, error, quality)：
      text    = 最终目的文本（可能为 ""，表示既没手写也没附件）
      error   = 组装失败原因（id 非法 / 文件不存在 / 超数量），此时 text 为 None
      quality = 目的来源的**质量风险**（如图片 OCR），要进报告 limitations 而不能
                只在上传时弹个 toast —— 一闪而过的提示等于没提示，用户事后
                只看到结论、看不到「判定标准本身可能被 OCR 认错」。

    截断策略（两层，都写进文本让人在报告里看见）：
      1. 单份文件在**上传时**已截到 PURPOSE_FILE_MAX（2000）并记 warning；
      2. 组装总长超 PURPOSE_TOTAL_MAX（6000）时整体截断，并附一句说明。
    说明文字直接并入目的文本，而不是悄悄丢——报告 purpose.text 会原样展示。
    """
    if purpose_files in (None, ""):
        return (typed or ""), None, []
    if not isinstance(purpose_files, list):
        return None, "purpose_files 必须是文件 id 数组", []
    ids = [str(x or "") for x in purpose_files]
    if len(ids) > PURPOSE_FILES_MAX:
        return None, f"目的文件最多 {PURPOSE_FILES_MAX} 个", []
    parts, notes, quality = [], [], []
    if typed:
        parts.append(typed)
    for pid in ids:
        record = _load_purpose_record(pid)
        if not record:
            return None, f"目的文件不存在或已过期：{pid or '（空 id）'}", []
        name = str(record.get("name") or pid)
        text = str(record.get("text") or "")
        # 「目的文件」是判定标准，不是待审对象：OCR 认错一个字，改的是**判定口径**。
        # 所以质量风险必须单独记下来进报告 limitations，不能塞进正文（会污染要求侧）。
        if str(record.get("kind") or "") == "image":
            quality.append(
                f"目的文件《{name}》是图片，其中文字由 OCR 识别，"
                "识别错误会直接改变本次审查的判定标准——"
                "关键要求（学历、年限、技能、金额）请以原文为准，"
                "建议改用 PDF / Word / 文本版重新上传。")
        # 说明里有 `《名》` 与 `（说明：…）` 两种定界符：名字必须先用安全名，
        # 否则含「）」的名字会让整段说明提前闭合、截断提示读不出来。
        mark_name, renamed = _marker_safe_name(name)
        if record.get("truncated"):
            notes.append(f"《{mark_name}》超过 {PURPOSE_FILE_MAX} 字，"
                         f"已截断至前 {PURPOSE_FILE_MAX} 字")
        if renamed:
            # 不静默改名：报告里看得出「块标记里的名字」与上传名不同。
            # 措辞里**不能出现「）」**——「（说明：…）」会提前闭合，后面的说明读不出来
            notes.append(f"文件名含【】《》等目的块定界符，"
                         f"标记内改写为《{mark_name}》")
        parts.append(f"【目的文件：{mark_name}】\n{text}")
    combined = "\n".join(p for p in parts if p.strip())
    if len(combined) > PURPOSE_TOTAL_MAX:
        combined = combined[:PURPOSE_TOTAL_MAX]
        notes.append(f"组合目的文本超过 {PURPOSE_TOTAL_MAX} 字，已整体截断")
    if notes:
        combined += "\n（说明：" + "；".join(notes) + "）"
    return combined, None, quality


LABELS_SUFFIX = ".labels.json"


def _target_role(path):
    """待审对象的角色。`*.labels.json` 是**机读标注**（ground truth），不是原文。

    把它当待审对象审，等于让标准答案参加考试：标注正文逐条写着
    「XX 处有错、应为 YY」，任何规则都会在上面开火，报告里于是出现
    一堆与本次目的毫无关系的问题（实测把会议纪要标注里的
    「「这个政策调整哈…」——正式纪要中的口语化表达」判成风格问题）。
    """
    try:
        name = path.name.lower()
    except AttributeError:
        return "text"
    return "labels" if name.endswith(LABELS_SUFFIX) else "text"


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
    if _target_role(target) == "labels":
        # 报错里给出「该审哪个」，否则用户只知道被拒、不知道下一步
        stem = target.name[: -len(LABELS_SUFFIX)]
        sibling = target.parent / (stem + ".txt")
        hint = (f"，对应的原文是 {sibling.relative_to(ROOT).as_posix()}"
                if sibling.is_file() else "")
        return None, (f"{target.name} 是机读标注（标准答案），不是待审原文{hint}。"
                      "审它会把它写的「某处有错」逐条当成问题报出来，"
                      "与审查目的无关。")
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
        if p == "/api/learning":
            # 只读：控制台能看到候选与配额，但**没有任何接口能写规则库**
            return self._send(200, {"ok": True, **rule_learning.summary()})
        if p == "/api/candidates":
            # 待人工审核的规则候选（口径与 CLI 的 rule_review.py 完全一致）
            items = rule_learning.console_candidates()
            return self._send(200, {
                "ok": True, "count": len(items),
                "pending": sum(1 for i in items if i["pending"]),
                "items": items,
            })
        if p == "/api/annotations":
            q = parse_qs(u.query)
            rid = q.get("id", [None])[0]
            items = [a for a in rule_learning.load_annotations()
                     if not rid or a.get("report_id") == rid]
            return self._send(200, {"ok": True, "items": items[-200:]})
        if p == "/api/agents":
            return self._send(200, {"ok": True, "items": agent_spec.list_agents()})
        if p == "/api/scenarios":
            # 配置坏了要让前端看见（400 语义），不能返回空列表：
            # 空列表会被当成"没有情境"，用户在下拉里看不到任何选项也不知道为什么
            try:
                return self._send(200, scenario.public_list())
            except scenario.ScenarioError as e:
                return self._send(200, {"ok": False, "error": str(e), "items": []})
        if p == "/api/format-specs":
            # 格式规格（docx 版式按哪套标准判）。同 scenarios：配置坏了要报错而不是空列表
            try:
                return self._send(200, format_spec.public_list())
            except format_spec.FormatSpecError as e:
                return self._send(200, {"ok": False, "error": str(e), "items": []})
        if p == "/api/purpose/files":
            # 暂存的目的文件（说明材料，不是待审对象）；不含原文，只给预览
            return self._send(200, {"ok": True,
                                    "items": _list_purpose_records()})
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
        if u.path == "/api/purpose/upload":
            return self._send(200, self._purpose_upload(u))
        data = self._json_body()
        if data is None:            # 超大 body：响应已发出（413），直接结束
            return
        if u.path == "/api/review":
            return self._send(200, self._run_review(data))
        if u.path == "/api/review/start":
            return self._send(200, self._start_review(data))
        if u.path == "/api/annotations":
            return self._send(200, self._add_annotation(data))
        if u.path == "/api/candidates/decide":
            return self._send(200, self._decide_candidate(data))
        if u.path == "/api/rules/toggle":
            return self._send(200, self._toggle_rule(data))
        if u.path == "/api/llm/config":
            return self._send(200, self._save_llm_config(data))
        if u.path == "/api/llm/test":
            return self._send(200, self._test_llm(data))
        if u.path == "/api/llm/reset":
            return self._send(200, self._reset_llm_config())
        if u.path == "/api/reports/favorite":
            return self._send(200, self._set_favorite(data))
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
        if u.path == "/api/purpose/files":
            pid = parse_qs(u.query).get("id", [None])[0]
            ok = _delete_purpose_record(pid)
            if not ok:
                return self._send(200, {"ok": False,
                                        "error": f"未找到目的文件：{pid or '（空 id）'}"})
            return self._send(200, {"ok": True, "id": pid,
                                    "items": _list_purpose_records()})
        return self._send(404, {"error": "not found"})

    # ----- 未实现的方法 -----
    # 基类 BaseHTTPRequestHandler 对 PUT/PATCH 直接回 **501 + HTML**：
    #   ① 501 属 5xx，任何"接口不许 5xx"的巡检都会把它当成服务故障；
    #   ② 前端 fetch 拿到的是 HTML，`res.json()` 当场抛错，看不到原因。
    # 统一改成 405 + JSON，并把 Allow 说清楚。
    def _method_not_allowed(self):
        allow = "GET, POST, DELETE"
        body = json.dumps({"ok": False,
                           "error": f"不支持的方法 {self.command}；允许：{allow}"},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(405)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Allow", allow)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        self._method_not_allowed()

    def do_PATCH(self):
        self._method_not_allowed()

    def do_OPTIONS(self):
        self._method_not_allowed()

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
        paths = [p for p in TARGETS.glob("*") if p.is_file()]
        paths += [p for p in UPLOADS.glob("*") if p.is_file()]
        for p in paths:
            st = p.stat()
            role = _target_role(p)
            items.append({
                "name": p.name,
                "rel": p.relative_to(ROOT).as_posix(),
                "size": st.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                "mtime_ts": st.st_mtime,
                "uploaded": p.parent == UPLOADS,
                "role": role,
                "reviewable": role != "labels",
            })
        # 排序规则（顺序即语义）：
        #   ① 可审对象在前 —— `*.labels.json` 是机读标注（测试用的标准答案），
        #      不是待审原文，绝不能因为字母序靠前就占住「默认选中」这个位置；
        #   ② 同一组内按修改时间**降序** —— 刚上传的简历排第一，
        #      而不是把 targets/ 里的历史样本顶到最前。
        items.sort(key=lambda x: (not x["reviewable"], -x["mtime_ts"], x["name"]))
        for it in items:
            it.pop("mtime_ts", None)
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

    # ------------------------------------------------------------------
    # 「审查目的」上传：与 /api/upload 同一套协议，但产物是**目的文本**而不是待审对象
    # ------------------------------------------------------------------
    def _purpose_upload(self, u):
        """接收目的说明文件 → 摄取成文本 → 暂存 state/purpose_uploads/。

        与 /api/upload 的三点区别：
          1. 落点在 state/ 而不是 targets/——目的文件不是待审对象，
             混进 targets/ 会污染待审列表；
          2. 文本按 PURPOSE_FILE_MAX 截断（目的会进每个维度的 prompt）；
          3. 提取不到文本（扫描件/无 OCR 的图片）**明确拒绝**，
             不留一个空目的文件等着被静默忽略。
        """
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
        if len(_list_purpose_records()) >= PURPOSE_STAGE_MAX:
            return {"ok": False,
                    "error": f"目的文件暂存已满（{PURPOSE_STAGE_MAX} 个），"
                             "请先删除不用的目的文件"}
        data = self.rfile.read(length)

        warnings = []
        if _marker_safe_name(name)[1]:
            # 名字里的【】《》（）是目的块定界符：进块时会被改写，先说一声，
            # 别等到报告里名字看着不一样才发现。
            warnings.append(
                "文件名含【】《》（）等定界符，写进目的块时会改写为 [ ] < > ( )，"
                "不影响内容解析")
        kind = "text"
        if not is_binary:
            # —— 文本路径：严格校验（UTF-8 / 拒 NUL / 剥 BOM），与待审对象上传同一口径 ——
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return {"ok": False, "error": "仅支持 UTF-8 编码的文本文件"}
            if chr(0) in text:
                return {"ok": False, "error": "疑似二进制文件，已拒绝"}
            if text.startswith(chr(0xFEFF)):
                text = text[1:]
        else:
            # ---------- 二进制路径：归档原件 + 摄取成文本 ----------
            original = _unique_upload_path(name, base=PURPOSE_ORIGINALS)
            try:
                original.write_bytes(data)
            except Exception as e:
                return {"ok": False, "error": f"保存失败：{e}"}
            try:
                # 每次请求独立工作目录：并发上传时 rmtree 不会删掉别人正在解压的文件
                work = PURPOSE_WORK / uuid.uuid4().hex[:8]
                result = ingest.ingest_file(original, work_dir=work)
            except ingest.IngestError as e:
                _unlink_quiet(original)
                return {"ok": False, "error": str(e)}
            except Exception as e:                    # noqa: BLE001
                _unlink_quiet(original)
                return {"ok": False, "error": f"文档解析失败：{e}"}
            finally:
                shutil.rmtree(work, ignore_errors=True)
            kind = str(result.get("kind") or "binary")
            warnings.extend(str(w) for w in (result["meta"].get("warnings") or []))
            # 目的文件是"标准"，不是"待审对象"：OCR 认错一个字，改的是**判定口径**
            # （实测一张招聘 JD 截图里「算法」被认成「篡法」、「本地部署」成「本地署」），
            # 比审待审对象时认错字更严重——所以单独提示一次，并给出更可靠的做法。
            if kind == "image":
                warnings.append(
                    "目的文件是图片：其中文字靠 OCR 识别，错字会直接改变本次审查的"
                    "判定标准（如「算法」被认成「篡法」）；篇幅较长的要求建议改传 "
                    "PDF / Word / 文本，识别更可靠。")
            # 压缩包可能有多个成员：并成一段文本，成员名写进说明
            chunks = [out["text"] for out in result["outputs"] if out.get("text")]
            if len(result["outputs"]) > 1:
                warnings.append(
                    f"压缩包内 {len(result['outputs'])} 个成员的文本已合并为一段目的")
            text = "\n".join(chunks)

        if not text.strip():
            # 扫描件 / 无 OCR 引擎时的图片：不留空目的文件（会被静默忽略）
            if is_binary:
                _unlink_quiet(original)
            return {"ok": False, "error": "未能从文件中提取到文本"
                                            "（扫描件或图片需 OCR 能力），"
                                            "请换用文本版文件或先配置 OCR",
                    "kind": kind, "warnings": warnings}

        truncated = len(text) > PURPOSE_FILE_MAX
        if truncated:
            text = text[:PURPOSE_FILE_MAX]
            warnings.append(f"目的文件超过 {PURPOSE_FILE_MAX} 字，"
                            f"已截断至前 {PURPOSE_FILE_MAX} 字参与解析")

        pid = uuid.uuid4().hex[:12]
        record = {
            "id": pid,
            "name": name,
            "kind": kind,
            "chars": len(text),
            "truncated": truncated,
            "text": text,
            "warnings": warnings,
            "original": (original.relative_to(ROOT).as_posix()
                         if is_binary else ""),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            (PURPOSE_DIR / f"{pid}.json").write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            if is_binary:
                _unlink_quiet(original)
            return {"ok": False, "error": f"暂存失败：{e}"}

        return {
            "ok": True,
            "id": pid,
            "name": name,
            "kind": kind,
            "chars": len(text),
            "truncated": truncated,
            "preview": text[:80],
            "warnings": warnings,
            "items": _list_purpose_records(),
        }

    def _favorites(self):
        """读取被收藏的报告 ID。

        文件不存在 / 损坏 / 内容不合规，一律当"没有收藏"处理——
        一个坏掉的收藏文件不能把整个历史列表拖垮（列表是主路径，收藏是附加）。
        """
        try:
            data = json.loads(FAVORITES.read_text(encoding="utf-8"))
        except Exception:
            return []
        ids = data.get("favorites") if isinstance(data, dict) else data
        if not isinstance(ids, list):
            return []
        out = []
        for x in ids:
            rid = str(x or "")
            if re.fullmatch(r"R-\d{8}-\d{3}", rid) and rid not in out:
                out.append(rid)
        return out

    def _save_favorites(self, ids):
        FAVORITES.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "favorites": ids,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        tmp = FAVORITES.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(FAVORITES))

    def _set_favorite(self, data):
        """收藏 / 取消收藏一份报告。body: {"id": "R-xxx", "favorite": true|false}

        `favorite` 必须是布尔值：不接受 "1" / "toggle" 之类的模糊写法，
        免得前端状态与服务端不一致时静默写反。
        """
        rid = str((data or {}).get("id") or "")
        if not re.fullmatch(r"R-\d{8}-\d{3}", rid):
            return {"ok": False, "error": "报告 ID 格式不合法"}
        if not (OUTPUTS / f"{rid}.json").exists():
            return {"ok": False, "error": f"未找到报告 {rid}"}
        want = (data or {}).get("favorite")
        if not isinstance(want, bool):
            return {"ok": False, "error": "favorite 必须是 true / false"}

        ids = self._favorites()
        if want and rid not in ids:
            ids.insert(0, rid)          # 最近收藏的排前面
        elif not want and rid in ids:
            ids.remove(rid)
        self._save_favorites(ids)
        return {"ok": True, "id": rid, "favorite": want, "favorites": ids}

    def _reports(self):
        """历史报告列表：**只按时间倒序**，最新的排最上面。

        排序键用报告内的 `timestamp`（生成时刻），不用文件 mtime：
        拷贝 / 解压 / 批量重写都会改 mtime，会让顺序莫名漂移；而 timestamp
        是报告自己写死的，永远等于"这份报告是什么时候跑出来的"。
        同秒连跑两份时 timestamp 相同，用 review_id 兜底（编号更大的在后生成的），
        保证顺序稳定、不随文件系统返回顺序抖动。
        """
        fav = set(self._favorites())
        items = []
        for p in OUTPUTS.glob("R-*.json"):
            try:
                r = _read_json(p)
            except Exception:
                continue          # 坏掉的报告不该让整个列表 500
            inc = r.get("integrity", {})
            mtime = p.stat().st_mtime
            ts = str(r.get("timestamp") or "")
            if not ts:
                # 老报告可能没写 timestamp：退回 mtime，但格式化成同一个口径再排
                ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))
            rid = str(r.get("review_id") or p.stem)
            items.append({
                "id": rid,
                "verdict": r.get("verdict"),
                "target_type": r.get("target_type"),
                "target_path": r.get("target_path"),
                "target_name": Path(r.get("target_path") or "").name,
                "review_mode": r.get("review_mode"),
                "timestamp": ts,
                "issues": len(r.get("issues", [])),
                "stats": r.get("stats", {}),
                "report_hash": (inc.get("report_hash") or "")[:16],
                "favorite": rid in fav,
                "mtime": mtime,
            })
        items.sort(key=lambda x: (x["timestamp"], x["id"]), reverse=True)
        return {"items": items, "favorites": sorted(fav)}

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
        sidecar = OUTPUTS / f"{rid}.md"
        # 只认 .json 会留下"清不掉的孤儿"：删除是「先移 json 再移 md」两次文件操作，
        # 中途被打断（进程被杀 / 磁盘满）就只剩一个 .md，而 .json 已进回收站。
        # 此时再删会报"未找到报告"，那个 .md 就永远躺在 outputs/ 里没人能处理。
        # 所以判"找不到"要看两个文件都不在。
        if not hit.exists() and not sidecar.exists():
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
        # 收藏表里摘掉这个 id：报告都进回收站了，收藏里留着会变成永远点不开的幽灵项
        favs = self._favorites()
        if rid in favs:
            favs.remove(rid)
            self._save_favorites(favs)
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
                # 执行方式：deterministic / declarative / prompt_only
                "capability": rule_exec.capability(r),
            })
        dims = {}
        for item in items:
            dims[item["dimension"]] = dims.get(item["dimension"], 0) + 1
        exec_counts = rule_exec.counts(all_rules)
        return {
            "ok": True,
            "items": items,
            "total": len(items),
            "enabled": sum(1 for i in items if i["enabled"]),
            "dimensions": dims,
            # 执行方式分布：确定性 / 声明式 / 仅提示
            "execution": exec_counts,
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

    ANNOTATION_TOKEN_ENV = "REVIEW_AGENT_ANNOTATION_TOKEN"

    def _add_annotation(self, data):
        """记录人工标注（可选鉴权 + 作者白名单）。"""
        need = os.environ.get(self.ANNOTATION_TOKEN_ENV, "").strip()
        if need and self.headers.get("X-Review-Token", "") != need:
            # 开放部署时才启用；单机默认不设这个变量，控制台照常可用。
            # 注意：即使鉴权通过，能否作为学习种子还取决于 config 里的作者白名单。
            return {"ok": False,
                    "error": f"标注需要 X-Review-Token 头（服务端已启用标注鉴权）"}
        return self._add_annotation_inner(data)

    def _add_annotation_inner(self, data):
        """记录人工标注。

        这是整个 HTTP 面上唯一与「学习」有关的写操作，而且它**只能写标注**：
        规则库的写入只存在于 tools/rule_promote.py 且需要审批。
        被审文档因此没有任何路径能影响自己的审查规则。
        """
        d = data or {}
        try:
            item = rule_learning.add_annotation(
                kind=str(d.get("kind") or ""),
                by=str(d.get("by") or "console"),
                target_path=d.get("target_path"),
                quote=d.get("quote"),
                dimension=d.get("dimension"),
                report_id=d.get("report_id"),
                issue_id=d.get("issue_id"),
                note=str(d.get("note") or ""),
            )
        except rule_learning.LearningError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "annotation": item}

    def _decide_candidate(self, data):
        """人工审核决定：**只写候选文件的 human_review，不动规则库**。

        这是 HTTP 面上第二个与「学习」有关的写操作（第一个是写标注）。
        写规则库依然只存在于 tools/rule_promote.py —— 控制台没有那条路径。
        """
        need = os.environ.get(self.ANNOTATION_TOKEN_ENV, "").strip()
        if need and self.headers.get("X-Review-Token", "") != need:
            return {"ok": False,
                    "error": "审核决定需要 X-Review-Token 头（服务端已启用鉴权）"}
        d = data or {}
        try:
            r = rule_learning.decide_candidate(
                d.get("candidate_id"),
                str(d.get("decision") or ""),
                note=str(d.get("note") or ""),
                by=str(d.get("by") or "console"),
            )
        except rule_learning.LearningError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **r}

    def _start_review(self, data):
        file = str((data or {}).get("file") or "").strip()
        target, error = _resolve_target(file)
        if error:
            return {"ok": False, "error": error}
        llm_opt = (data or {}).get("llm")
        if llm_opt not in (True, False, None):
            llm_opt = None
        research_opt = (data or {}).get("research")
        if research_opt not in (True, False, None):
            research_opt = None
        type_opt = (data or {}).get("type")
        if type_opt not in TARGET_TYPES:
            type_opt = None
        agent_opt, agent_err = _resolve_agent((data or {}).get("agent"))
        if agent_err:
            return {"ok": False, "error": agent_err}
        scenario_opt, scenario_err = _resolve_scenario((data or {}).get("scenario"))
        if scenario_err:
            return {"ok": False, "error": scenario_err}
        spec_opt, spec_err = _resolve_format_spec((data or {}).get("format_spec"))
        if spec_err:
            return {"ok": False, "error": spec_err}
        # 目的组装（手写 + 上传的目的文件）在启动前完成：id 非法/超量要直接报错，
        # 不能等任务跑起来才失败。组装后的文本经 --purpose 传给子进程，
        # --purpose-max 告知解析上限（文件类目的可超过 300 字）。
        typed_purpose = _clean_purpose((data or {}).get("purpose"))
        purpose_text, purpose_err, purpose_quality = _assemble_purpose(
            typed_purpose, (data or {}).get("purpose_files"))
        if purpose_err:
            return {"ok": False, "error": purpose_err}
        purpose_max = len(purpose_text) if purpose_text else None
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
                "research": research_opt,
                "type": type_opt,
                "agent": agent_opt,
                "scenario": scenario_opt,
                "format_spec": spec_opt,
            }
        threading.Thread(
            target=_run_review_job,
            args=(job_id, file, llm_opt, progress, type_opt, agent_opt,
                  purpose_text or None, purpose_max, purpose_quality, scenario_opt,
                  spec_opt, research_opt),
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
        # 库外事实核验默认关闭；前端勾选后显式传 research:true
        if (data or {}).get("research") is True:
            cmd.append("--research")
        else:
            cmd.append("--no-research")
        type_opt = (data or {}).get("type")
        if type_opt in TARGET_TYPES:
            cmd.extend(["--type", type_opt])
        agent_opt, agent_err = _resolve_agent((data or {}).get("agent"))
        if agent_err:
            return {"ok": False, "error": agent_err}
        if agent_opt:
            cmd.extend(["--agent", agent_opt])
        scenario_opt, scenario_err = _resolve_scenario((data or {}).get("scenario"))
        if scenario_err:
            return {"ok": False, "error": scenario_err}
        if scenario_opt:
            cmd.extend(["--scenario", scenario_opt])
        spec_opt, spec_err = _resolve_format_spec((data or {}).get("format_spec"))
        if spec_err:
            return {"ok": False, "error": spec_err}
        if spec_opt:
            cmd.extend(["--format-spec", spec_opt])
        typed_purpose = _clean_purpose((data or {}).get("purpose"))
        purpose_text, purpose_err, purpose_quality = _assemble_purpose(
            typed_purpose, (data or {}).get("purpose_files"))
        if purpose_err:
            return {"ok": False, "error": purpose_err}
        if purpose_text:
            cmd.extend(["--purpose", purpose_text])
            cmd.extend(["--purpose-max", str(len(purpose_text))])
        if purpose_quality:
            cmd.extend(["--purpose-notes",
                        json.dumps(purpose_quality, ensure_ascii=False)])
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


def _resolve_scenario(raw):
    """校验前端传来的情境 id；空值表示不指定（= 全规则，等价于旧行为）。"""
    if raw in (None, "", "default"):
        return None, None
    try:
        known = [s["id"] for s in scenario.load()["scenarios"]]
        if str(raw) not in known:
            return None, f"未知审查情境：{raw}；可选：{'、'.join(known)}"
    except scenario.ScenarioError as e:
        return None, f"情境配置错误：{e}"
    return str(raw), None


def _resolve_format_spec(raw):
    """校验前端传来的格式规格 id。

    空值返回 None：由 review.py 用配置里的 default（当前 none = 不判版式）。
    不传空字符串给子进程，是因为 --format-spec "" 会被当成"选了空规格"。
    """
    if raw in (None, "", "default"):
        return None, None
    try:
        known = [s["id"] for s in format_spec.load()["specs"]]
        if str(raw) not in known:
            return None, f"未知格式规格：{raw}；可选：{'、'.join(known)}"
    except format_spec.FormatSpecError as e:
        return None, f"格式规格配置错误：{e}"
    return str(raw), None


def _run_review_job(job_id, file, llm_opt, progress_path, type_opt=None,
                    agent_opt=None, purpose=None, purpose_max=None,
                    purpose_quality=None, scenario_opt=None, format_spec_opt=None,
                    research_opt=None):
    """后台线程：跑 review.py，进度事件写入 progress_path。"""
    before = {p.stem for p in OUTPUTS.glob("R-*.json")}
    cmd = [str(PY), str(ROOT / "review.py"), file,
           "--progress", str(progress_path)]
    if llm_opt is True:
        cmd.append("--llm")
    elif llm_opt is False:
        cmd.append("--no-llm")
    if research_opt is True:
        cmd.append("--research")
    else:
        cmd.append("--no-research")
    if type_opt:
        cmd.extend(["--type", type_opt])
    if agent_opt:
        cmd.extend(["--agent", agent_opt])
    if scenario_opt:
        # 情境只决定"哪些规则参与"，关了什么会写进报告 limitations
        cmd.extend(["--scenario", scenario_opt])
    if format_spec_opt:
        # 格式规格只提供"版式判定的数值"，没选时由 review.py 用 default（none）
        cmd.extend(["--format-spec", format_spec_opt])
    if purpose:
        cmd.extend(["--purpose", purpose])
        # 文件类目的可超过 300 字：把实际上限告诉解析层，避免二次截断
        if purpose_max:
            cmd.extend(["--purpose-max", str(purpose_max)])
    if purpose_quality:
        # 目的来源的质量风险（图片 OCR / 截断）要进报告 limitations：
        # 只上传时 toast 一下，用户事后只看到结论、看不到判定标准被认错了
        cmd.extend(["--purpose-notes", json.dumps(purpose_quality, ensure_ascii=False)])
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
