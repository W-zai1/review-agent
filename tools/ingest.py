#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ingest.py —— 文档摄取：把二进制文档转成「可审查文本」（零依赖，仅标准库）

职责边界：
  - 只负责「取文本」，**不负责审查**；审查仍由 review.py 的文本链路完成
  - 原文件由调用方归档（targets/uploads/_originals/），提取文本另存，两者可对照
  - 提取失败一律**显式告警**，绝不静默返回空字符串冒充「已审查」

支持矩阵：
  | 类型                | 提取方式                                  | 备注                       |
  |---------------------|-------------------------------------------|----------------------------|
  | .pdf                | 有 pypdf 用 pypdf，否则内置提取器兜底      | 内置为兜底，复杂排版可能失真 |
  | .docx / .docm       | zipfile 读 word/document.xml，解析 w:t      | 纯标准库，稳定             |
  | .doc / .rtf         | 明确拒绝                                  | 提示转存为 .docx           |
  | 图片 .png/.jpg/.…   | 不做 OCR，标记 vision_required=True        | 需视觉模型或人工复核       |
  | .zip                | 递归展开（防 zip slip + 解压量上限）        | 成员按各自类型继续处理     |
  | 其它                | 明确拒绝并说明原因                         | —                          |

安全约束（防止被恶意文件拖垮）：
  - PDF 流解压限长，防 zlib 炸弹
  - ZIP 解压总量限 50 MB、成员数限 200、嵌套深度限 2、防 zip slip 路径穿越
  - 提取文本限长（默认 200k 字符），超出截断并告警
"""
from __future__ import annotations

import io
import os
import re
import zipfile
import zlib
from pathlib import Path

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
MAX_TEXT_CHARS = 200_000        # 单份提取文本上限
MAX_PDF_STREAM_BYTES = 8 * 1024 * 1024   # 单个 PDF 流解压上限
MAX_ZIP_TOTAL_BYTES = 50 * 1024 * 1024   # 压缩包解压总量上限
MAX_ZIP_MEMBERS = 200           # 压缩包成员数上限
MAX_ZIP_DEPTH = 2
MAX_DOCX_XML_BYTES = 8 * 1024 * 1024   # 单个 XML 成员解压上限               # 嵌套压缩包深度上限

PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx", ".docm"}
DOC_EXTS = {".doc", ".rtf"}
IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic",
}
ZIP_EXTS = {".zip"}
# 可直接作为文本审查对象的扩展名（与 server.UPLOAD_ALLOWED 保持一致）
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".json", ".yaml", ".yml", ".html", ".css", ".csv", ".log", ".xml",
}

IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp",
    ".tif": "image/tiff", ".tiff": "image/tiff", ".heic": "image/heic",
}


class IngestError(Exception):
    """无法摄取该文件（调用方应转成 4xx 提示）。"""


# --------------------------------------------------------------------------
# 种类判定
# --------------------------------------------------------------------------
def sniff(path) -> str:
    """按扩展名判定文件种类：text/pdf/docx/image/zip/unsupported。"""
    ext = Path(path).suffix.lower()
    if ext in PDF_EXTS:
        return "pdf"
    if ext in DOCX_EXTS:
        return "docx"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in ZIP_EXTS:
        return "zip"
    if ext in TEXT_EXTS:
        return "text"
    if ext in DOC_EXTS:
        return "legacy-doc"
    return "unsupported"


def _safe_join(dest: Path, name: str) -> Path:
    """把压缩包成员名安全地落到 dest 内（防 zip slip 路径穿越）。

    含 '..' 或绝对路径的成员名一律**拒绝**（明确是恶意/异常包），
    而不是悄悄净化——静默净化会让用户以为包被完整接收。
    """
    raw = str(name).replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise IngestError(f"压缩包成员含绝对路径，已拒绝：{name}")
    if any(part == ".." for part in raw.split("/")):
        raise IngestError(f"压缩包成员试图写出目标目录，已拒绝：{name}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts:
        raise IngestError("压缩包内含非法路径")
    target = dest.joinpath(*parts)
    try:
        resolved = target.resolve()
        base = dest.resolve()
    except OSError:
        raise IngestError("压缩包内含非法路径")
    if base != resolved and base not in resolved.parents:
        raise IngestError(f"压缩包成员试图写出目标目录：{name}")
    return target


def _decode_bytes(raw: bytes) -> str:
    """启发式解码：PDF/DOCX 里的文本可能是 UTF-16BE / UTF-8 / GB18030 / Latin-1。"""
    if not raw:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if b"\x00" in raw[:200]:
        try:
            return raw.decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def _truncate(text: str, limit: int = MAX_TEXT_CHARS):
    if len(text) <= limit:
        return text, None
    return text[:limit], f"提取文本超过 {limit} 字符，已截断"


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def _pdf_builtin(data: bytes):
    """内置 PDF 提取器：解压内容流 → 抽取文本算子。

    只做「尽力而为」的抽取，遇到扫描版 PDF（无文本层）会得到空文本，
    此时由上层显式告警，不伪造结论。
    """
    warnings = []
    chunks = []
    for raw_stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        payload = None
        try:
            # 限长解压，防 zlib 炸弹
            dec = zlib.decompressobj()
            payload = dec.decompress(raw_stream, MAX_PDF_STREAM_BYTES)
            if dec.unconsumed_tail:
                warnings.append("PDF 内容流超出解压上限，可能截断")
        except zlib.error:
            payload = raw_stream          # 未压缩流
        if not payload:
            continue
        for block in re.findall(rb"BT(.*?)ET", payload, re.S):
            line = _pdf_text_of_block(block)
            if line:
                chunks.append(line)
    text = "\n".join(chunks)
    if not text.strip():
        warnings.append("PDF 未提取到文本层（可能是扫描件/图片型 PDF）")
    return text, warnings


def _pdf_text_of_block(block: bytes) -> str:
    """抽取一个 BT…ET 文本块里的可见文字，按 Td/TD 的 y 变化粗略分行。"""
    pieces = []
    last_y = None
    for m in re.finditer(
            rb"(?P<nums>(?:[-\d.]+\s+){1,2}(?:Td|TD))"
            rb"|\((?P<lit>(?:\\.|[^\\()])*)\)"
            rb"|<(?P<hex>[0-9A-Fa-f\s]+)>", block):
        if m.group("nums"):
            nums = m.group("nums").split()
            if len(nums) >= 2:
                y = float(nums[-2])
                if last_y is not None and abs(y - last_y) > 0.5:
                    pieces.append("\n")
                last_y = y
            continue
        if m.group("lit") is not None:
            lit = m.group("lit")
            lit = re.sub(rb"\\([()\\])", rb"\1", lit)
            lit = (lit.replace(b"\\n", b"\n").replace(b"\\r", b"\r")
                      .replace(b"\\t", b"\t").replace(b"\\b", b"\b")
                      .replace(b"\\f", b"\f"))
            pieces.append(_decode_bytes(lit))
        else:
            hexs = re.sub(rb"\s", b"", m.group("hex"))
            if len(hexs) % 2 == 0 and len(hexs) >= 4:
                try:
                    pieces.append(bytes.fromhex(hexs.decode("ascii")).decode("utf-16-be", "ignore"))
                except ValueError:
                    pass
    return "".join(pieces).strip()


def extract_pdf(path) -> tuple[str, dict]:
    """提取 PDF 文本；优先 pypdf（可选依赖），否则内置提取器。"""
    meta = {"kind": "pdf", "extractor": None, "warnings": []}
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        PdfReader = None

    if PdfReader is not None:
        try:
            reader = PdfReader(str(path))
            parts = []
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception:
                    parts.append("")
            text = "\n".join(p for p in parts if p.strip())
            text, cut = _truncate(text)
            meta.update({"extractor": "pypdf", "pages": len(reader.pages)})
            if cut:
                meta["warnings"].append(cut)
            if text.strip():
                return text, meta
            # pypdf 取不到文本 → 回退内置提取器（可能是结构特殊但确有文本层的 PDF）
            meta["warnings"].append("pypdf 未取到文本，已回退内置提取器")
            meta["extractor"] = None
        except Exception as e:                       # noqa: BLE001
            meta["warnings"].append(f"pypdf 解析失败，已回退内置提取器：{e}")
            meta["extractor"] = None

    data = Path(path).read_bytes()
    text, warnings = _pdf_builtin(data)
    text, cut = _truncate(text)
    meta.update({"extractor": "builtin-pdf", "warnings": meta["warnings"] + warnings})
    if cut:
        meta["warnings"].append(cut)
    if not text.strip():
        meta["warnings"].append("PDF 未提取到文本层（可能是扫描件/图片型 PDF，需 OCR 或人工复核）")
    meta["pages"] = len(re.findall(rb"/Type\s*/Page[^s]", data))
    return text, meta


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------
def _xml_text(xml: str) -> str:
    """把 WordprocessingML 段落转成纯文本（逐段落一行）。"""
    lines = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        parts = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", para, re.S)
        if not parts:
            continue
        text = "".join(parts)
        text = (text.replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&quot;", '"').replace("&apos;", "'")
                    .replace("&amp;", "&"))
        text = re.sub(r"<[^>]+>", "", text)
        if text.strip():
            lines.append(text)
    return "\n".join(lines)


def extract_docx(path) -> tuple[str, dict]:
    """提取 .docx 文本（正文 + 页眉页脚）。"""
    meta = {"kind": "docx", "extractor": "docx-xml", "warnings": []}
    try:
        with zipfile.ZipFile(str(path)) as z:
            names = z.namelist()
            if "word/document.xml" not in names:
                raise IngestError("不是有效的 Word 文档（缺少 word/document.xml）")
            # 解压上限：docx 是 zip，高压缩比文件能把 30MB XML 在内存里炸开。
            # PDF 早就有 8MB 上限，docx 却没有（第三轮 F1）。
            def _read_capped(member):
                info = z.getinfo(member)
                if info.file_size > MAX_DOCX_XML_BYTES:
                    meta["warnings"].append(
                        f"{member} 解压后 {info.file_size // 1024 // 1024}MB，"
                        f"超出 {MAX_DOCX_XML_BYTES // 1024 // 1024}MB 上限，已截断读取")
                    with z.open(member) as fh:
                        return fh.read(MAX_DOCX_XML_BYTES).decode("utf-8", "replace")
                with z.open(member) as fh:
                    return fh.read().decode("utf-8", "replace")

            parts = [_xml_text(_read_capped("word/document.xml"))]
            for name in sorted(names):
                if re.match(r"word/(header|footer)\d*\.xml$", name):
                    parts.append(_xml_text(_read_capped(name)))
    except zipfile.BadZipFile:
        raise IngestError("Word 文件已损坏或不是 .docx 格式")
    text = "\n".join(p for p in parts if p.strip())
    text, cut = _truncate(text)
    if cut:
        meta["warnings"].append(cut)
    if not text.strip():
        meta["warnings"].append("Word 文档内容为空")
    return text, meta


# --------------------------------------------------------------------------
# 图片
# --------------------------------------------------------------------------
def image_info(path) -> dict:
    """读取图片基本信息（不解码像素、不做 OCR）。"""
    p = Path(path)
    ext = p.suffix.lower()
    size = p.stat().st_size
    width = height = None
    head = p.read_bytes()[:32]
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            width = int.from_bytes(head[16:20], "big")
            height = int.from_bytes(head[20:24], "big")
        elif head[:2] == b"\xff\xd8":                 # JPEG：扫 SOF 标记
            data = p.read_bytes()
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height = int.from_bytes(data[i + 5:i + 7], "big")
                    width = int.from_bytes(data[i + 7:i + 9], "big")
                    break
                seg = int.from_bytes(data[i + 2:i + 4], "big")
                i += 2 + max(seg, 2)
        elif head[:6] in (b"GIF87a", b"GIF89a"):
            width = int.from_bytes(head[6:8], "little")
            height = int.from_bytes(head[8:10], "little")
    except Exception:                                # noqa: BLE001
        pass
    return {
        "kind": "image",
        "extractor": "none",
        "mime": IMAGE_MIME.get(ext, "application/octet-stream"),
        "width": width,
        "height": height,
        "bytes": size,
        "vision_required": True,
        "warnings": [],
    }


# --------------------------------------------------------------------------
# 压缩包
# --------------------------------------------------------------------------
def expand_zip(path, dest: Path, depth: int = 0) -> list:
    """解压 zip 到 dest，返回 [(绝对路径, 相对 dest 的路径)]。

    防 zip slip（路径穿越）、压缩炸弹（总量/成员数上限）、嵌套过深。
    """
    if depth > MAX_ZIP_DEPTH:
        raise IngestError("压缩包嵌套层级过深")
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    total = 0
    with zipfile.ZipFile(str(path)) as z:
        infos = z.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise IngestError(f"压缩包成员过多（{len(infos)} > {MAX_ZIP_MEMBERS}）")
        for info in infos:
            if info.is_dir():
                continue
            if total + info.file_size > MAX_ZIP_TOTAL_BYTES:
                raise IngestError("压缩包解压后体积超出 50 MB 上限")
            total += info.file_size
            target = _safe_join(dest, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            rel = os.path.relpath(target, dest).replace("\\", "/")
            written.append((target, rel))
    return written


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
def ingest_file(path, work_dir=None, rel_name=None, depth=0) -> dict:
    """摄取单个文件，返回统一结构的结果。

    参数：
      path      : 实际文件路径
      work_dir  : 压缩包解包工作目录
      rel_name  : 输出时使用的相对名（压缩包成员用它保留目录层级）

    返回 dict：
      {
        "ok": bool,
        "kind": "text|pdf|docx|image|zip|...",
        "text": str,                 # 提取到的文本（image 可能为空）
        "meta": {...},               # 抽取元数据 + warnings
        "outputs": [                 # 派生出的待审对象
            {"name": "a.pdf.txt", "text": "...", "meta": {...}},
        ],
      }
    """
    path = Path(path)
    kind = sniff(path)
    label = rel_name or path.name
    stem = Path(label).stem
    meta = {"kind": kind, "source_name": path.name,
            "source_size": path.stat().st_size, "warnings": []}

    if kind == "text":
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise IngestError("仅支持 UTF-8 编码的文本文件")
        text = text.lstrip(chr(0xFEFF))
        text, cut = _truncate(text)
        meta.update({"extractor": "plain-text"})
        if cut:
            meta["warnings"].append(cut)
        return {"ok": True, "kind": kind, "text": text, "meta": meta,
                "outputs": [{"name": label, "text": text, "meta": meta}]}

    if kind == "pdf":
        text, pmeta = extract_pdf(path)
        meta.update(pmeta)
        out_name = f"{stem}.pdf.txt"
        return {"ok": True, "kind": kind, "text": text, "meta": meta,
                "outputs": ([{"name": out_name, "text": text, "meta": meta}]
                            if text.strip() else [])}

    if kind == "docx":
        text, dmeta = extract_docx(path)
        meta.update(dmeta)
        out_name = f"{stem}.docx.txt"
        return {"ok": True, "kind": kind, "text": text, "meta": meta,
                "outputs": ([{"name": out_name, "text": text, "meta": meta}]
                            if text.strip() else [])}

    if kind == "image":
        meta.update(image_info(path))
        # 尝试 OCR：把图片里的文字转成可审文本
        try:
            import ocr  # 同目录模块（tools/）
            text, ometa = ocr.ocr_image(path)
        except Exception as e:                                # noqa: BLE001
            meta["warnings"].append(
                f"图片未做内容审查（OCR 不可用：{e}），需人工复核或换用视觉模型")
            text = ""
        else:
            meta["ocr"] = ometa
            for w in ometa.get("warnings") or []:
                meta["warnings"].append(w)
            if text.strip():
                meta["warnings"].append(
                    f"图片文字经 {ometa.get('engine')} 引擎 OCR 转写后审查，"
                    "识别文本可能有识别误差，结论请以原图为准。")
        out_name = f"{stem}.image.txt"
        return {"ok": True, "kind": kind, "text": text, "meta": meta,
                "outputs": ([{"name": out_name, "text": text, "meta": meta}]
                            if text.strip() else [])}

    if kind == "legacy-doc":
        raise IngestError("暂不支持旧版 .doc/.rtf（OLE2 二进制格式），请另存为 .docx 后再上传")

    if kind == "zip":
        if work_dir is None:
            raise IngestError("压缩包需要一个解包工作目录")
        members = expand_zip(path, Path(work_dir) / path.stem, depth=depth)
        results = []
        for member, rel in members:
            try:
                sub = ingest_file(member, work_dir, rel_name=rel, depth=depth + 1)
            except IngestError as e:
                # 嵌套超限意味着"有内容没被展开"，不能当成功——否则用户以为整包都审了
                if "嵌套" in str(e):
                    raise IngestError(f"压缩包嵌套超过 {MAX_ZIP_DEPTH} 层（{rel}），已拒绝整包")
                meta["warnings"].append(f"成员 {rel} 跳过：{e}")
                continue
            for out in sub["outputs"]:
                out["name"] = f"{path.stem}/{out['name']}"
                out["meta"] = dict(out.get("meta") or {})
                out["meta"]["archive"] = path.name
            results.extend(sub["outputs"])
            meta["warnings"].extend(sub["meta"].get("warnings") or [])
        meta.update({"extractor": "zip", "members": len(members)})
        return {"ok": True, "kind": kind, "text": "", "meta": meta, "outputs": results}

    raise IngestError(f"不支持的文件类型：{path.suffix or '无扩展名'}")


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        print("usage: python tools/ingest.py <file> [workdir]")
        sys.exit(2)
    target = Path(sys.argv[1])
    work = Path(sys.argv[2]) if len(sys.argv) > 2 else target.parent
    try:
        result = ingest_file(target, work)
    except IngestError as e:
        print(f"✗ {e}")
        sys.exit(1)
    print(json.dumps({
        "ok": result["ok"], "kind": result["kind"],
        "meta": result["meta"],
        "outputs": [{"name": o["name"], "chars": len(o["text"])} for o in result["outputs"]],
    }, ensure_ascii=False, indent=2))
