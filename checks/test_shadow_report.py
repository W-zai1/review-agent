#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_shadow_report.py —— 影子报告：v1 能自己说清的那部分结论。

这份报告存在的意义是「不冒充正式报告」：
    * 只写新流水线真的算过的东西（裁定 / 发现 / 证据 / 争议 / 覆盖率 / 节点审计）
    * `unsupported_blocks` 显式列出旧报告有、这份没有的块
    * 落在 `outputs/v1/`，不占正式报告命名空间

所以这里重点测三件事：**没算的块必须显式缺席**、**丢弃的发现不能混进来**、
**审计信息（节点事件 / 检查点）必须随报告走**。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.application import (                 # noqa: E402
    UNSUPPORTED_BLOCKS,
    build_shadow_report,
    render_shadow_md,
)
from review_agent.domain import (                       # noqa: E402
    STAGE_ORDER,
    Decision,
    Evidence,
    Finding,
    FindingOrigin,
    Location,
    ReviewProfile,
    RuleRef,
    Severity,
    Stage,
    Verdict,
)
from review_agent.runtime import RunState  # noqa: E402

RUN_ID = "R-20261030-001-RUN01"

# CLI 端到端用例要跑真实审查，依赖本地私有资产（targets/ 样本 + agents/ Agent 定义）；
# 公开 clone 不含这两者（见 .gitignore），缺失时跳过本用例，本地带资产时照常运行。
PRIVATE_OK = (ROOT / "targets" / "contract_001.md").exists() and (ROOT / "agents").is_dir()


def finding(seq, *, rule_id="FACT-001", quote="原文一句话", severity=Severity.MINOR,
            origin=FindingOrigin.RULE):
    return Finding(
        id=f"ISS-{seq:04d}", run_id=RUN_ID, dimension="fact",
        severity=severity, quote=quote, location=Location("第 1 句", 1),
        confidence=0.8, origin=origin,
        rule=RuleRef(rule_id=rule_id, dimension="fact"),
    )


def state(*, findings=(), evidence=(), artifacts=None, decision=True):
    return RunState(
        review_id="R-20261030-001", run_id=RUN_ID, stage=Stage.FINALIZE,
        profile=ReviewProfile(id="doc-review", name="文档审查", version="1"),
        completed_stages=STAGE_ORDER,
        findings=tuple(findings), evidence=tuple(evidence),
        decision=(Decision(id="DEC-0001", run_id=RUN_ID, verdict=Verdict.WARN,
                           rationale="存在 1 条 major") if decision else None),
        artifacts=artifacts or {},
    )


class BuildTests(unittest.TestCase):
    def test_shape_and_identity(self):
        report = build_shadow_report(state(findings=[finding(1)]))
        self.assertEqual(report["engine"], "architecture-v1")
        self.assertEqual(report["schema_version"], "1.0-v1-shadow")
        self.assertEqual(report["review_id"], "R-20261030-001")
        self.assertEqual(report["verdict"], "warn")
        self.assertEqual(report["stats"]["minor"], 1)
        self.assertEqual(report["by_origin"]["rule"], 1)

    def test_unsupported_blocks_are_listed(self):
        report = build_shadow_report(state())
        blocks = [item["block"] for item in report["unsupported_blocks"]]
        self.assertEqual(blocks, [key for key, _ in UNSUPPORTED_BLOCKS])
        self.assertIn("law_library", blocks)
        self.assertIn("research", blocks)
        # 哈希链、整改单、目的比对都已接入，不再算「未支持」
        for done in ("integrity.chain", "action_plan", "purpose"):
            self.assertNotIn(done, blocks)

    def test_limitations_mention_missing_logs_hash(self):
        report = build_shadow_report(state())
        self.assertTrue(any("logs_hash" in item for item in report["limitations"]))

    def test_limitations_include_unsupported_and_warnings(self):
        base = state()
        warned = RunState(
            review_id=base.review_id, run_id=base.run_id, stage=base.stage,
            profile=base.profile, completed_stages=base.completed_stages,
            warnings=("节点告警一条",))
        report = build_shadow_report(warned)
        self.assertTrue(any("law_library" in item for item in report["limitations"]))
        self.assertTrue(any("节点告警一条" in item for item in report["limitations"]))

    def test_dropped_findings_do_not_appear(self):
        keep = finding(1)
        drop = finding(2, quote="这句回查不到")
        report = build_shadow_report(state(
            findings=[keep, drop],
            artifacts={"target_text": "原文一句话",
                       "evidence_dropped": [{"finding_id": "ISS-0002"}]}))
        self.assertEqual([i["issue_id"] for i in report["issues"]], ["ISS-0001"])

    def test_evidence_rows_are_joined_to_issues(self):
        ev = Evidence(id="EV-0001", run_id=RUN_ID, quote="原文一句话",
                      location=Location("第 1 句", 1), finding_ids=("ISS-0001",))
        report = build_shadow_report(state(findings=[finding(1)], evidence=[ev]))
        self.assertEqual(report["evidence"][0]["issue_id"], "ISS-0001")
        self.assertEqual(report["evidence"][0]["evidence_id"], "EV-0001")

    def test_audit_carries_events_and_checkpoints(self):
        class StubEvent:
            def as_dict(self):
                return {"stage": "analysis", "node": "rule-analyzer", "ok": True}

        class StubCheckpoint:
            stage = Stage.ANALYSIS
            sequence = 3
            state_hash = "a" * 64

        report = build_shadow_report(state(), events=[StubEvent()],
                                     checkpoints=[StubCheckpoint()])
        self.assertEqual(report["audit"]["events"][0]["node"], "rule-analyzer")
        self.assertEqual(report["audit"]["checkpoints"][0]["stage"], "analysis")
        self.assertEqual(report["audit"]["checkpoints"][0]["sequence"], 3)

    def test_target_meta_is_used_when_present(self):
        report = build_shadow_report(state(artifacts={
            "target_meta": {"path": "targets/a.md", "type": "contract",
                            "chars": 42, "encoding": "utf-8"},
            "target_hash": "b" * 64}))
        self.assertEqual(report["target"]["type"], "contract")
        self.assertEqual(report["target"]["chars"], 42)
        self.assertEqual(report["target"]["hash"], "b" * 64)


class RenderTests(unittest.TestCase):
    def test_markdown_states_verdict_and_boundaries(self):
        report = build_shadow_report(state(
            findings=[finding(1, severity=Severity.MAJOR)]))
        text = render_shadow_md(report)
        self.assertIn("warn", text)
        self.assertIn("边界（这份报告没有做什么）", text)
        self.assertIn("law_library", text)
        self.assertIn("ISS-0001", text)

    def test_markdown_flags_empty_result_honestly(self):
        text = render_shadow_md(build_shadow_report(state()))
        self.assertIn("未发现问题", text)
        self.assertIn("未覆盖项见上方边界", text)

    def test_markdown_renders_action_plan_when_present(self):
        report = build_shadow_report(state(findings=[finding(1)]))
        report["action_plan"] = {
            "total": 1, "concrete": 1, "manual": 0,
            "headline": "1 条可直接改，0 条需人工改写",
            "top": [{"issue_id": "ISS-0001", "rule_id": "FACT-001",
                     "severity": "minor", "summary": "补口径",
                     "how": "在数字后补统计口径", "consequence": "读者无法核对"}],
        }
        text = render_shadow_md(report)
        self.assertIn("整改建议", text)
        self.assertIn("在数字后补统计口径", text)
        self.assertIn("读者无法核对", text)

    def test_action_plan_is_null_until_attached(self):
        self.assertIsNone(build_shadow_report(state()).get("action_plan"))

    def test_purpose_is_null_until_attached(self):
        self.assertIsNone(build_shadow_report(state()).get("purpose"))

    def test_markdown_renders_purpose_goals(self):
        report = build_shadow_report(state())
        report["purpose"] = {
            "text": "给客户的合同",
            "judge": {"mode": "rule"},
            "goals": [
                {"id": "required_clause", "question": "要件是否齐全？",
                 "status": "found", "note": ""},
                {"id": "liability", "question": "违约责任是否对等？",
                 "status": "not_found", "note": ""},
            ],
        }
        text = render_shadow_md(report)
        self.assertIn("审查目的比对", text)
        self.assertIn("要件是否齐全？", text)
        self.assertIn("未找到证据", text)
        self.assertIn("未找到证据 ≠ 不存在", text)


class EngineShadowReportTests(unittest.TestCase):
    """v1 在 CLI 上必须真的写出 `outputs/v1/` 两份文件。"""

    @unittest.skipUnless(PRIVATE_OK,
                         "私有审查资产缺失（targets/ 样本或 agents/ 定义不在公开仓库），"
                         "跳过 CLI 影子报告端到端用例")
    def test_v1_writes_shadow_report(self):
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        import review
        saved = {name: getattr(review, name)
                 for name in ("OUTPUTS", "LOGS", "LEDGER", "STATE", "GAP_LOG")}
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name in ("outputs", "logs", "ledger", "state"):
                (base / name).mkdir()
            review.OUTPUTS = base / "outputs"
            review.LOGS = base / "logs"
            review.LEDGER = base / "ledger"
            review.STATE = base / "state"
            review.GAP_LOG = base / "state" / "gap_log.jsonl"
            try:
                out = io.StringIO()
                with mock.patch.object(sys, "argv", [
                        "review.py", "targets/contract_001.md",
                        "--no-llm", "--engine", "v1",
                        "--purpose", "这是给客户的合同，找出对我方不利的条款",
                ]), redirect_stdout(out):
                    review.main()
                files = sorted((base / "outputs" / "v1").iterdir())
                names = [p.name for p in files]
                self.assertEqual(len([n for n in names if n.endswith(".json")]), 1)
                self.assertEqual(len([n for n in names if n.endswith(".md")]), 1)
                payload = json.loads(
                    (base / "outputs" / "v1").glob("*.json").__next__()
                    .read_text(encoding="utf-8"))
                self.assertEqual(payload["engine"], "architecture-v1")
                self.assertIn("action_plan", payload)
                self.assertGreaterEqual((payload.get("action_plan") or {}).get("total", 0), 1,
                                        "整改单必须进影子报告")
                self.assertTrue(any(i.get("fix") for i in payload["issues"]),
                                "每条发现应带 fix（改哪句/怎么改）")
                purpose = payload.get("purpose") or {}
                self.assertTrue(purpose.get("goals"), "给了 --purpose 就必须有目的比对块")
                self.assertTrue(purpose.get("evidence"), "目的比对应带原文证据")
                self.assertNotIn("purpose",
                                 [b["block"] for b in payload["unsupported_blocks"]])
                self.assertEqual(self._reports(base), [],
                                 "v1 不应写正式报告")
            finally:
                for name, value in saved.items():
                    setattr(review, name, value)

    @staticmethod
    def _reports(base):
        return sorted((base / "outputs").glob("R-*.json"))


if __name__ == "__main__":
    unittest.main()
