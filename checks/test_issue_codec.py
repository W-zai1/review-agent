#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_issue_codec.py —— issue dict ⇄ Finding 的可逆性验收。

阶段 1 的收口标准是「报告里的结论由领域对象回写」。这条标准只能靠**可逆性**证明：
拿旧报告里的每一条 issue，编成 Finding 再写回去，必须逐键相等。丢一个键、
把 null 变成 false、把 1 变成 1.0，都算失败——因为那会改报告哈希、也会让
历史报告无法并排比对。

历史全量用例只在本地跑（outputs/ 被 .gitignore 排除，CI 里没有）；
CI 跑的是 checks/fixtures/legacy_report_min.json 这份最小样本。

运行：
    python -m unittest discover -s checks -v
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_agent.adapters.issue_codec import (        # noqa: E402
    KNOWN_KEYS,
    CodecMismatch,
    decode_issue,
    encode_issue,
    export_issues,
)
from review_agent.domain import (                      # noqa: E402
    ConfidenceBasis,
    FindingOrigin,
    FindingStatus,
    Severity,
)

RUN_ID = "R-20261030-001-RUN01"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "legacy_report_min.json"
OUTPUTS = ROOT / "outputs"


def sample_issue(**overrides):
    """一份「当前版本产出的 issue」：键集合与顺序跟 Session.make_issue 一致。"""
    issue = {
        "issue_id": "ISS-0001",
        "dimension": "compliance",
        "severity": "major",
        "quote": "截止目前，项目已完成 20 个里程碑节点",
        "location": "第 5 句",
        "rule_id": "COMP-004",
        "rule_text": "披露具体数据须标注口径与来源",
        "evidence": "source_presence_check：含具体数字但无口径",
        "tool": "regex + checklist",
        "confidence": 0.75,
        "confidence_basis": "rule",
        "suggestion": "补充统计口径与来源。",
        "open_discovery": False,
    }
    issue.update(overrides)
    return issue


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def historical_reports():
    """历史报告（本地才有）；只认带 review_id 与字符串 verdict 的报告。"""
    if not OUTPUTS.is_dir():
        return []
    out = []
    for path in sorted(OUTPUTS.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "review_id" in data and isinstance(
                data.get("verdict"), str):
            out.append((path, data))
    return out


class FixtureRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.report = load_fixture()

    def test_issues_and_suppressed_round_trip(self):
        for key in ("issues", "suppressed"):
            raw = self.report.get(key) or []
            self.assertTrue(raw, f"样本里没有 {key}，这条用例就没有信息量")
            rebuilt = export_issues(raw, run_id=RUN_ID)
            self.assertEqual(rebuilt, [dict(x) for x in raw])

    def test_key_order_matches_legacy_make_issue(self):
        raw = self.report["issues"][0]
        rebuilt = export_issues([raw], run_id=RUN_ID)[0]
        self.assertEqual(list(rebuilt)[:len(KNOWN_KEYS)],
                         [k for k in KNOWN_KEYS if k in rebuilt])

    def test_domain_fields_are_populated(self):
        encoded = encode_issue(self.report["issues"][0], run_id=RUN_ID)
        finding = encoded.finding
        self.assertEqual(finding.run_id, RUN_ID)
        self.assertIsInstance(finding.severity, Severity)
        self.assertIsNotNone(finding.rule)
        self.assertEqual(finding.rule.rule_id, self.report["issues"][0]["rule_id"])


class TypeFidelityTests(unittest.TestCase):
    """回写必须保住标量类型：报告字节变了，哈希链就断了。"""

    def test_int_confidence_stays_int(self):
        raw = sample_issue(confidence=1)
        rebuilt = export_issues([raw], run_id=RUN_ID)[0]
        self.assertIs(type(rebuilt["confidence"]), int)

    def test_null_open_discovery_stays_null(self):
        raw = sample_issue(open_discovery=None)
        rebuilt = export_issues([raw], run_id=RUN_ID)[0]
        self.assertIsNone(rebuilt["open_discovery"])

    def test_absent_optional_keys_are_not_invented(self):
        raw = sample_issue()
        raw.pop("confidence_basis")
        raw.pop("open_discovery")
        rebuilt = export_issues([raw], run_id=RUN_ID)[0]
        self.assertNotIn("confidence_basis", rebuilt)
        self.assertNotIn("open_discovery", rebuilt)

    def test_extra_fields_survive(self):
        raw = sample_issue(fix={"kind": "replace"}, consequence="不改会误导",
                           aggregated_count=3, aggregated_ids=["ISS-0002"])
        rebuilt = export_issues([raw], run_id=RUN_ID)[0]
        self.assertEqual(rebuilt["fix"], {"kind": "replace"})
        self.assertEqual(rebuilt["consequence"], "不改会误导")
        self.assertEqual(rebuilt["aggregated_count"], 3)

    def test_location_keeps_string_form(self):
        raw = sample_issue(location="文末（最后一条之后）")
        rebuilt = decode_issue(encode_issue(raw, run_id=RUN_ID))
        self.assertEqual(rebuilt["location"], "文末（最后一条之后）")


class FindingSemanticsTests(unittest.TestCase):
    def test_low_confidence_major_needs_confirmation(self):
        finding = encode_issue(sample_issue(confidence=0.6), run_id=RUN_ID).finding
        self.assertIs(finding.status, FindingStatus.NEEDS_CONFIRMATION)

    def test_rule_basis_maps_to_rule_origin(self):
        finding = encode_issue(sample_issue(confidence_basis="rule"),
                               run_id=RUN_ID).finding
        self.assertIs(finding.origin, FindingOrigin.RULE)
        self.assertIs(finding.confidence_basis, ConfidenceBasis.RULE)

    def test_open_discovery_flag_maps_to_origin(self):
        finding = encode_issue(sample_issue(rule_id="OPEN-0001", open_discovery=True),
                               run_id=RUN_ID).finding
        self.assertIs(finding.origin, FindingOrigin.OPEN_DISCOVERY)
        self.assertEqual(finding.rule.rule_id, "OPEN-0001")


class StructuralRejectionTests(unittest.TestCase):
    """装不下的输入必须报错，不能悄悄降级成一条「看起来正常」的发现。"""

    def test_unknown_severity_is_rejected(self):
        with self.assertRaises(CodecMismatch) as ctx:
            encode_issue(sample_issue(severity="critical"), run_id=RUN_ID)
        self.assertEqual(ctx.exception.issue_id, "ISS-0001")

    def test_illegal_dimension_is_rejected(self):
        with self.assertRaises(CodecMismatch):
            encode_issue(sample_issue(dimension="合规"), run_id=RUN_ID)

    def test_illegal_issue_id_is_rejected(self):
        with self.assertRaises(CodecMismatch):
            encode_issue(sample_issue(issue_id="1"), run_id=RUN_ID)


@unittest.skipUnless(historical_reports(), "outputs/ 不存在（CI 无历史语料）")
class HistoricalCorpusTests(unittest.TestCase):
    def test_every_historical_issue_round_trips(self):
        total = 0
        for path, report in historical_reports():
            for key in ("issues", "suppressed"):
                raw = report.get(key) or []
                rebuilt = export_issues(raw, run_id=RUN_ID)
                self.assertEqual(rebuilt, [dict(x) for x in raw],
                                 f"{path.name}: {key} 回写不一致")
                total += len(raw)
        self.assertGreater(total, 0, "历史语料里一条 issue 都没有")


if __name__ == "__main__":
    unittest.main()
