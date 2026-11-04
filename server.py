#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 审查 Agent 本地 Web 控制台（零依赖，仅标准库）

提供：
  GET  /                          -> web/index.html
  GET  /static/<path>             -> web/ 下的静态资源
  GET  /api/targets               -> 列出 targets/ 与 targets/uploads/ 下待审文件
  GET  /api/reports               -> 列出 outputs/ 下历史报告（按时间倒序，最新在最前）
  GET  /api/reports/trash         -> 列出回收站中的报告（可恢复）
  GET  /api/report?id=R-xxx       -> 读取单份报告 JSON
  DELETE /api/report?id=R-xxx     -> 移入回收站（UI 一键；API 仍须 body 传 confirm=id）
  DELETE /api/reports/batch       -> 批量移入回收站（须 confirm=DELETE_SELECTED）
  POST /api/reports/trash/restore -> 从回收站恢复报告（追加 ledger 恢复事件）
  DELETE /api/reports/trash?id=   -> 彻底删除一份已回收报告（须 confirm=id）
  DELETE /api/reports/trash       -> 清空回收站（须 confirm=EMPTY）
  GET  /api/integrity             -> 校验 ledger 链 + 每份报告自洽
  GET  /api/rules                 -> 规则库列表（含启用状态）
  GET  /api/logs?id=R-xxx         -> 读取单次审查的四类日志
  GET  /api/review/status?job=x   -> 查询异步审查任务的进度与报告
  GET  /api/llm/status            -> 大模型配置状态（key 仅 masked）
  GET  /api/llm/providers         -> 常见服务商 / 本地模型预设
  POST /api/rules/toggle          -> 启用/停用某条规则
  POST /api/upload?name=x.txt     -> 上传 UTF-8 文本到 targets/uploads/
  DELETE /api/target?rel=...      -> 删除一个待审文件（移入 targets/uploads/.trash/，可撤销）
  POST /api/target/restore        -> 撤销删除：把回收区文件搬回原目录
  POST /api/purpose/upload?name=x -> 上传「审查目的」说明文件（多格式，摄取成文本后暂存）
  GET  /api/purpose/files         -> 列出暂存的目的文件（不含原文）
  DELETE /api/purpose/files?id=x  -> 删除一个暂存的目的文件
  POST /api/review/start          -> 启动异步审查（带 S1~S6 进度）
  POST /api/reports/favorite      -> 收藏 / 取消收藏一份历史报告
  POST /api/reports/favorites     -> 批量收藏 / 取消收藏历史报告
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
import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlsplit

# 台账的锁 / seq 推导 / 墓碑格式只有一套实现（`infrastructure/ledger.py`）。
# server 与 review.py 共用它：两边各写一份的下场是「一个按行数推号、一个按记录数推号」，
# 删过报告之后新记录的 seq 就会撞号。
from review_agent.infrastructure import ledger as ledger_store
from interfaces.web.reports import ReportService
# 任务记录是「这条审查到底跑完没有」的唯一真相：JOBS 只是给前端看的内存视图。
from review_agent.application import (
    FileTaskStore,
    TaskOutcome,
    TaskRecord,
    TaskService,
    active_tasks,
    find_active,
)
from review_agent.domain import InvalidTransition, NotFoundError, RunStatus, Stage

# 强制 UTF-8 输出：Windows / GitHub Actions runner 的控制台可能用 cp1252 等
# 非 UTF-8 编码，打印中文（任务恢复日志等）会抛 UnicodeEncodeError。
for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if _reconf is not None:
        try:
            _reconf(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
TARGETS = ROOT / "targets"
UPLOADS = TARGETS / "uploads"
OUTPUTS = ROOT / "outputs"
LEDGER = ROOT / "ledger"
LOG_DIR = ROOT / "logs"
STATE_DIR = ROOT / "state"                               # 运行状态（检查点 / 任务记录 / 缓存）
FAVORITES = ROOT / "config" / "favorites.json"         # 被收藏的报告 ID（顺序无意义）
TRASH = OUTPUTS / ".trash"                              # 已删报告回收区（可恢复）
# 已删待审文件的回收区。放在 uploads/ 下面而不是 targets/ 顶层：
# _targets() 只扫顶层文件，子目录不会混进待审列表，不用额外加排除规则。
TARGET_TRASH = UPLOADS / ".trash"                       # 已删待审文件回收区（可撤销）
LLM_HISTORY = ROOT / "config" / "llm_history.json"     # LLM 配置记忆（最近用过的）
LLM_HISTORY_MAX = 12
# 外发清单：哪些材料发给了第三方（只记元信息，不记内容）
EGRESS_LOG = ROOT / "state" / "egress.log"
LLM_CACHE_DIR = ROOT / "cache" / "llm"
# 进程启动时锁定源码指纹。启动脚本用它判断端口上是当前代码，还是仍在跑旧版本。
SERVER_SOURCE_FINGERPRINT = hashlib.sha256(
    Path(__file__).read_bytes()).hexdigest()

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
# 任务记录（`state/tasks/<review_id>.json`）：跨进程、跨重启的事实来源。
# `JOBS` 从这一刻起只是它的内存视图，不再自己长出一套状态。
TASKS = FileTaskStore(STATE_DIR / "tasks")
# 活着的子进程：取消要真的停掉它（terminate），而不是只把状态改成"已取消"。
PROCS: dict[str, subprocess.Popen] = {}
PROCS_LOCK = threading.Lock()
TASK_STALE_AFTER = timedelta(hours=1)

# 审查子进程超时：长文 + LLM 远超 5 分钟，写死 300s 等于必然超时。
# 默认 1800s，允许用环境变量 / 命令行调整，但必须落在 [30, 86400]。
DEFAULT_REVIEW_TIMEOUT = 1800
REVIEW_TIMEOUT_MIN = 30
REVIEW_TIMEOUT_MAX = 86400
JOB_RETENTION_SECONDS = 7 * 24 * 60 * 60      # 启动时清理 7 天前的 .jobs 进度文件


def _clamp_review_timeout(raw, fallback):
    """把原始值转成整数并夹到合法区间；无法解析时返回 fallback。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return max(REVIEW_TIMEOUT_MIN, min(REVIEW_TIMEOUT_MAX, value))


def _review_timeout(environ=None):
    """读取 REVIEW_AGENT_REVIEW_TIMEOUT，默认 1800s，非法值回落默认。"""
    env = os.environ if environ is None else environ
    raw = env.get("REVIEW_AGENT_REVIEW_TIMEOUT")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_REVIEW_TIMEOUT
    return _clamp_review_timeout(raw, DEFAULT_REVIEW_TIMEOUT)


def _parse_review_timeout(argv=None):
    """解析 --review-timeout：优先级 CLI > 环境变量 > 默认值。

    CLI 值非法时退回环境变量结果（而不是直接抹掉环境配置），
    两处都对 [30, 86400] 做 clamp。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    env_value = _review_timeout()
    for i, arg in enumerate(args):
        if arg == "--review-timeout":
            if i + 1 >= len(args):
                return env_value
            return _clamp_review_timeout(args[i + 1], env_value)
        if arg.startswith("--review-timeout="):
            return _clamp_review_timeout(arg.split("=", 1)[1], env_value)
    return env_value


# 线程内只读；main() 在起 server 前按 CLI > env > 默认覆写一次。
REVIEW_TIMEOUT = _review_timeout()


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
import fact_gap  # noqa: E402
import agent_spec  # noqa: E402
import scenario  # noqa: E402
import format_spec  # noqa: E402
import rule_learning  # noqa: E402
import rule_exec  # noqa: E402
import llm_client  # noqa: E402
import rules_store  # noqa: E402
import version  # noqa: E402

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


def _report_service(favorites_getter=None, favorites_setter=None):
    """按当前模块全局构造报告服务。

    测试会 monkeypatch `server.OUTPUTS/TRASH/...`，所以这里必须每次读全局，
    不能在 import 时固化一份实例。
    """
    return ReportService(
        root=ROOT,
        outputs=OUTPUTS,
        log_dir=LOG_DIR,
        state_dir=STATE_DIR,
        trash=TRASH,
        ledger_dir=LEDGER,
        favorites_path=FAVORITES,
        llm_cache_dir=LLM_CACHE_DIR,
        favorites_getter=favorites_getter,
        favorites_setter=favorites_setter,
        purge_gap=fact_gap.purge_review,
    )


def _legacy_task_view(task):
    """把 V1 task.json 的节点明细投影成控制台认识的 S1–S6 steps。"""
    if not isinstance(task, dict) or not task.get("steps_legacy"):
        return task
    out = dict(task)
    out["steps_v1"] = task.get("steps")
    out["steps"] = task.get("steps_legacy")
    if out.get("final_status") == "completed":
        out["final_status"] = "success"
    return out


# 审查子进程的错误分类行（review.py::_emit_error 写入 stderr）。
# 用一行机器可读的 JSON 替代「拿正则去猜 stderr 里那句话是什么意思」：
# 用户路径写错（input）与服务端坏了（system）要给完全不同的提示。
ERROR_MARKER = "REVIEW_ERROR "


def _parse_review_error(text):
    """从子进程输出里取结构化错误信息；没有就返回 None（老版本 review.py）。"""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if not line.startswith(ERROR_MARKER):
            continue
        try:
            info = json.loads(line[len(ERROR_MARKER):])
        except ValueError:
            return None
        return info if isinstance(info, dict) else None
    return None


def _strip_error_marker(text):
    """展示给用户的错误里不要出现那行机器可读 JSON。"""
    kept = [line for line in str(text or "").splitlines()
            if not line.strip().startswith(ERROR_MARKER)]
    return "\n".join(kept).strip()


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


def _target_rel_path(rel, *, must_exist=True):
    """把前端给的 rel（如 targets/uploads/x.txt）解析成磁盘路径，并挡掉越界。

    返回 (path, error)。rel 来自请求参数，属于不可信输入：
    `..`、绝对路径、指向 targets/ 之外的一律拒绝——否则一个 DELETE 就能删仓库里任何文件。
    """
    raw = str(rel or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return None, "路径不合法"
    if any(part in ("..", "") for part in raw.split("/")):
        return None, "路径不合法"
    try:
        path = (ROOT / raw).resolve()
    except (OSError, ValueError):
        return None, "路径不合法"
    try:
        path.relative_to(TARGETS.resolve())
    except ValueError:
        return None, "路径不在 targets/ 内"
    if must_exist and not path.is_file():
        return None, "文件不存在（可能已被删除）"
    return path, ""



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


def _origin_parts(origin):
    """解析 Origin，返回 (scheme, hostname, port)；非 http(s) 或非法 URL 返回 None。

    端口缺失时按 scheme 补默认值，否则 `Origin: http://127.0.0.1`
    （监听 80）会被误判成端口不同。
    """
    try:
        parts = urlsplit(str(origin or "").strip())
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None
        host = (parts.hostname or "").lower()
        if not host:
            return None
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 80 if scheme == "http" else 443
    return scheme, host, port


def _url_netloc(url):
    """返回 URL 的 (hostname, port)；端口缺失按 scheme 归一化，非法 URL 返回 None。"""
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
        if not host:
            return None
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = 80 if (parts.scheme or "").lower() == "http" else \
                (443 if (parts.scheme or "").lower() == "https" else None)
    return host, port


def _url_has_userinfo(url):
    """URL 的 authority 里是否带 user:pass@（凭据不允许进配置文件/响应）。"""
    try:
        return "@" in urlsplit(str(url or "")).netloc
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = f"ReviewAgentConsole/{version.__version__}"

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
        # 用 resolve()+relative_to 判边界：字符串前缀会让 web_evil/ 通过，
        # 也会把 /static/ 这种目录送进 read_bytes() 抛 500。
        try:
            resolved = path.resolve()
            resolved.relative_to(WEB.resolve())
        except (ValueError, OSError):
            return self._send(404, {"error": "not found"})
        if not resolved.is_file():
            return self._send(404, {"error": "not found"})
        data = resolved.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _ctype(resolved))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _guard_api(self):
        """拒绝非本机 Host、跨站 Origin 与跨站写请求（CSRF / DNS rebinding）。

        Origin 必须同时匹配 Host 与真实监听端口——只比 hostname 会让
        http://127.0.0.1:<别的端口> 被当成本源。浏览器跨源写请求还必须带
        X-Review-Console 自定义头（跨源无法免预检自带）；无 Origin 的
        curl / CLI / 测试客户端保持兼容。
        """
        host_header = self.headers.get("Host")
        host = _hostname_from_header(host_header)
        if not host:
            # HTTP/1.1 必须带 Host；HTTP/1.0 客户端允许省略，保持兼容。
            if str(self.request_version or "") >= "HTTP/1.1":
                self._send(403, {"ok": False, "error": "缺少 Host 头"})
                return False
        elif host not in LOCAL_HOSTS:
            self._send(403, {"ok": False, "error": "Host 不被允许"})
            return False
        origin = self.headers.get("Origin")
        if origin:
            parts = _origin_parts(origin)
            if parts is None:
                self._send(403, {"ok": False, "error": "跨站请求已被拒绝"})
                return False
            _scheme, origin_host, origin_port = parts
            listen_port = self.server.server_address[1]
            if (host and origin_host != host) or origin_port != listen_port:
                self._send(403, {"ok": False, "error": "跨站请求已被拒绝"})
                return False
            if self.command in ("POST", "DELETE", "PUT", "PATCH") and \
                    (self.headers.get("X-Review-Console") or "").strip() != "1":
                self._send(403, {"ok": False,
                                 "error": "缺少 X-Review-Console 请求头"})
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
        if p == "/api/health":
            return self._send(200, {
                "ok": True,
                "service": "review-agent",
                "pid": os.getpid(),
                "root": str(ROOT),
                "fingerprint": SERVER_SOURCE_FINGERPRINT,
            })
        if p == "/api/targets":
            return self._send(200, self._targets())
        if p == "/api/reports":
            return self._send(200, self._reports())
        if p == "/api/reports/trash":
            return self._send(200, self._trash_reports())
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
        if u.path == "/api/review/cancel":
            return self._send(200, self._cancel_review(data))
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
        if u.path == "/api/reports/favorites":
            return self._send(200, self._set_favorites(data))
        if u.path == "/api/reports/trash/restore":
            return self._send(200, self._restore_report(data))
        if u.path == "/api/target/restore":
            return self._send(200, self._restore_target(data))
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
            # 删除是「移文件 + 追加墓碑」两步：任何一步抛异常都要回结构化错误，
            # 不能让异常掀掉连接（旧实现的台账整文件重写在 Windows 上会 PermissionError）。
            try:
                return self._send(200, self._delete_report(rid, data))
            except Exception as exc:                    # noqa: BLE001 - HTTP 边界
                return self._send(500, {"ok": False,
                                        "error": f"删除失败：{type(exc).__name__}: {exc}"})
        if u.path == "/api/target":
            rel = parse_qs(u.query).get("rel", [None])[0]
            try:
                return self._send(200, self._delete_target(rel))
            except Exception as exc:                    # noqa: BLE001 - HTTP 边界
                return self._send(500, {"ok": False,
                                        "error": f"删除失败：{type(exc).__name__}: {exc}"})
        if u.path == "/api/reports/batch":
            data = self._json_body()
            try:
                return self._send(200, self._delete_reports(data))
            except Exception as exc:                    # noqa: BLE001 - HTTP 边界
                return self._send(500, {"ok": False,
                                        "error": f"批量删除失败：{type(exc).__name__}: {exc}"})
        if u.path == "/api/reports/trash":
            rid = parse_qs(u.query).get("id", [None])[0]
            data = self._json_body()
            try:
                result = (self._purge_trash_report(rid, data) if rid
                          else self._empty_trash(data))
                return self._send(200, result)
            except Exception as exc:                    # noqa: BLE001 - HTTP 边界
                return self._send(500, {"ok": False,
                                        "error": f"彻底删除失败：{type(exc).__name__}: {exc}"})
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
        if not self._guard_api():
            return
        self._method_not_allowed()

    def do_PATCH(self):
        if not self._guard_api():
            return
        self._method_not_allowed()

    def do_OPTIONS(self):
        self._method_not_allowed()

    MAX_JSON_BODY = 1 * 1024 * 1024        # JSON 接口 1MB 上限（与文本 2MB / 二进制 20MB 对称）

    def _json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > self.MAX_JSON_BODY:
            # 读入前就拒，避免把超大 body 全量读进内存；
            # 但 body 还在 socket 里，必须关连接，否则 keep-alive 下一条请求会错位。
            self.close_connection = True
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

    # ----- 待审文件：删除 / 撤销（软删除） -----
    def _delete_target(self, rel):
        """删除待审列表中的顶层文件或上传文件，统一切到可撤销回收区。"""
        path, err = _target_rel_path(rel)
        if path is None:
            return {"ok": False, "error": err}
        if path.parent not in (TARGETS.resolve(), UPLOADS.resolve()):
            return {"ok": False, "error": "只能删除待审列表中的文件"}
        TARGET_TRASH.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = TARGET_TRASH / f"{stamp}__{path.name}"
        n = 1
        while dest.exists():                     # 同一秒删多个同名文件不能互相覆盖
            n += 1
            dest = TARGET_TRASH / f"{stamp}-{n}__{path.name}"
        original_rel = path.relative_to(ROOT).as_posix()
        manifest = {
            "schema": 1,
            "original_rel": original_rel,
            "trashed": dest.relative_to(ROOT).as_posix(),
            "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        manifest_path = TARGET_TRASH / ".manifests" / f"{dest.name}.json"
        try:
            shutil.move(str(path), str(dest))
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = manifest_path.with_name(
                f"{manifest_path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, manifest_path)
        except OSError as exc:
            # 文件已搬走但清单没写成时必须搬回，避免留下无法从界面撤销的孤儿文件。
            if dest.exists() and not path.exists():
                try:
                    shutil.move(str(dest), str(path))
                except OSError:
                    pass
            return {"ok": False, "error": f"删除失败：{exc}"}
        return {"ok": True, "rel": rel, "name": path.name,
                "trashed": manifest["trashed"],
                "items": self._targets()["items"]}

    def _restore_target(self, data):
        """撤销删除：按清单把回收区文件搬回原目录；拒绝覆盖同名文件。"""
        trashed = str((data or {}).get("trashed") or "")
        path, err = _target_rel_path(trashed)
        if path is None:
            return {"ok": False, "error": err}
        if path.parent != TARGET_TRASH.resolve():
            return {"ok": False, "error": "该路径不在待审文件回收区"}
        name = path.name.split("__", 1)[-1]      # <时间戳>__<原名>
        manifest_path = TARGET_TRASH / ".manifests" / f"{path.name}.json"
        original_rel = f"targets/uploads/{name}"
        if manifest_path.is_file():
            try:
                manifest = _read_json(manifest_path)
                original_rel = str(manifest.get("original_rel") or original_rel)
            except Exception:
                return {"ok": False, "error": "撤销信息已损坏，无法恢复"}
        dest, err = _target_rel_path(original_rel, must_exist=False)
        if dest is None:
            return {"ok": False, "error": err}
        if dest.parent not in (TARGETS.resolve(), UPLOADS.resolve()):
            return {"ok": False, "error": "恢复目标不是待审文件位置"}
        if dest.exists():
            return {"ok": False, "error": f"同名文件已存在：{name}"}
        try:
            shutil.move(str(path), str(dest))
        except OSError as exc:
            return {"ok": False, "error": f"恢复失败：{exc}"}
        try:
            manifest_path.unlink()
        except OSError:
            pass
        return {"ok": True, "rel": original_rel, "name": name,
                "items": self._targets()["items"]}

    def _upload(self, u):
        """接收原始字节上传：只收 UTF-8 文本/代码，落到 targets/uploads/。"""
        q = parse_qs(u.query)
        name = _safe_upload_name((q.get("name") or [""])[0])
        if not name:
            self.close_connection = True   # body 未读，避免 keep-alive 错位
            return {"ok": False, "error": "文件名无效"}
        ext = os.path.splitext(name)[1].lower()
        if ext not in UPLOAD_ALLOWED:
            self.close_connection = True   # body 未读，避免 keep-alive 错位
            return {"ok": False, "error": f"不支持的文件类型：{ext or '无扩展名'}"}
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self.close_connection = True   # body 未读，避免 keep-alive 错位
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
            self.close_connection = True   # body 未读，避免 keep-alive 错位
            return {"ok": False, "error": "文件名无效"}
        ext = os.path.splitext(name)[1].lower()
        if ext not in UPLOAD_ALLOWED:
            self.close_connection = True   # body 未读，避免 keep-alive 错位
            return {"ok": False, "error": f"不支持的文件类型：{ext or '无扩展名'}"}
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            self.close_connection = True   # body 未读，避免 keep-alive 错位
            return {"ok": False, "error": "文件为空"}
        is_binary = ext in UPLOAD_BINARY_EXTS
        limit = UPLOAD_MAX_BINARY_BYTES if is_binary else UPLOAD_MAX_BYTES
        if length > limit:
            self.close_connection = True
            return {"ok": False, "error": f"文件超过 {limit // (1024 * 1024)} MB 限制"}
        if len(_list_purpose_records()) >= PURPOSE_STAGE_MAX:
            self.close_connection = True   # body 未读，避免 keep-alive 错位
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
        return _report_service().favorites()

    def _save_favorites(self, ids):
        return _report_service().save_favorites(ids)

    def _set_favorite(self, data):
        return _report_service(
            self._favorites, self._save_favorites).set_favorite(data)

    def _set_favorites(self, data):
        return _report_service(
            self._favorites, self._save_favorites).set_favorites(data)

    def _reports(self):
        return _report_service(
            self._favorites, self._save_favorites).reports()

    def _delete_report(self, rid, data):
        """删除报告；未带 confirm=<报告ID> 时由服务层返回 need_confirm。"""
        return _report_service(
            self._favorites, self._save_favorites).delete_report(rid, data)

    def _delete_reports(self, data):
        return _report_service(
            self._favorites, self._save_favorites).delete_reports(data)

    def _trash_reports(self):
        return _report_service().trash_reports()

    def _restore_report(self, data):
        return _report_service().restore_report(data)

    def _purge_trash_report(self, rid, data):
        return _report_service().purge_trash_report(rid, data)

    def _empty_trash(self, data):
        return _report_service().empty_trash(data)

    def _report(self, rid):
        return _report_service().report(rid)

    def _purge_derived(self, rid):
        """兼容入口：实际实现见 interfaces/web/reports.py。"""
        return _report_service()._purge_derived(rid)

    def _mark_ledger_deleted(self, rid):
        """兼容入口：实际实现见 interfaces/web/reports.py。"""
        return _report_service()._mark_ledger_deleted(rid)

    def _mark_ledger_restored(self, rid):
        """兼容入口：实际实现见 interfaces/web/reports.py。"""
        return _report_service()._mark_ledger_restored(rid)

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
            [p for p in LOG_DIR.glob("R-*") if p.is_dir()]
            + [p for p in (LOG_DIR / "v1").glob("R-*") if p.is_dir()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if rid:
            if not re.fullmatch(r"R-\d{8}-\d{3}", str(rid)):
                return {"ok": False, "error": "日志 ID 格式不合法"}
            folder = LOG_DIR / rid
            if not folder.is_dir():
                folder = LOG_DIR / "v1" / rid
        else:
            folder = dirs[0] if dirs else None
        if not folder or not folder.is_dir():
            # 区分「报告被删除（连同日志移入回收区）」与「日志从未存在」
            if rid and ((TRASH / f"{rid}_logs").is_dir()
                        or (TRASH / f"{rid}_v1logs").is_dir()):
                return {"ok": False, "deleted": True,
                        "error": "该报告已被删除，日志已随报告移入 outputs/.trash/（可恢复）"}
            if rid and not ((OUTPUTS / f"{rid}.json").exists()
                            or (OUTPUTS / "v1" / f"{rid}.json").exists()):
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
            "task": _legacy_task_view(read_json("task.json")),
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
        # 任务记录是唯一事实来源：JOBS 可能来自 .jobs/*.jsonl（重启恢复），
        # 与 state/tasks 可能不一致；状态/错误/报告一律以记录为准。
        review_id = str(job.get("review_id") or "")
        if review_id:
            try:
                record = TASKS.get(review_id)
            except (OSError, ValueError, KeyError, TypeError):
                record = None
            if record is not None:
                job = _job_view(record, job)
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
            rid = job["report_id"]
            path = OUTPUTS / f"{rid}.json"
            if not path.exists():
                path = OUTPUTS / "v1" / f"{rid}.json"
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
        # 本次手填的格式要求（分项表单）。给了它就以它为准，spec_opt 只当来源记录。
        custom_opt, custom_err = _resolve_format_custom((data or {}).get("format_custom"))
        if custom_err:
            return {"ok": False, "error": custom_err}
        # 目的组装（手写 + 上传的目的文件）在启动前完成：id 非法/超量要直接报错，
        # 不能等任务跑起来才失败。组装后的文本经 --purpose 传给子进程，
        # --purpose-max 告知解析上限（文件类目的可超过 300 字）。
        typed_purpose = _clean_purpose((data or {}).get("purpose"))
        purpose_text, purpose_err, purpose_quality = _assemble_purpose(
            typed_purpose, (data or {}).get("purpose_files"))
        if purpose_err:
            return {"ok": False, "error": purpose_err}
        purpose_max = len(purpose_text) if purpose_text else None
        # 「同一份材料 + 同一套参数」的指纹：重复提交不该建出第二个任务，
        # 也不该在台账里多一条同结论记录（实测生产台账里 286 条只对应 77 个目标）。
        # 自定义格式要求必须进指纹：同一份文件、同样的其他选项，
        # 只改了「行距 2 倍 → 固定 22 磅」也是一次新的审查。
        # 不进指纹的后果是幂等命中旧任务，用户以为按新要求判的，其实拿到的是旧结论。
        spec_key = (spec_opt if not custom_opt
                    else "custom:" + json.dumps(custom_opt.get("check") or {},
                                                ensure_ascii=False, sort_keys=True))
        key = _idempotency_key(file=file, llm=llm_opt, research=research_opt,
                               type_=type_opt, agent=agent_opt,
                               scenario=scenario_opt, spec=spec_key,
                               purpose_text=purpose_text)
        with JOBS_LOCK:
            now = time.time()
            # 1) 幂等命中：同一个键的未结束任务直接复用（返回同一个 job_id）
            existing = find_active(TASKS, key)
            if existing is not None:
                for old_id, old in list(JOBS.items()):
                    if old.get("review_id") == existing.review_id:
                        JOBS[old_id] = _job_view(existing, old)
                        print(f"[review] 幂等命中：沿用任务 {existing.review_id}"
                              f"（job {old_id}）", flush=True)
                        return {"ok": True, "job_id": old_id, "deduped": True,
                                "review_id": existing.review_id,
                                "status": JOBS[old_id]["status"]}
            # 2) 并发闸门：看**任务记录**，不看内存里的 running。后者在重启后是空的，
            #    而且同步接口根本不占位（等于没闸门）。
            busy = active_tasks(TASKS, stale_after=TASK_STALE_AFTER)
            if busy:
                return {"ok": False, "error": "已有审查任务进行中，请稍候",
                        "review_id": busy[0].review_id}
            # 3) 清掉一小时前结束的内存任务（只清视图与进度文件，不动任务记录）
            for old_id, old in list(JOBS.items()):
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
            # 任务身份 / 状态 / 幂等 / 超时全部交给 TaskService；
            # server 只把「这一次怎么跑」封成 Runner（内部仍是 review.py 子进程）。
            runner = _SubprocessRunner({
                "job_id": job_id,
                "file": file,
                "progress": progress,
                "llm_opt": llm_opt,
                "research_opt": research_opt,
                "type_opt": type_opt,
                "agent_opt": agent_opt,
                "purpose": purpose_text or None,
                "purpose_max": purpose_max,
                "purpose_quality": purpose_quality,
                "scenario_opt": scenario_opt,
                "spec_opt": spec_opt,
                "custom_opt": custom_opt,
            })
            try:
                record = _task_service().start_async_with(
                    runner, target, profile_id=agent_opt,
                    purpose=(purpose_text or None), engine="v1",
                    use_llm=llm_opt is True, timeout=REVIEW_TIMEOUT,
                    idempotency_key=key)
            except NotFoundError as exc:
                return {"ok": False, "error": str(exc)}
            except OSError as exc:
                return {"ok": False, "error": f"无法写入任务记录：{exc}"}
            review_id = record.review_id
            if record.status != RunStatus.QUEUED.value:
                # 幂等竞态：前置 find_active 之后、_open 之前，另一个请求先建了同一任务。
                existing = next((jid for jid, job in JOBS.items()
                                 if job.get("review_id") == review_id), None)
                return {"ok": True, "job_id": existing or job_id, "deduped": True,
                        "review_id": review_id, "status": record.status}
            # 只补视图；runner 可能已经先写过（setdefault 不覆盖它的进度）。
            JOBS.setdefault(job_id, {
                "id": job_id,
                "review_id": review_id,
                "file": file,
                "status": "running",
                "task_status": RunStatus.QUEUED.value,
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
                # 本次任务实际使用的超时：/api/review/status 可见，排查
                # 「为什么跑了很久才失败 / 为什么 300s 就断了」时有据可查。
                "timeout": REVIEW_TIMEOUT,
            })
        print(f"[review] 任务 {job_id}（{review_id}）已创建：{file}"
              f"（超时 {REVIEW_TIMEOUT}s）", flush=True)
        return {"ok": True, "job_id": job_id, "review_id": review_id}

    def _cancel_review(self, data):
        """取消正在跑（或排队）的审查。

        为什么现在能做到：执行进程登记在 `PROCS` 里，取消就是 `terminate` 它——
        旧实现只在内存里把状态改成 error，进程还在后台写完报告，用户看到
        「已取消」却在 `outputs/` 里多出一份报告。

        状态以任务记录为准：`terms` 之后子进程退出码非零，但 `TaskService`
        先落 CANCELLED、执行器回来时不会覆盖成 error。
        """
        d = data or {}
        job_id = str(d.get("job") or "").strip()
        review_id = str(d.get("review_id") or "").strip()
        if not review_id and job_id:
            with JOBS_LOCK:
                review_id = str((JOBS.get(job_id) or {}).get("review_id") or "")
        if not review_id:
            return {"ok": False, "error": "找不到任务：请给 job 或 review_id"}
        record = TASKS.get(review_id)
        if record is None:
            return {"ok": False, "error": f"找不到任务：{review_id}"}
        status = RunStatus(record.status)
        if status is RunStatus.CANCELLED:
            cancelled = record                      # 幂等：连点两下不该报错
        elif status.is_terminal:
            return {"ok": False,
                    "error": f"任务已是终态（{status.value}），不能取消"}
        else:
            # 先真的停进程：恢复出来的旧任务没有本进程令牌，只能靠 PROCS 兜底；
            # 本进程发起的任务还会被 runner 的令牌监视器再确认一次。
            with PROCS_LOCK:
                proc = PROCS.get(review_id)
            if proc is not None:
                try:
                    proc.terminate()
                except OSError as exc:
                    print(f"[review] 取消时终止子进程失败：{exc}", flush=True)
            try:
                cancelled = _task_service().cancel(review_id, reason="用户取消")
            except InvalidTransition as exc:
                return {"ok": False, "error": str(exc)}
            except NotFoundError as exc:
                return {"ok": False, "error": str(exc)}
        with JOBS_LOCK:
            for jid, job in JOBS.items():
                if job.get("review_id") != review_id:
                    continue
                job.update(_job_view(cancelled, job))
                job.update({"status": "cancelled",
                            "finished": job.get("finished") or time.time()})
        print(f"[review] 任务 {review_id} 已取消（{cancelled.status}）", flush=True)
        return {"ok": True, "review_id": review_id,
                "status": cancelled.status, "job": job_id or None}

    def _integrity(self):
        chains = [p for p in (LEDGER / "chain.log", LEDGER / "v1-chain.log")
                  if p.exists()]
        if not chains:
            return {"ok": False, "chain_ok": False, "entries": [],
                    "reports": [], "detail": "ledger 尚未生成（运行一次审查后才有）"}
        # 链的解读交给唯一实现：墓碑（独立行 / 历史原位行）不再被当作"坏行"，
        # 否则删过一份报告的用户会永远看到红叉（旧实现就是这样）。
        views = [(path, ledger_store.entries_for_api(path)) for path in chains]
        # V1 是默认链路；有 v1 链时以它作为主视图，legacy 链仍参与 chain_ok。
        primary_path, view = next(
            ((path, v) for path, v in views if path.name == "v1-chain.log"),
            views[0],
        )
        entries = view["entries"]
        chain_ok = all(v["chain_ok"] for _, v in views)
        broken_at = next((v["broken_at"] for _, v in views if not v["chain_ok"]), None)

        reports = []
        cur_rules_ver = integrity.rules_version()
        report_paths = [*OUTPUTS.glob("R-*.json"),
                        *(OUTPUTS / "v1").glob("R-*.json")]
        for p in sorted(report_paths):
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
            "tombstones": view["tombstones"],
            "deleted": view["deleted"],
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
                security_error = llm_client.endpoint_security_error(base_url)
                if security_error:
                    errors.append(security_error)
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
        security_error = llm_client.endpoint_security_error(base_url)
        if security_error:
            return {"ok": False, "error": security_error}
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
        effective_base = base_url or base.base_url
        security_error = llm_client.endpoint_security_error(effective_base)
        if security_error:
            return {"ok": False, "error": security_error}
        if _url_has_userinfo(effective_base):
            # userinfo 会原样回显并写进 llm_history.json，等于明文留凭据
            return {"ok": False,
                    "error": "Base URL 不允许包含用户名/密码（userinfo）"}
        if base_url:
            if not (base_url.startswith("http://") or base_url.startswith("https://")):
                return {"ok": False,
                        "error": "Base URL 必须以 http:// 或 https:// 开头"}
            if _url_netloc(base_url) is None:
                return {"ok": False, "error": "Base URL 格式不合法"}

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
            key_supplied = False
        else:
            key = str(data.get("api_key") or "").strip()
            key_supplied = bool(key)
            if key:
                cfg.api_key = key

        # 防「沿用旧 Key 测试新 host」：_save_llm_config / _restore_llm_history
        # 都有这道校验，只有试连曾漏掉——那等于把真 Key 交给任意 host。
        # 本机 / 局域网端点不需要 Key：允许试连，但必须把继承来的 Key 清掉。
        if base.api_key and not key_supplied and cfg.api_key:
            saved_netloc = _url_netloc(base.base_url)
            target_netloc = _url_netloc(cfg.base_url)
            if target_netloc != saved_netloc:
                if llm_client._is_local(cfg.base_url):
                    cfg.api_key = ""
                else:
                    return {"ok": False,
                            "error": "切换服务商后请重新填写 API Key"}

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
        # 同步接口也要占并发闸门：旧实现只查内存 JOBS 里的 running，而同步调用根本不写 JOBS，
        # 于是「同步 + 异步同时跑」是允许的——两条进程同时写 ledger/outputs。
        busy = active_tasks(TASKS, stale_after=TASK_STALE_AFTER)
        if busy:
            return {"ok": False, "error": "已有审查任务进行中，请稍候",
                    "review_id": busy[0].review_id}
        llm_opt = (data or {}).get("llm")
        research_opt = (data or {}).get("research")
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
        # 本次手填的格式要求（分项表单）。给了它就以它为准，spec_opt 只当来源记录。
        custom_opt, custom_err = _resolve_format_custom((data or {}).get("format_custom"))
        if custom_err:
            return {"ok": False, "error": custom_err}
        typed_purpose = _clean_purpose((data or {}).get("purpose"))
        purpose_text, purpose_err, purpose_quality = _assemble_purpose(
            typed_purpose, (data or {}).get("purpose_files"))
        if purpose_err:
            return {"ok": False, "error": purpose_err}
        # 与异步接口同一条链：TaskService 建任务 / 落状态 / 超时；
        # server 只把「这一次怎么跑」封成 Runner（内部仍是 review.py 子进程）。
        job_id = "sync-" + uuid.uuid4().hex[:8]
        progress = JOBS_DIR / f"{job_id}.jsonl"
        try:
            progress.unlink()
        except OSError:
            pass
        runner = _SubprocessRunner({
            "job_id": job_id,
            "file": file,
            "progress": progress,
            "llm_opt": llm_opt,
            "research_opt": research_opt,
            "type_opt": type_opt,
            "agent_opt": agent_opt,
            "purpose": purpose_text or None,
            "purpose_max": len(purpose_text) if purpose_text else None,
            "purpose_quality": purpose_quality,
            "scenario_opt": scenario_opt,
            "spec_opt": spec_opt,
            "custom_opt": custom_opt,
        })
        t0 = time.time()
        try:
            record = _task_service().start_review_with(
                runner, target, profile_id=agent_opt,
                purpose=(purpose_text or None), engine="v1",
                use_llm=llm_opt is True, timeout=REVIEW_TIMEOUT)
        except NotFoundError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": f"无法写入任务记录：{exc}"}
        elapsed = round(time.time() - t0, 1)
        review_id = record.review_id
        status = RunStatus(record.status)
        if status is RunStatus.COMPLETED:
            report_path = record.report_path
            if not report_path or not Path(str(report_path)).exists():
                return {"ok": False, "error": "审查完成但未生成报告",
                        "timeout": REVIEW_TIMEOUT, "elapsed": elapsed,
                        "review_id": review_id}
            report = _read_json(Path(str(report_path)))
            return {"ok": True, "report": report,
                    "id": report.get("review_id") or review_id,
                    "elapsed": elapsed, "timeout": REVIEW_TIMEOUT}
        error = record.error or "运行失败"
        payload = {"ok": False, "error": error, "timeout": REVIEW_TIMEOUT,
                   "elapsed": elapsed, "review_id": review_id}
        if error.startswith("任务超时"):
            # 对外沿用「审查超时」措辞；任务记录里保留 TaskService 的统一文案
            payload["error"] = f"审查超时（{REVIEW_TIMEOUT}s）"
        info = _parse_review_error(error) or {}
        if info:
            payload["error_kind"] = info.get("kind")
            payload["error_status"] = info.get("http_status")
        return payload


def _review_subprocess_env():
    """把控制台会话 Key 传给审查子进程；无会话 Key 时沿用父进程环境。"""
    key = llm_client.session_api_key()
    if not key:
        return None
    env = os.environ.copy()
    env["REVIEW_AGENT_LLM_API_KEY"] = key
    return env


def _llm_flag(llm_opt) -> str:
    """把前端的 llm 三态折成**显式**命令行开关（永远给一个）。

    为什么不能"不传就让配置决定"：`config/llm.local.json` 里 `enabled` 通常是 true，
    不传参数时 `review.py` 会按配置走 —— 用户没勾选，全文照样发往外部模型。
    这是默认外发，不是"用户选择外发"。
    `None`（接口调用方没带这个字段）按**不外发**处理：缺省必须落在保守的一侧。
    """
    return "--llm" if llm_opt is True else "--no-llm"


def _record_egress(kind, *, target=None, job_id=None, detail="", enabled=True):
    """记一行"哪些内容发给了第三方"（append-only jsonl）。

    为什么单独记：报告里能查到"用过模型"，查不到"这次把哪份材料送出去了"。
    出问题时要回答的是后者。这里只记**元信息**（对象、任务号、提供方），
    不记 prompt 内容——审计日志本身不该成为新的泄露面。
    """
    if not enabled:
        return
    try:
        EGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "kind": str(kind),
            "target": (str(target) if target else None),
            "job_id": job_id,
            "detail": str(detail or ""),
        }
        with open(EGRESS_LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        # 记录失败不该让审查跑不下去：但要显式说一句，不能静默
        print(f"[egress] 外发记录写入失败：{exc}", flush=True)


def _trash_destination(name):
    return _report_service()._trash_destination(name)


def _root_relative(path):
    return _report_service()._root_relative(path)


def _trash_manifest_path(rid):
    return _report_service()._trash_manifest_path(rid)


def _record_trash_move(manifest, src, dst):
    return _report_service()._record_trash_move(manifest, src, dst)


def _save_trash_manifest(manifest):
    return _report_service()._save_trash_manifest(manifest)


def _load_trash_manifest(rid):
    return _report_service()._load_trash_manifest(rid)


def _trash_suffix_match(name, prefix, suffix):
    return _report_service()._trash_suffix_match(name, prefix, suffix)


def _legacy_trash_items(rid, report=None, dedupe=True):
    return _report_service()._legacy_trash_items(
        rid, report=report, dedupe=dedupe)


def _trash_items(rid, report=None, dedupe=True):
    return _report_service()._trash_items(rid, report=report, dedupe=dedupe)


def _move_derived(src, prefix, moved, label, manifest=None):
    return _report_service()._move_derived(
        src, prefix, moved, label, manifest=manifest)


def _purge_owned_cache(directory, owner, prefix, label, moved, manifest=None):
    return _report_service()._purge_owned_cache(
        directory, owner, prefix, label, moved, manifest=manifest)


def _snapshot_ref(report):
    return _report_service()._snapshot_ref(report)


def _snapshot_file(ref):
    return _report_service()._snapshot_file(ref)


def _snapshot_referenced(ref, exclude_rid):
    return _report_service()._snapshot_referenced(ref, exclude_rid)


def _find_report(rid):
    return _report_service()._find_report(rid)


def _purge_derived(rid, report=None, manifest=None):
    return _report_service()._purge_derived(
        rid, report=report, manifest=manifest)


def _mark_ledger_deleted(rid):
    return _report_service()._mark_ledger_deleted(rid)


def _mark_ledger_restored(rid):
    return _report_service()._mark_ledger_restored(rid)

RESULT_MARKER = "REVIEW_RESULT "


def _parse_review_result(text):
    """从子进程 stdout 里取「这次跑出来的报告是哪一份」。

    与 `_parse_review_error` 对称：父进程不再用「`outputs/` 跑前跑后的差集」认领报告——
    那个做法在两个任务并发时会认领到**对方的**报告，而且不会报错，只会让用户
    下载到别人的结论。子进程自己说出 review_id 与路径，归属就没有猜测空间。
    """
    for raw in reversed((text or "").splitlines()):
        line = raw.strip()
        if not line.startswith(RESULT_MARKER):
            continue
        try:
            payload = json.loads(line[len(RESULT_MARKER):])
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("review_id"):
            return payload
    return None


def _allocate_review_id():
    """向 `review.py` 要一个号（它内部用 O_EXCL 锁 + 扫 outputs/ledger/state/tasks 发号）。

    为什么不在这里自己算：号必须与报告里那个一致，而发号规则会随产物布局变化
    （`state/tasks` 就是后来才加进去的）。复制一份到服务端，等于埋一个
    「某天两边的号开始撞」的定时炸弹。
    """
    cmd = [str(PY), str(ROOT / "review.py"), "--print-review-id"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT),
                              timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法分配审查号：{exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"无法分配审查号（退出码 {proc.returncode}）："
                           f"{(proc.stderr or '').strip()[:200]}")
    for raw in reversed((proc.stdout or "").splitlines()):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("review_id"):
            return str(payload["review_id"])
    raise RuntimeError(f"无法解析审查号：{(proc.stdout or '')[:200]}")


def _idempotency_key(*, file, llm, research, type_, agent, scenario, spec,
                     purpose_text) -> str:
    """「同一份材料 + 同一套参数」的指纹，用来防止重复提交产生第二个任务。

    刻意包含 purpose：同一份合同、换个审查目的，是**另一个**任务。
    刻意用哈希而不是原串：它会落进 `state/tasks/`，不该把用户填的目的写成明文键。
    """
    payload = json.dumps({
        "file": str(file), "llm": llm, "research": research, "type": type_,
        "agent": agent, "scenario": scenario, "spec": spec,
        "purpose": (purpose_text or ""),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _job_view(record: TaskRecord, job: dict | None = None) -> dict:
    """把任务记录折成前端认识的 job 形状（`running/done/error` 三态之外加 `cancelled`）。

    前端只读 `status` 的三态；这里多做一步映射而不是改前端：`queued` 视为 running，
    `cancelled/aborted` 视为 error 但带 `cancelled=True`——老前端不会因此白屏。
    """
    status = RunStatus(record.status)
    if status in (RunStatus.COMPLETED,):
        shown = "done"
    elif status in (RunStatus.CANCELLED, RunStatus.ABORTED):
        shown = "cancelled"
    elif status in (RunStatus.FAILED,):
        shown = "error"
    else:
        shown = "running"
    view = dict(job or {})
    view.update({
        "id": view.get("id"),
        "review_id": record.review_id,
        "status": shown,
        "task_status": status.value,
        "error": record.error,
        "report_id": record.report_path and Path(record.report_path).stem
                     or view.get("report_id"),
        "finished": view.get("finished") or (
            time.time() if status.is_terminal else None),
    })
    return view


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


def _resolve_format_custom(raw):
    """校验前端手填的格式要求，返回**已构建**的规格 dict。

    为什么不直接拒绝、要在这里先构建一遍：`build_custom` 的白名单校验
    是"前端能填什么 = 后端认什么"的唯一来源。放到子进程里校验也能跑，
    但那时任务已经建好、号已经发出，用户拿到的是一个失败的任务而不是
    「这一栏填错了」。空表单也在这里报错（而不是静默按"不判版式"跑）——
    用户既然展开了表单，就是要判版式。
    """
    if raw in (None, "", {}, []):
        return None, None
    if not isinstance(raw, dict):
        return None, "format_custom 必须是对象"
    try:
        return format_spec.build_custom(raw), None
    except format_spec.FormatSpecError as e:
        return None, f"格式要求有误：{e}"


# ---------------------------------------------------------------------------
# 服务重启后的任务状态恢复
#
# JOBS 只在内存里。服务一重启，正在轮询的前端就永远停在「审查中…」。
# 启动时扫 logs/.jobs/*.jsonl：有对应报告就恢复成 done，没有就明确报
# 「任务已中断」，绝不伪造 running（进程句柄已经丢了，没人会再更新它）。
# ---------------------------------------------------------------------------
RECOVER_INTERRUPTED_ERROR = "服务重启，任务已中断，请重新审查"


def _parse_iso_ts(value):
    """把 review.py 写的 ISO 时间转成 epoch；解析失败返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _read_job_events(path):
    """读取 .jobs/<job>.jsonl，坏行跳过（一行坏掉不该让整个任务消失）。"""
    events = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _norm_job_target(value):
    return str(value or "").strip().replace("\\", "/")


def _find_recovered_report(events, outputs_dir, first_ts, last_ts):
    """按「目标 + 时间窗」找这次任务生成的报告。

    .jobs/*.jsonl 里没有 report_id，只有 S1~S6 进度事件。报告对象里有
    target_path 和 timestamp：同一目标、时间落在本次任务窗口内的那份就是它。
    同一时间只允许一个任务运行，所以不需要也不应该按文件 mtime 瞎猜。
    """
    target = ""
    for event in events:
        detail = event.get("detail")
        if isinstance(detail, dict):
            candidate = detail.get("待审对象")
            if candidate:
                target = _norm_job_target(candidate)
                break

    best = None
    paths = [*Path(outputs_dir).glob("R-*.json"),
             *(Path(outputs_dir) / "v1").glob("R-*.json")]
    for path in paths:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(report, dict):
            continue
        report_target = _norm_job_target(report.get("target_path"))
        if target and report_target and target != report_target:
            continue
        report_ts = _parse_iso_ts(report.get("timestamp"))
        if report_ts is None:
            try:
                report_ts = path.stat().st_mtime
            except OSError:
                continue
        # 报告 timestamp 在 S6 事件之前几毫秒生成；给 5s 容差，
        # 后面最多等到超时窗口结束（进程退出后写报告）。
        if report_ts < first_ts - 5 or report_ts > last_ts + 300:
            continue
        distance = abs(report_ts - last_ts)
        if best is None or distance < best[0]:
            best = (distance, str(report.get("review_id") or path.stem))
    return best[1] if best else None


def _job_state_from_jsonl(path, timeout, outputs_dir=None, now=None):
    """把一个 .jobs/*.jsonl 解析成 JOBS 里的任务状态（纯函数，便于单测）。

    - 找得到对应报告 -> done + report_id
    - 找不到报告    -> error；超过 timeout 的标记 timed_out
    - 永远不返回 running
    """
    path = Path(path)
    now = time.time() if now is None else float(now)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = now
    events = _read_job_events(path)
    first_ts = _parse_iso_ts(events[0].get("ts")) if events else None
    last_ts = _parse_iso_ts(events[-1].get("ts")) if events else None
    first_ts = mtime if first_ts is None else first_ts
    last_ts = mtime if last_ts is None else last_ts

    report_id = None
    if events:
        report_id = _find_recovered_report(
            events, OUTPUTS if outputs_dir is None else outputs_dir,
            first_ts, last_ts)

    stale = (now - mtime) > max(0, int(timeout))
    status = "done" if report_id else "error"
    last_step = None
    if events and events[-1].get("step"):
        last_step = str(events[-1].get("step"))
    target = ""
    for event in events:
        detail = event.get("detail")
        if isinstance(detail, dict) and detail.get("待审对象"):
            target = _norm_job_target(detail.get("待审对象"))
            break

    return {
        "id": path.stem,
        "file": target,
        "status": status,
        "created": first_ts,
        "finished": last_ts,
        "report_id": report_id,
        "error": None if status == "done" else RECOVER_INTERRUPTED_ERROR,
        "llm": None,
        "research": None,
        "type": None,
        "agent": None,
        "scenario": None,
        "format_spec": None,
        "timeout": int(timeout),
        "recovered": True,
        "last_step": last_step,
        # 没有报告，且进度文件已经超过本次超时窗口：大概率是被超时打断的。
        # 两个分支都是 error（进程句柄已丢），这里只用于诊断/展示。
        "timed_out": bool(stale and status == "error"),
        "recover_reason": ("done" if status == "done"
                           else "timeout" if stale else "interrupted"),
    }


# ---------------------------------------------------------------------------
# 任务生命周期统一：TaskService 是唯一状态事实源，server 只负责「怎么跑」
# ---------------------------------------------------------------------------
_TASK_SERVICE: TaskService | None = None
_TASK_SERVICE_KEY: tuple[int, str] | None = None
# 建服务要串行：两个 HTTP 线程同时首次调用会各建一个实例，
# 于是「取消」拿到的令牌可能不属于正在执行的那个实例（取消只改状态、进程照跑）。
_TASK_SERVICE_LOCK = threading.Lock()


def _unused_runner(*_args, **_kwargs):
    """默认 runner 占位：server 每次都通过 `start_*_with` 传入具体执行器。"""
    raise RuntimeError("TaskService 的默认 runner 不应被调用（server 走 start_*_with）")


def _task_service() -> TaskService:
    """进程内唯一的 TaskService 实例。

    任务记录（`state/tasks/`）是唯一事实来源；`JOBS` 只是前端视图。
    测试会替换 `TASKS` / `ROOT`，因此按二者身份重建实例，避免把旧存储
    或旧根目录带进下一个用例。
    """
    global _TASK_SERVICE, _TASK_SERVICE_KEY
    with _TASK_SERVICE_LOCK:
        key = (id(TASKS), str(ROOT))
        if _TASK_SERVICE is None or _TASK_SERVICE_KEY != key:
            _TASK_SERVICE = TaskService(
                runner=_unused_runner,
                store=TASKS,
                allocate=lambda _day: _allocate_review_id(),
                root=ROOT,
                stale_after=TASK_STALE_AFTER,
            )
            _TASK_SERVICE_KEY = key
        return _TASK_SERVICE


class _SubprocessRunner:
    """Runner 适配层：一次 server 审查 = 一个 review.py 子进程。

    刻意不砍进程隔离：`review.py` 仍在独立进程里跑（超时/取消/崩溃不会拖垮
    HTTP 服务）。任务状态不在这里写——`TaskService` 负责；这里只维护
    `PROCS`（取消用）与 `JOBS`（前端视图）。
    """

    def __init__(self, spec: dict) -> None:
        self.spec = spec

    def __call__(self, target, *, review_id, profile_id, purpose, engine,
                 use_llm, token=None):
        s = self.spec
        return _run_review_job(
            s["job_id"], review_id, s["file"], s["llm_opt"], s["progress"],
            s["type_opt"], s["agent_opt"], s["purpose"], s["purpose_max"],
            s["purpose_quality"], s["scenario_opt"], s["spec_opt"],
            s["research_opt"], s["custom_opt"], token=token, engine=engine)


def _watch_cancel(proc, token, *, poll=0.1, grace=10.0):
    """子进程运行期间盯取消令牌：置位后先 terminate，10 秒不退再 kill。

    超时语义已收口到 `TaskService`（它置位同一个令牌）；这里只负责
    「令牌变了就真的停进程」，避免同一件事有两个超时实现。
    """
    terminated_at = None
    while True:
        if proc.poll() is not None:
            return
        if token is not None and token.cancelled:
            if terminated_at is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                terminated_at = time.time()
            elif time.time() - terminated_at > grace:
                try:
                    proc.kill()
                except OSError:
                    pass
                return
        time.sleep(poll)


def _recover_jobs(now=None):
    """启动时恢复 .jobs 状态，并清理超过 7 天的进度文件。"""
    now = time.time() if now is None else float(now)
    recovered = 0
    pruned = 0
    try:
        paths = sorted(JOBS_DIR.glob("*.jsonl"))
    except OSError:
        return 0
    for path in paths:
        job_id = path.stem
        with JOBS_LOCK:
            existing = JOBS.get(job_id)
        if existing and existing.get("status") == "running":
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if now - mtime > JOB_RETENTION_SECONDS:
            try:
                path.unlink()
                pruned += 1
            except OSError:
                pass
            with JOBS_LOCK:
                JOBS.pop(job_id, None)
            continue
        try:
            state = _job_state_from_jsonl(path, REVIEW_TIMEOUT, now=now)
        except Exception:                                    # noqa: BLE001
            continue
        with JOBS_LOCK:
            JOBS.setdefault(job_id, state)
        recovered += 1
    if recovered or pruned:
        print(f"[recover] 恢复 {recovered} 个任务状态，"
              f"清理 {pruned} 个超过 7 天的进度文件", flush=True)
    # 任务记录也要体检：`.jobs/*.jsonl` 只覆盖「有进度文件的那次」，
    # 而并发闸门与「跑完没有」现在都看 `state/tasks/`。没有这一步，
    # 一次崩溃留下的 running 记录会永远挡住后续所有审查。
    try:
        report = _task_service().recover(
            has_checkpoint=lambda run_id: (STATE_DIR / "runs" / f"{run_id}.state.json").exists())
    except OSError as exc:
        print(f"[recover] 任务记录体检失败：{exc}", flush=True)
        return recovered
    if report.total:
        print(f"[recover] 任务记录：可续 {len(report.resumable)} 个，"
              f"判废 {len(report.orphaned)} 个", flush=True)
    return recovered


def _run_review_job(job_id, review_id, file, llm_opt, progress_path, type_opt=None,
                    agent_opt=None, purpose=None, purpose_max=None,
                    purpose_quality=None, scenario_opt=None, format_spec_opt=None,
                    research_opt=None, format_custom=None, engine="v1",
                    token=None):
    """Runner 的执行体：跑 review.py，进度事件写入 progress_path。

    与改造前的差别（问题 2 / 第二阶段）：这里**不再写任务状态**。
    任务创建、QUEUED→RUNNING、完成/失败/取消、超时全部由 `TaskService` 负责；
    本函数只做三件事：起子进程、盯取消令牌、把结果折成 `TaskOutcome` 返回。
    `JOBS` 仍只是前端视图。

    进程隔离保留：`review.py` 在独立进程中运行，取消/超时通过令牌 terminate。
    """
    cmd = [str(PY), str(ROOT / "review.py"), file,
           "--progress", str(progress_path), "--review-id", str(review_id)]
    # 引擎必须显式钉死：子进程会继承 server 的环境变量，若宿主机设了
    # REVIEW_ENGINE=legacy，不传 --engine 就会「任务记录写 v1、实际跑 legacy」。
    cmd.extend(["--engine", str(engine or "v1")])
    # 显式开关：不传就等于"按配置文件决定"，而配置里 enabled 通常是 true
    cmd.append(_llm_flag(llm_opt))
    if llm_opt is True:
        _record_egress("llm", target=file, job_id=job_id,
                       detail="review.py --llm（全文分块送审）")
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
    if format_custom:
        # 本次手填的格式要求：整块 check 以 JSON 传过去。
        # 走 JSON 而不是拼成 --format-spec <id>，是因为它没有 id——
        # 它只对这一次生效，不进 config/format_specs.yaml。
        cmd.extend(["--format-spec-json",
                    json.dumps(format_custom.get("check") or {},
                               ensure_ascii=False, separators=(",", ":"))])
    elif format_spec_opt:
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
    timeout = REVIEW_TIMEOUT
    status = "done"
    error = None
    error_kind = None
    report_id = None
    result = None
    print(f"[review] 任务 {job_id}（{review_id}）开始：{file}"
          f"（超时 {timeout}s）", flush=True)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, cwd=str(ROOT),
                                env=_review_subprocess_env())
        with PROCS_LOCK:
            PROCS[review_id] = proc
        watcher = threading.Thread(target=_watch_cancel, args=(proc, token),
                                   name=f"cancel-{review_id}", daemon=True)
        watcher.start()
        try:
            # 不在这里设超时：超时语义只有 TaskService 一份（它置位同一个令牌），
            # 令牌一置位，watcher 就 terminate / kill 子进程。
            stdout, stderr = proc.communicate()
        finally:
            with PROCS_LOCK:
                PROCS.pop(review_id, None)
        current = TASKS.get(review_id)
        if (token is not None and token.cancelled and current is not None
                and RunStatus(current.status) is RunStatus.RUNNING):
            # 取消写记录与子进程退出是两个线程：给状态一次落盘机会再判视图
            time.sleep(0.05)
            current = TASKS.get(review_id) or current
        cancelled = (current is not None
                     and RunStatus(current.status) is RunStatus.CANCELLED)
        timed_out = bool(token is not None and token.cancelled and not cancelled)
        if cancelled:
            status = "cancelled"
            error = current.error
        elif proc.returncode != 0:
            status = "error"
            if timed_out:
                # TaskService 已经把「任务超时」写进记录；视图以记录为准
                error = (current.error if current is not None else None) \
                    or f"审查超时（{timeout}s）"
                error_kind = "system"
            else:
                raw = (stderr or stdout or f"退出码 {proc.returncode}")[-2000:]
                info = _parse_review_error(raw) or {}
                error_kind = info.get("kind")
                error = _strip_error_marker(raw) or raw
        else:
            result = _parse_review_result(stdout)
            report_path = (result or {}).get("report_path")
            if not report_path or not Path(str(report_path)).exists():
                status = "error"
                error = "审查完成但未生成报告（子进程未报告结果路径）"
                error_kind = "system"
            else:
                report_id = (result or {}).get("report_id") or review_id
    except Exception as e:                              # noqa: BLE001 - 执行器边界
        status = "error"
        error = str(e)[:2000]
        error_kind = "system"

    # 任务状态由 TaskService 落盘；这里只把结果带回去。
    current = TASKS.get(review_id)
    stage = Stage(current.stage) if current is not None else Stage.INTAKE
    if status == "done":
        outcome = TaskOutcome(status=RunStatus.COMPLETED, stage=Stage.FINALIZE,
                              verdict=(result or {}).get("verdict"),
                              report_path=(result or {}).get("report_path"))
    elif status == "cancelled":
        outcome = TaskOutcome(status=RunStatus.ABORTED, stage=stage, error=error)
    else:
        outcome = TaskOutcome(status=RunStatus.FAILED, stage=stage, error=error)

    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update({
                "status": status,
                "task_status": (RunStatus.CANCELLED.value if cancelled
                                else outcome.status.value),
                "error": error,
                # 错误分类（input / config / system）：前端据此决定「让用户改参数」
                # 还是「提示联系维护者」，不用去正则匹配 error 文本。
                "error_kind": error_kind,
                "report_id": report_id,
                "finished": time.time(),
                "elapsed": round(time.time() - started, 1),
                "timeout": timeout,
            })
    print(f"[review] 任务 {job_id}（{review_id}）{status}："
          f"{report_id or error or ''}（{round(time.time() - started, 1)}s，"
          f"超时上限 {timeout}s）", flush=True)
    return outcome


def main():
    global REVIEW_TIMEOUT
    # 优先级 CLI > 环境变量 > 默认；写一次，起 server 之后线程内只读。
    REVIEW_TIMEOUT = _parse_review_timeout(sys.argv[1:])
    port = 8000
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except Exception:
            pass
    # 先按新超时恢复上次进程留下的任务，再开始接请求：
    # 否则服务重启后旧任务永远停在 running。
    _recover_jobs()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"审查 Agent 控制台已启动： http://127.0.0.1:{port}")
    print(f"  根目录：{ROOT}")
    print(f"  解释器：{PY}")
    print(f"  审查超时：{REVIEW_TIMEOUT}s"
          f"（--review-timeout / REVIEW_AGENT_REVIEW_TIMEOUT）")
    print("  按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
