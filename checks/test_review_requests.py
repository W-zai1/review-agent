"""同步/异步审查请求的参数归一必须只有一份实现。"""

import unittest
from pathlib import Path

from interfaces.web import review_requests


class ReviewRequestTests(unittest.TestCase):
    def setUp(self):
        self.key_calls = []

    def _assemble(self, data, *, normalize_flags=True, purpose_error=None):
        def resolve_target(file):
            if not file:
                return None, "缺少目标文件"
            return Path(file), None

        def resolve_agent(raw):
            return (raw or None), None

        def resolve_scenario(raw):
            return (raw or None), None

        def resolve_format_spec(raw):
            return (raw or None), None

        def resolve_format_custom(raw):
            return (raw or None), None

        def assemble_purpose(typed, files):
            if purpose_error:
                return "", purpose_error, None
            text = typed or ""
            return text, None, "quality" if text else None

        def make_key(**kwargs):
            self.key_calls.append(kwargs)
            return "key"

        file, target, error = review_requests.resolve_target(data, resolve_target)
        if error:
            return None, error
        return review_requests.assemble_request(
            data, file=file, target=target, target_types={"doc"},
            resolve_agent=resolve_agent, resolve_scenario=resolve_scenario,
            resolve_format_spec=resolve_format_spec,
            resolve_format_custom=resolve_format_custom,
            clean_purpose=lambda raw: str(raw or "").strip(),
            assemble_purpose=assemble_purpose,
            idempotency_key=make_key,
            normalize_flags=normalize_flags)

    def test_target_error_short_circuits(self):
        request, error = self._assemble({"file": ""})
        self.assertIsNone(request)
        self.assertEqual(error, "缺少目标文件")

    def test_start_normalizes_non_boolean_flags(self):
        request, error = self._assemble(
            {"file": "a.md", "llm": "yes", "research": "no", "type": "bad"})
        self.assertIsNone(error)
        self.assertIsNone(request.llm)
        self.assertIsNone(request.research)
        self.assertIsNone(request.type)

    def test_sync_keeps_raw_flags(self):
        request, error = self._assemble(
            {"file": "a.md", "llm": "yes"}, normalize_flags=False)
        self.assertIsNone(error)
        self.assertEqual(request.llm, "yes")

    def test_purpose_error_is_returned(self):
        request, error = self._assemble(
            {"file": "a.md"}, purpose_error="目的文件过多")
        self.assertIsNone(request)
        self.assertEqual(error, "目的文件过多")

    def test_custom_format_enters_key(self):
        request, error = self._assemble({
            "file": "a.md",
            "format_custom": {"check": {"line": 2, "bold": True}},
        })
        self.assertIsNone(error)
        self.assertEqual(request.spec_key, 'custom:{"bold": true, "line": 2}')
        self.assertEqual(self.key_calls[0]["spec"], request.spec_key)

    def test_runner_spec_carries_request(self):
        request, error = self._assemble({"file": " a.md ", "purpose": " 目的 "})
        self.assertIsNone(error)
        self.assertEqual(request.file, "a.md")
        spec = review_requests.build_runner_spec(
            request, job_id="job1", progress=Path("p.jsonl"))
        self.assertEqual(spec["job_id"], "job1")
        self.assertEqual(spec["file"], "a.md")
        self.assertEqual(spec["purpose"], "目的")
        self.assertEqual(spec["purpose_max"], 2)


if __name__ == "__main__":
    unittest.main()
