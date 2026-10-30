#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_document_node.py —— Document Analyzer 节点的独立验收。

这个节点做两件确定性的事：读文本、判类型。两件都容易「看起来对」：
    读文本：编码猜错 = 整篇乱码但流程不报错；
    判类型：猜错 = 换掉整套判定标准（合同要件检查、任务书、披露规则跟着变）。

所以这里逐条钉住分类规则的边界，并用假 reader 覆盖读失败路径。
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.domain import STAGE_ORDER, ReviewProfile, Stage  # noqa: E402
from review_agent.runtime import NodeContext, RunState, advance  # noqa: E402
from review_agent.runtime.nodes import (                # noqa: E402
    DocumentAnalyzerNode,
    classify_target,
)

REVIEW_ID = "R-20261030-001"
RUN_ID = f"{REVIEW_ID}-RUN01"

CONTRACT_HEAD = "合同编号：A-001\n甲方：甲公司\n乙方：乙公司\n签署日期：2026-10-30"
MEETING = "周会纪要\n本周完成 3 个模块，下周继续联调。"
# 要件词只出现在正文 600 字之后：不能因此判成合同本体
BODY_ONLY = "会议纪要\n" + ("无关内容。" * 200) + "甲方、乙方、本合同、签署"


class ClassifyTests(unittest.TestCase):
    def test_extension_rules(self):
        self.assertEqual(classify_target(Path("a.docx"), "")["type"], "docx")
        self.assertEqual(classify_target(Path("a.py"), "")["type"], "code")
        self.assertEqual(classify_target(Path("a.weird"), "")["type"], "llm_response")

    def test_json_with_code_is_code(self):
        self.assertEqual(classify_target(Path("a.json"), '{"x": "def f()"}')["type"], "code")
        self.assertEqual(classify_target(Path("a.json"), '{"a": 1}')["type"], "llm_response")

    def test_contract_title_with_two_markers(self):
        info = classify_target(Path("a.md"), CONTRACT_HEAD)
        self.assertEqual(info["type"], "contract")
        self.assertEqual(info["confidence"], 0.92)

    def test_contract_title_with_few_markers(self):
        info = classify_target(Path("a.md"), "协议说明\n甲方：甲公司")
        self.assertEqual(info["type"], "contract")
        self.assertEqual(info["confidence"], 0.75)

    def test_markers_only_in_body_are_not_a_contract(self):
        info = classify_target(Path("a.md"), BODY_ONLY)
        self.assertEqual(info["type"], "llm_response")
        self.assertEqual(info["confidence"], 0.55)
        self.assertIn("提及", info["reason"])

    def test_plain_text(self):
        info = classify_target(Path("a.md"), MEETING)
        self.assertEqual(info["type"], "llm_response")
        self.assertEqual(info["confidence"], 0.85)

    def test_reason_key_order_is_stable(self):
        self.assertEqual(list(classify_target(Path("a.md"), MEETING)),
                         ["type", "confidence", "reason"])


def ingestion_state(stage=Stage.INGESTION):
    done = STAGE_ORDER[:STAGE_ORDER.index(stage)]
    return RunState(
        review_id=REVIEW_ID, run_id=RUN_ID, stage=stage,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
        completed_stages=done,
    )


def reader(text=CONTRACT_HEAD, encoding="utf-8", warning=None):
    def _read(path):
        return text, encoding, warning

    return _read


class DocumentNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_text_and_builds_meta(self):
        state = ingestion_state()
        result = await DocumentAnalyzerNode(reader()).run(
            NodeContext(state=state, config={"target_path": "targets/a.md"}))
        self.assertTrue(result.success)
        output = dict(result.output)
        self.assertEqual(output["target_text"], CONTRACT_HEAD)
        meta = output["target_meta"]
        self.assertEqual(meta["type"], "contract")
        self.assertEqual(meta["chars"], len(CONTRACT_HEAD))
        self.assertEqual(meta["encoding"], "utf-8")

    async def test_reader_warning_becomes_node_warning(self):
        state = ingestion_state()
        result = await DocumentAnalyzerNode(reader(warning="疑似乱码")).run(
            NodeContext(state=state, config={"target_path": "targets/a.md"}))
        self.assertTrue(result.success)
        self.assertEqual(result.warnings, ("疑似乱码",))

    async def test_missing_path_is_rejected(self):
        state = ingestion_state()
        result = await DocumentAnalyzerNode(reader()).run(NodeContext(state=state))
        self.assertFalse(result.success)
        self.assertIn("target_path", "；".join(result.errors))

    async def test_unreadable_target_fails_loudly(self):
        def boom(path):
            raise OSError("文件被占用")

        state = ingestion_state()
        result = await DocumentAnalyzerNode(boom).run(
            NodeContext(state=state, config={"target_path": "targets/a.md"}))
        self.assertFalse(result.success)
        self.assertIn("目标不可读", "；".join(result.errors))

    async def test_classifier_failure_is_rejected(self):
        def bad(path, text):
            raise ValueError("判不了")

        state = ingestion_state()
        result = await DocumentAnalyzerNode(reader(), classifier=bad).run(
            NodeContext(state=state, config={"target_path": "targets/a.md"}))
        self.assertFalse(result.success)

    async def test_wrong_stage_is_rejected(self):
        state = ingestion_state(stage=Stage.INTAKE)
        result = await DocumentAnalyzerNode(reader()).run(
            NodeContext(state=state, config={"target_path": "targets/a.md"}))
        self.assertFalse(result.success)

    async def test_advance_carries_text_and_meta(self):
        state = ingestion_state()
        result = await DocumentAnalyzerNode(reader()).run(
            NodeContext(state=state, config={"target_path": "targets/a.md"}))
        nxt = advance(state, Stage.INGESTION, result)
        self.assertIs(nxt.stage, Stage.PLANNING)
        self.assertIn("target_text", nxt.artifacts)
        self.assertIn("target_meta", nxt.artifacts)


class DelegationTests(unittest.TestCase):
    """旧流程的 detect_type_detail 必须与节点实现同一口径。"""

    def test_review_helper_matches_node(self):
        import review
        for path in ("a.md", "a.docx", "a.json", "a.py"):
            for text in (CONTRACT_HEAD, MEETING, BODY_ONLY, '{"code": "def f()"}'):
                self.assertEqual(review.detect_type_detail(Path(path), text),
                                 classify_target(Path(path), text))


if __name__ == "__main__":
    unittest.main()
