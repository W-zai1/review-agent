#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_integration_objects.py —— 阶段 5 集成验收：三类对象真跑一遍。

单元测试证明「每个零件是对的」，集成测试证明「拼起来能出结论」。这里用**真实**
`run_one`（旧链路，当前默认生产路径）跑三类对象：

    txt   纯文本：含一处确定性的加总矛盾，必须被算出来
    code  代码文件：`.py`，走代码类型判定
    docx  Word：现场用 zipfile 造一个只含 `word/document.xml` 的最小 docx

断言的是**能交付**的四件事：出报告、报告落盘、裁定在合法档位、问题能追溯到规则。
不 mock 任何被测对象；`agents/` 缺失时 skip（公开 clone 不含 Agent 配置），
不伪装成「跑过了」。
"""
import json
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402

HAS_AGENTS = (ROOT / "agents").is_dir()

TXT_TEXT = (
    "三季度经营情况说明。销售费用 360 万，管理费用 210 万，研发费用 480 万，"
    "三项加总 1080 万元。"
)
CODE_TEXT = (
    "import subprocess\n\n\n"
    "def run(cmd):\n"
    "    return subprocess.run(cmd, shell=True)\n"
)
DOCX_TEXT = "本合同的试用期为 9 个月，工资按 80% 发放。"


def _document_xml(text: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )


class _Workspace:
    """把产物目录重定向到临时目录，并按需造三类目标文件。"""

    NAMES = ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")

    def __init__(self):
        self._saved = {name: getattr(review, name) for name in self.NAMES}
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        for name in ("outputs", "logs", "ledger", "state"):
            (base / name).mkdir()
        review.OUTPUTS = base / "outputs"
        review.LOGS = base / "logs"
        review.LEDGER = base / "ledger"
        review.STATE = base / "state"
        review.GAP_LOG = base / "state" / "gap_log.jsonl"
        self.base = base
        # 目标必须落在仓库内：流水线要算 target 相对仓库根的路径
        self.target_dir = ROOT / "targets"
        self.target_dir.mkdir(exist_ok=True)
        self._made = []

    def make(self, suffix: str, content: str) -> Path:
        path = self.target_dir / f"_it_{uuid.uuid4().hex[:8]}{suffix}"
        if suffix == ".docx":
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("word/document.xml", _document_xml(content))
        else:
            path.write_text(content, encoding="utf-8")
        self._made.append(path)
        return path

    def close(self):
        for path in self._made:
            try:
                path.unlink()
            except OSError:
                pass
        for name, value in self._saved.items():
            setattr(review, name, value)
        self.tmp.cleanup()


@unittest.skipUnless(HAS_AGENTS, "agents/ 不在仓库（公开 clone 不含），跳过集成用例")
class ObjectTypeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.ws = _Workspace()
        self.addCleanup(self.ws.close)
        self.rules = review.load_rules()

    def run_object(self, path):
        return review.run_one(path, self.rules, use_llm=False)

    def test_txt_report_is_deliverable(self):
        report = self.run_object(self.ws.make(".txt", TXT_TEXT))

        self.assertIn(report["verdict"], {"pass", "warn", "fail"})
        self.assertEqual(report["review_mode"], "rule-only")
        self.assertTrue(report["review_id"].startswith("R-"))
        self.assertIsInstance(report["issues"], list)
        self.assertTrue(report["core_conclusion"])
        # 确定性加总矛盾必须被算出来（不是模型说的）
        arithmetic = [i for i in report["issues"]
                      if i["rule_id"] == "LOGIC-001"]
        self.assertTrue(arithmetic, "分项 360+210+480=1050 与「加总 1080」的矛盾必须命中")
        self.assertIn("1050", " ".join(i.get("evidence", "") for i in arithmetic))

    def test_txt_report_is_written_to_disk(self):
        report = self.run_object(self.ws.make(".txt", TXT_TEXT))
        path = self.ws.base / "outputs" / f"{report['review_id']}.json"
        self.assertTrue(path.exists(), "报告必须落盘，不能只打印")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["review_id"], report["review_id"])

    def test_code_object_is_recognized(self):
        report = self.run_object(self.ws.make(".py", CODE_TEXT))
        self.assertEqual(report["target_type"], "code")
        self.assertIn(report["verdict"], {"pass", "warn", "fail"})

    def test_docx_object_is_recognized_and_reviewed(self):
        report = self.run_object(self.ws.make(".docx", DOCX_TEXT))
        self.assertEqual(report["target_type"], "docx")
        self.assertIn(report["verdict"], {"pass", "warn", "fail"})
        self.assertTrue((report.get("target_text") or "").strip(),
                        "docx 必须提取到正文，否则「审了个空文档」也算通过")

    def test_three_objects_get_distinct_review_ids(self):
        ids = [self.run_object(self.ws.make(suffix, text))["review_id"]
               for suffix, text in ((".txt", TXT_TEXT), (".py", CODE_TEXT),
                                    (".docx", DOCX_TEXT))]
        self.assertEqual(len(set(ids)), 3, "三次审查不能拿到同一个 R-id")


if __name__ == "__main__":
    unittest.main()
