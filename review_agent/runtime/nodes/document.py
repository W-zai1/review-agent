# -*- coding: utf-8 -*-
"""文档分析节点（Document Analyzer）。

职责：把「一个文件」变成「可审的文本 + 判定元数据」。

    路径 ──reader──▶ (text, encoding, warning)
          ──classifier──▶ {type, confidence, reason}
          ──────────────▶ target_text / target_meta

读文件与判类型都是确定性的，所以这个节点**不碰 LLM**，可以用假 reader 全量测试。

为什么类型判定要独立成可测函数：
    旧实现只要全文任意位置出现「合同/协议/甲方」任一关键词就判 contract——
    会议纪要里一句「验收标准与合同附件一致」就能让整篇变成合同，于是
    ①合同要件检查报 3 条无意义缺失 ②审查任务书变成「合同/协议文本」
    ③数据披露检查被关掉。**一次猜测改了整个判定标准**。
    现在的规则是「标题与开头加权，正文里提到合同不算合同」，
    这些边界必须能被逐条钉住，而不是靠上线后从报告里反推。

端口（为什么不全在这里读文件）：
    `reader` / `classifier` 由调用方注入。运行时不 import 旧代码（架构约束），
    旧流程通过 `review.py` 的同名包装函数调用这里的实现，两边口径一致。
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from ...domain import Stage
from ..node import NodeContext, NodeResult

NAME: Final = "document-analyzer"

# 合同「要件词」：出现在开头才算合同本体，只出现在正文多半是「提到合同」
CONTRACT_MARKERS: Final[tuple[str, ...]] = (
    "甲方", "乙方", "本合同", "本协议", "签署", "盖章", "生效",
    "有效期", "违约责任", "争议解决", "仲裁", "诉讼", "附件",
)

CODE_EXTS: Final[tuple[str, ...]] = (
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".java", ".cpp", ".c",
)
TEXT_EXTS: Final[tuple[str, ...]] = (
    ".md", ".txt", ".markdown", ".csv", ".log", ".xml", "",
)
_TITLE_CONTRACT_RE: Final = re.compile(r"(合同|协议|契约)")
_JSON_CODE_RE: Final = re.compile(r"(def |function |import |=>)")


def classify_target(path: Path, text: str) -> dict[str, Any]:
    """类型判定 + 置信度 + 依据（纯函数，输入只有扩展名与正文）。"""
    path = Path(path)
    text = text or ""
    ext = path.suffix.lower()
    if ext in (".docx", ".docm"):
        return {"type": "docx", "confidence": 0.9, "reason": f"Word 文档扩展名 {ext}"}
    if ext in CODE_EXTS:
        return {"type": "code", "confidence": 0.9, "reason": f"源码扩展名 {ext}"}
    if ext == ".json":
        if _JSON_CODE_RE.search(text):
            return {"type": "code", "confidence": 0.8, "reason": "JSON 内含代码特征"}
        return {"type": "llm_response", "confidence": 0.7,
                "reason": "JSON 数据文件"}
    if ext not in TEXT_EXTS:
        return {"type": "llm_response", "confidence": 0.6,
                "reason": f"未识别扩展名 {ext}"}

    head = text[:600]
    first_line = next((ln for ln in text.strip().splitlines() if ln.strip()), "")
    title_contract = bool(_TITLE_CONTRACT_RE.search(first_line))
    head_hits = [k for k in CONTRACT_MARKERS if k in head]
    all_hits = [k for k in CONTRACT_MARKERS if k in text]

    if (title_contract and len(head_hits) >= 2) or len(head_hits) >= 4:
        return {"type": "contract", "confidence": 0.92,
                "reason": f"标题/开头出现合同要件词：{'、'.join(head_hits[:6])}"}
    if title_contract and len(head_hits) >= 1:
        return {"type": "contract", "confidence": 0.75,
                "reason": f"标题含合同字样，开头要件词较少：{'、'.join(head_hits[:4])}"}
    if len(all_hits) >= 4:
        return {"type": "llm_response", "confidence": 0.55,
                "reason": (f"全文出现 {len(all_hits)} 个合同/协议字样，但集中在正文而非开头"
                           "——判为普通文本（疑似「提及」合同，不是合同本体）")}
    return {"type": "llm_response", "confidence": 0.85,
            "reason": "未出现合同要件特征"}


# (路径) -> (正文, 编码, 告警)
Reader = Callable[[Path], tuple[str, str, str | None]]
# (路径, 正文) -> {type, confidence, reason}
Classifier = Callable[[Path, str], Mapping[str, Any]]


class DocumentAnalyzerNode:
    """ingestion 阶段节点：读文本、判类型、记元数据。"""

    name: Final = NAME
    stage: Final = Stage.INGESTION
    produces: Final = frozenset({"target_text", "target_meta"})

    def __init__(self, reader: Reader,
                 classifier: Classifier = classify_target) -> None:
        self.reader = reader
        self.classifier = classifier

    async def run(self, ctx: NodeContext) -> NodeResult:
        state = ctx.state
        if state.stage is not Stage.INGESTION:
            return NodeResult.fail(
                f"{NAME} 只处理 ingestion 阶段，当前状态在 {state.stage.value}")
        raw_path = ctx.config.get("target_path")
        if not raw_path:
            return NodeResult.fail("ingestion 阶段缺少 config['target_path']")
        path = Path(str(raw_path))

        try:
            text, encoding, warning = self.reader(path)
        except Exception as e:                          # noqa: BLE001 - 边界统一转失败
            # 读不出就是读不出：不许产出「空文档 → 没问题」的报告
            return NodeResult.fail(
                f"目标不可读：{path.name}: {type(e).__name__}: {e}")

        try:
            info = dict(self.classifier(path, text) or {})
        except Exception as e:                          # noqa: BLE001
            return NodeResult.fail(
                f"类型判定失败：{type(e).__name__}: {e}")

        meta = {
            "path": path.as_posix(),
            "chars": len(text),
            "encoding": encoding,
            "type": info.get("type", "unknown"),
            "type_confidence": info.get("confidence"),
            "type_reason": info.get("reason", ""),
        }
        warnings: tuple[str, ...] = ()
        if warning:
            warnings = (str(warning),)
        return NodeResult.ok(
            output={"target_text": text, "target_meta": meta},
            warnings=warnings,
        )
