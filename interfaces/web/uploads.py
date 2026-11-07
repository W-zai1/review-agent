"""待审对象与「审查目的」文件的上传适配层。

Handler 只负责从请求里取查询参数和 Content-Length，真正的校验、落盘、
摄取与暂存记录都收在这里。读请求体的动作以回调传入：校验不通过时
Handler 不读 body，才能安全地关闭 keep-alive 连接。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable


ReadBody = Callable[[int], bytes]


def _unlink_quiet(path) -> bool:
    """尽力而为的删除：环境级拦截（只读挂载/安全策略）不应拖垮 HTTP 响应。"""
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except BaseException:                        # noqa: BLE001
        return False


class UploadService:
    """把上传协议与摄取细节从 HTTP Handler 里分开。

    所有路径、限制、摄取器和记录读取函数都由 server 注入，模块本身不 import
    server，避免反向依赖。
    """

    def __init__(
        self,
        *,
        root: Path,
        uploads: Path,
        purpose_dir: Path,
        purpose_originals: Path,
        purpose_work: Path,
        allowed,
        binary_exts,
        max_text_bytes: int,
        max_binary_bytes: int,
        purpose_file_max: int,
        purpose_stage_max: int,
        safe_upload_name,
        unique_upload_path,
        marker_safe_name,
        list_purpose_records,
        ingest,
    ):
        self._root = root
        self._uploads = uploads
        self._purpose_dir = purpose_dir
        self._purpose_originals = purpose_originals
        self._purpose_work = purpose_work
        self._allowed = allowed
        self._binary_exts = binary_exts
        self._max_text_bytes = max_text_bytes
        self._max_binary_bytes = max_binary_bytes
        self._purpose_file_max = purpose_file_max
        self._purpose_stage_max = purpose_stage_max
        self._safe_upload_name = safe_upload_name
        self._unique_upload_path = unique_upload_path
        self._marker_safe_name = marker_safe_name
        self._list_purpose_records = list_purpose_records
        self._ingest = ingest

    # ------------------------------------------------------------------
    # 公共校验：两种上传共用同一套名字 / 扩展名 / 大小口径
    # ------------------------------------------------------------------
    def _prepare(self, raw_name, content_length):
        """校验 body 之前的元数据。

        返回 (name, ext, length, is_binary, limit, error, close_connection)。
        所有 error 都发生在读 body 之前，因此调用方必须关闭连接。
        """
        name = self._safe_upload_name(raw_name)
        if not name:
            return None, "", 0, False, 0, "文件名无效", True
        ext = os.path.splitext(name)[1].lower()
        if ext not in self._allowed:
            return None, "", 0, False, 0, f"不支持的文件类型：{ext or '无扩展名'}", True
        try:
            length = int(content_length or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return None, "", 0, False, 0, "文件为空", True
        is_binary = ext in self._binary_exts
        limit = self._max_binary_bytes if is_binary else self._max_text_bytes
        if length > limit:
            return (None, "", 0, False, 0,
                    f"文件超过 {limit // (1024 * 1024)} MB 限制", True)
        return name, ext, length, is_binary, limit, "", False

    # ------------------------------------------------------------------
    # 待审对象上传
    # ------------------------------------------------------------------
    def upload_target(self, raw_name, content_length, read_body: ReadBody):
        """接收原始字节上传：文本直接落盘，二进制先归档原件再摄取。

        返回 (payload, close_connection)。close_connection 只在 body 尚未
        读取时可能为 True，Handler 据此避免 keep-alive 错位。
        """
        name, ext, length, is_binary, limit, error, close = self._prepare(
            raw_name, content_length)
        if error:
            return {"ok": False, "error": error}, close
        data = read_body(length)

        if not is_binary:
            # —— 文本路径：严格校验（UTF-8 / 拒 NUL / 剥 BOM）——
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return {"ok": False, "error": "仅支持 UTF-8 编码的文本文件"}, False
            if chr(0) in text:
                return {"ok": False, "error": "疑似二进制文件，已拒绝"}, False
            if text.startswith(chr(0xFEFF)):
                text = text[1:]
            payload = text.encode("utf-8")
            target = self._unique_upload_path(name, base=self._uploads)
            try:
                target.write_bytes(payload)
            except Exception as e:                # noqa: BLE001
                return {"ok": False, "error": f"保存失败：{e}"}, False
            return {
                "ok": True,
                "kind": "text",
                "file": {
                    "name": target.name,
                    "rel": target.relative_to(self._root).as_posix(),
                    "size": len(payload),
                },
            }, False

        # ---------- 二进制路径：归档原件 + 摄取成文本 ----------
        originals = self._uploads / "_originals"
        originals.mkdir(parents=True, exist_ok=True)
        original = self._unique_upload_path(name, base=originals)
        try:
            original.write_bytes(data)
        except Exception as e:                    # noqa: BLE001
            return {"ok": False, "error": f"保存失败：{e}"}, False

        work = self._uploads / "_work"
        try:
            result = self._ingest.ingest_file(original, work_dir=work)
        except self._ingest.IngestError as e:
            return {"ok": False, "error": str(e),
                    "original": original.relative_to(self._root).as_posix()}, False
        except Exception as e:                    # noqa: BLE001
            return {"ok": False, "error": f"文档解析失败：{e}",
                    "original": original.relative_to(self._root).as_posix()}, False
        finally:
            shutil.rmtree(work, ignore_errors=True)

        meta_dir = self._uploads / "_meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        extracted = []
        for out in result["outputs"]:
            # 压缩包成员可能带目录层级，压平后落盘，保证都能出现在待审列表里
            flat = re.sub(r"[\\/]+", "-", out["name"]).lstrip("-")
            target = self._unique_upload_path(flat, base=self._uploads)
            try:
                target.write_text(out["text"], encoding="utf-8")
            except Exception as e:                # noqa: BLE001
                return {"ok": False, "error": f"派生文本保存失败：{e}"}, False
            ometa = out.get("meta") or {}
            (meta_dir / f"{target.name}.json").write_text(
                json.dumps(ometa, ensure_ascii=False, indent=2), encoding="utf-8")
            extracted.append({
                "name": target.name,
                "rel": target.relative_to(self._root).as_posix(),
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
            "original": original.relative_to(self._root).as_posix(),
        }, False

    # ------------------------------------------------------------------
    # 「审查目的」上传：同一套协议，产物是目的文本而不是待审对象
    # ------------------------------------------------------------------
    def upload_purpose(self, raw_name, content_length, read_body: ReadBody):
        """接收目的说明文件 → 摄取成文本 → 暂存 state/purpose_uploads/。

        与待审对象上传的三点区别：
          1. 落点在 state/ 而不是 targets/——目的文件不是待审对象，
             混进 targets/ 会污染待审列表；
          2. 文本按 PURPOSE_FILE_MAX 截断（目的会进每个维度的 prompt）；
          3. 提取不到文本（扫描件/无 OCR 的图片）明确拒绝，
             不留一个空目的文件等着被静默忽略。
        """
        name, ext, length, is_binary, limit, error, close = self._prepare(
            raw_name, content_length)
        if error:
            return {"ok": False, "error": error}, close
        if len(self._list_purpose_records()) >= self._purpose_stage_max:
            return {
                "ok": False,
                "error": f"目的文件暂存已满（{self._purpose_stage_max} 个），"
                         "请先删除不用的目的文件",
            }, True
        data = read_body(length)

        warnings = []
        if self._marker_safe_name(name)[1]:
            # 名字里的【】《》（）是目的块定界符：进块时会被改写，先说一声，
            # 别等到报告里名字看着不一样才发现。
            warnings.append(
                "文件名含【】《》（）等定界符，写进目的块时会改写为 [ ] < > ( )，"
                "不影响内容解析")
        kind = "text"
        original = None
        if not is_binary:
            # —— 文本路径：严格校验（UTF-8 / 拒 NUL / 剥 BOM）——
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return {"ok": False, "error": "仅支持 UTF-8 编码的文本文件"}, False
            if chr(0) in text:
                return {"ok": False, "error": "疑似二进制文件，已拒绝"}, False
            if text.startswith(chr(0xFEFF)):
                text = text[1:]
        else:
            # ---------- 二进制路径：归档原件 + 摄取成文本 ----------
            original = self._unique_upload_path(name, base=self._purpose_originals)
            try:
                original.write_bytes(data)
            except Exception as e:                # noqa: BLE001
                return {"ok": False, "error": f"保存失败：{e}"}, False
            try:
                # 每次请求独立工作目录：并发上传时 rmtree 不会删掉别人正在解压的文件
                work = self._purpose_work / uuid.uuid4().hex[:8]
                result = self._ingest.ingest_file(original, work_dir=work)
            except self._ingest.IngestError as e:
                _unlink_quiet(original)
                return {"ok": False, "error": str(e)}, False
            except Exception as e:                # noqa: BLE001
                _unlink_quiet(original)
                return {"ok": False, "error": f"文档解析失败：{e}"}, False
            finally:
                shutil.rmtree(work, ignore_errors=True)
            kind = str(result.get("kind") or "binary")
            warnings.extend(str(w) for w in (result["meta"].get("warnings") or []))
            # 目的文件是"标准"，不是"待审对象"：OCR 认错一个字，改的是判定口径
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
            if is_binary and original is not None:
                _unlink_quiet(original)
            return {"ok": False, "error": "未能从文件中提取到文本"
                                            "（扫描件或图片需 OCR 能力），"
                                            "请换用文本版文件或先配置 OCR",
                    "kind": kind, "warnings": warnings}, False

        truncated = len(text) > self._purpose_file_max
        if truncated:
            text = text[:self._purpose_file_max]
            warnings.append(f"目的文件超过 {self._purpose_file_max} 字，"
                            f"已截断至前 {self._purpose_file_max} 字参与解析")

        pid = uuid.uuid4().hex[:12]
        record = {
            "id": pid,
            "name": name,
            "kind": kind,
            "chars": len(text),
            "truncated": truncated,
            "text": text,
            "warnings": warnings,
            "original": (original.relative_to(self._root).as_posix()
                         if original is not None else ""),
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        try:
            (self._purpose_dir / f"{pid}.json").write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8")
        except Exception as e:                    # noqa: BLE001
            if original is not None:
                _unlink_quiet(original)
            return {"ok": False, "error": f"暂存失败：{e}"}, False

        return {
            "ok": True,
            "id": pid,
            "name": name,
            "kind": kind,
            "chars": len(text),
            "truncated": truncated,
            "preview": text[:80],
            "warnings": warnings,
            "items": self._list_purpose_records(),
        }, False
