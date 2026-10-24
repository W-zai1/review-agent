#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ocr.py —— 多引擎 OCR 适配层（零强制依赖，自动探测与降级）

引擎优先级（从优到劣，取决于本机具备什么）：
  1. vision   视觉大模型（走项目已有的 OpenAI 兼容客户端，content 数组多模态）
  2. winrt    Windows 内置 OCR（WinRT，**零安装**，支持 zh-Hans-CN / en-US）
  3. rapidocr RapidOCR（可选：pip install rapidocr-onnxruntime，纯 ONNX 无需 torch）
  4. tesseract Tesseract（可选：需装二进制 + 中文语言包）

设计原则：
  - **探测优先**：不装任何东西也能跑（Windows 上直接用 winrt）
  - **失败不伪造**：所有引擎都不可用时抛 OcrUnavailable，调用方据此明确标注
    「未做内容审查」，绝不返回空字符串冒充「已识别」
  - OCR 结果本质有误差，识别文本的 `limitations` 里会提示

单独试跑：
    python tools/ocr.py 图片.png
    python tools/ocr.py --engines          # 只看本机可用引擎
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WINRT_SCRIPT = Path(__file__).resolve().parent / "ocr_winrt.ps1"
OCR_CONFIG = ROOT / "config" / "ocr.json"

# 视觉模型配置示例（config/ocr.json，可选）
# {
#   "engine": "auto",
#   "vision": {
#     "base_url": "http://127.0.0.1:11434/v1",
#     "model": "qwen2.5vl:3b",
#     "api_key": "",
#     "timeout": 120
#   }
# }

OCR_PROMPT = (
    "请把这张图片里的文字**逐字**转写出来。要求：\n"
    "1. 只输出识别到的文字本身，不要解释、不要总结、不要加标题\n"
    "2. 保留原文的段落与换行\n"
    "3. 表格用制表符或竖线分隔\n"
    "4. 看不清的地方用 [无法辨认] 标注，不要猜测"
)


class OcrUnavailable(Exception):
    """本机没有可用的 OCR 引擎。"""


class OcrError(Exception):
    """OCR 引擎存在但执行失败。"""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_ocr_config() -> dict:
    try:
        data = json.loads(OCR_CONFIG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _vision_cfg() -> dict:
    cfg = load_ocr_config()
    vision = cfg.get("vision")
    return vision if isinstance(vision, dict) else {}


# --------------------------------------------------------------------------
# 引擎探测
# --------------------------------------------------------------------------
def _has_winrt() -> bool:
    if os.name != "nt" or not WINRT_SCRIPT.exists():
        return False
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    return bool(exe)


def _has_tesseract() -> bool:
    return bool(shutil.which("tesseract"))


def _has_rapidocr() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _has_vision() -> bool:
    v = _vision_cfg()
    return bool(v.get("base_url") and v.get("model"))


def available_engines() -> list:
    """返回本机可用的 OCR 引擎列表（按优先级）。"""
    engines = []
    if _has_vision():
        engines.append("vision")
    if _has_winrt():
        engines.append("winrt")
    if _has_rapidocr():
        engines.append("rapidocr")
    if _has_tesseract():
        engines.append("tesseract")
    return engines


# --------------------------------------------------------------------------
# 引擎 1：Windows 内置 OCR（WinRT）
# --------------------------------------------------------------------------
def _ocr_winrt(path: Path) -> str:
    exe = shutil.which("powershell") or shutil.which("powershell.exe")
    if not exe:
        raise OcrUnavailable("未找到 powershell.exe")
    with tempfile.TemporaryDirectory(prefix="ra-ocr-") as tmp:
        out = Path(tmp) / "out.txt"
        try:
            proc = subprocess.run(
                [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(WINRT_SCRIPT), "-Image", str(path), "-Out", str(out)],
                capture_output=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            raise OcrError("Windows OCR 超时（>120s）")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise OcrError(err or f"Windows OCR 退出码 {proc.returncode}")
        if not out.exists():
            raise OcrError("Windows OCR 未产出结果文件")
        return out.read_text(encoding="utf-8", errors="replace").strip()


# --------------------------------------------------------------------------
# 引擎 2：视觉大模型（OpenAI 兼容多模态）
# --------------------------------------------------------------------------
def _ocr_vision(path: Path) -> str:
    v = _vision_cfg()
    try:
        import llm_client
        from dataclasses import replace
    except Exception as e:                                     # noqa: BLE001
        raise OcrError(f"无法加载 llm_client：{e}")
    try:
        base = llm_client.load_config()
    except Exception as e:                                     # noqa: BLE001
        raise OcrError(f"LLM 配置加载失败：{e}")
    cfg = replace(
        base,
        base_url=str(v.get("base_url") or base.base_url),
        model=str(v.get("model") or base.model),
        timeout=float(v.get("timeout") or 120),
    )
    if v.get("api_key"):
        cfg.api_key = str(v["api_key"])
    if not cfg.model:
        raise OcrError("视觉模型未配置 model")

    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": OCR_PROMPT},
            {"type": "image_url",
             "image_url": {"url": f"data:{mime};base64,{data}"}},
        ],
    }]
    client = llm_client.LLMClient(cfg)
    try:
        res = client.chat(messages)
    except Exception as e:                                     # noqa: BLE001
        raise OcrError(f"视觉模型调用失败：{e}")
    return (res.content or "").strip()


# --------------------------------------------------------------------------
# 引擎 3：RapidOCR（可选）
# --------------------------------------------------------------------------
def _ocr_rapidocr(path: Path) -> str:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as e:                                     # noqa: BLE001
        raise OcrUnavailable(f"rapidocr-onnxruntime 未安装：{e}")
    try:
        engine = RapidOCR()
        result, _ = engine(str(path))
    except Exception as e:                                     # noqa: BLE001
        raise OcrError(f"RapidOCR 执行失败：{e}")
    if not result:
        return ""
    lines = []
    for item in result:
        try:
            lines.append(str(item[1]))
        except Exception:
            continue
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# 引擎 4：Tesseract（可选）
# --------------------------------------------------------------------------
def _ocr_tesseract(path: Path) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrUnavailable("未找到 tesseract")
    lang = "chi_sim+eng"
    try:
        proc = subprocess.run([exe, str(path), "stdout", "-l", lang],
                              capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise OcrError("Tesseract 超时（>120s）")
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if "Failed loading language" in err or "Tesseract couldn't load" in err:
            raise OcrError("Tesseract 缺少中文语言包（需 chi_sim）")
        raise OcrError(err or f"Tesseract 退出码 {proc.returncode}")
    return (proc.stdout or b"").decode("utf-8", "replace").strip()


ENGINES = {
    "vision": _ocr_vision,
    "winrt": _ocr_winrt,
    "rapidocr": _ocr_rapidocr,
    "tesseract": _ocr_tesseract,
}

# CJK 字符区间（含中文标点、全角符号）
_CJK = r"一-鿿　-〿＀-￯"


def normalize_ocr_text(text: str) -> str:
    """清理 OCR 引擎在汉字之间插入的空格。

    Windows OCR 逐字输出，识别中文会得到「合 同 甲 方」，这些空格会导致
    中文正则规则（合同/甲方/乙方…）全部漏匹配。这里把汉字之间的空格去掉。
    """
    if not text:
        return text
    prev = None
    out = text
    while prev != out:
        prev = out
        out = re.sub(rf"([{_CJK}])[ \t]+(?=[{_CJK}])", r"\1", out)
    return out


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
def ocr_image(path, prefer=None) -> tuple:
    """对图片做 OCR，返回 (text, meta)。

    meta = {
        "engine": "winrt" | ... ,
        "chars": int,
        "warnings": [str],
        "available": [已探测到的引擎],
    }
    没有可用引擎时抛 OcrUnavailable；引擎存在但失败抛 OcrError。
    """
    path = Path(path)
    if not path.exists():
        raise OcrError(f"图片不存在：{path}")
    engines = available_engines()
    if not engines:
        raise OcrUnavailable(
            "本机没有可用的 OCR 引擎（Windows 内置 OCR 不可用 / 未配置视觉模型 / "
            "未安装 rapidocr 或 tesseract）")
    if prefer:
        if prefer not in ENGINES:
            raise OcrError(f"未知 OCR 引擎：{prefer}")
        order = [prefer] + [e for e in engines if e != prefer]
    else:
        order = engines

    errors = []
    for name in order:
        try:
            text = normalize_ocr_text(ENGINES[name](path))
        except OcrUnavailable as e:
            errors.append(f"{name}: 不可用（{e}）")
            continue
        except OcrError as e:
            errors.append(f"{name}: 失败（{e}）")
            continue
        except Exception as e:                                 # noqa: BLE001
            errors.append(f"{name}: 异常（{e}）")
            continue
        warnings = []
        if not text.strip():
            warnings.append("OCR 未识别出任何文字（图片可能无文字或质量过低）")
        if len(errors):
            warnings.append("部分引擎失败：" + "；".join(errors))
        return text, {
            "engine": name,
            "chars": len(text),
            "available": engines,
            "cjk_spaces_removed": True,
            "warnings": warnings,
        }
    raise OcrError("所有 OCR 引擎均失败：" + "；".join(errors))


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--engines":
        print(json.dumps({
            "available": available_engines(),
            "vision_configured": _has_vision(),
            "winrt_script": str(WINRT_SCRIPT) if WINRT_SCRIPT.exists() else None,
        }, ensure_ascii=False, indent=2))
        sys.exit(0)
    if not args:
        print("usage: python tools/ocr.py <image> | --engines")
        sys.exit(2)
    try:
        text, meta = ocr_image(args[0])
    except (OcrUnavailable, OcrError) as e:
        print(f"✗ {e}")
        sys.exit(1)
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    print("--- OCR 文本 ---")
    print(text)
