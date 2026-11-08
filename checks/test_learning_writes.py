"""学习写入口：Token 顺序、错误文案与返回结构。"""

from __future__ import annotations

import unittest

from interfaces.web.learning_writes import LearningWrites


class FakeLearning:
    class LearningError(Exception):
        pass

    def __init__(self):
        self.calls = []

    def add_annotation(self, **kwargs):
        self.calls.append(("annotation", kwargs))
        return {"id": "A-1", **kwargs}

    def decide_candidate(self, candidate_id, decision, *, note, by):
        self.calls.append(("candidate", candidate_id, decision, note, by))
        return {"candidate_id": candidate_id, "decision": decision}


def service(*, token="", headers=None, learning=None, env=None):
    return LearningWrites(
        rule_learning=learning or FakeLearning(),
        token_env="REVIEW_AGENT_ANNOTATION_TOKEN",
        headers=headers or {},
        env={"REVIEW_AGENT_ANNOTATION_TOKEN": token} if env is None else env,
    )


class AnnotationTests(unittest.TestCase):
    def test_no_token_configured_allows_annotation(self):
        learning = FakeLearning()
        r = service(learning=learning).add_annotation({"kind": "hit", "note": "n"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["annotation"]["kind"], "hit")
        self.assertEqual(len(learning.calls), 1)

    def test_token_missing_rejects_before_writing(self):
        learning = FakeLearning()
        r = service(token="secret", learning=learning).add_annotation({"kind": "hit"})
        self.assertEqual(r, {"ok": False,
                             "error": "标注需要 X-Review-Token 头（服务端已启用标注鉴权）"})
        self.assertEqual(learning.calls, [])

    def test_token_mismatch_rejects(self):
        r = service(token="secret", headers={"X-Review-Token": "wrong"}).add_annotation({})
        self.assertFalse(r["ok"])
        self.assertIn("X-Review-Token", r["error"])

    def test_token_match_allows_annotation(self):
        r = service(token="secret",
                    headers={"X-Review-Token": "secret"}).add_annotation(
                        {"kind": "miss", "by": "alice"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["annotation"]["by"], "alice")

    def test_learning_error_is_mapped(self):
        class Broken(FakeLearning):
            def add_annotation(self, **kwargs):
                raise self.LearningError("kind 非法")

        r = service(learning=Broken()).add_annotation({"kind": "x"})
        self.assertEqual(r, {"ok": False, "error": "kind 非法"})


class CandidateTests(unittest.TestCase):
    def test_token_missing_rejects_before_writing(self):
        learning = FakeLearning()
        r = service(token="secret", learning=learning).decide_candidate(
            {"candidate_id": "C-1"})
        self.assertEqual(r, {"ok": False,
                             "error": "审核决定需要 X-Review-Token 头（服务端已启用鉴权）"})
        self.assertEqual(learning.calls, [])

    def test_token_match_merges_decision(self):
        r = service(token="secret",
                    headers={"X-Review-Token": "secret"}).decide_candidate(
                        {"candidate_id": "C-1", "decision": "approve", "by": "bob"})
        self.assertEqual(r, {"ok": True, "candidate_id": "C-1",
                             "decision": "approve"})


if __name__ == "__main__":
    unittest.main()
