#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_legacy_freeze.py —— legacy 冻结为「只读回滚实现」（问题 1 / 第一阶段）。

冻结不是删除入口，而是三条可检查的边界：

    1. 默认链路仍是 v1（不选引擎 = v1），legacy 只能显式启用；
    2. 显式启用 legacy 时，stderr 必须说明「仅用于回滚与差异验证」；
    3. 提示不能进 stdout——stdout 上的 REVIEW_RESULT 行是服务端认领报告的唯一依据。

这里只测开关语义，不跑真实审查：跑 legacy 会写 outputs/ledger，属于运行数据，
不该由公开不变量测试触发。
"""
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402


class LegacyFreezeTests(unittest.TestCase):
    def test_default_engine_is_v1(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop(review.ENGINE_ENV, None)
            self.assertEqual(review.DEFAULT_ENGINE, "v1")
            self.assertEqual(review._take_engine(["t.md"])[0], "v1")

    def test_legacy_is_still_reachable(self):
        """冻结不是删除：回滚与差异验证仍要能显式选择它。"""
        self.assertIn("legacy", review.ENGINES)
        self.assertTrue(review.LEGACY_FROZEN)
        engine, rest = review._take_engine(["--engine", "legacy", "t.md"])
        self.assertEqual(engine, "legacy")
        self.assertEqual(rest, ["t.md"])

    def test_legacy_prints_freeze_notice_to_stderr(self):
        err = io.StringIO()
        with redirect_stderr(err):
            review._take_engine(["--engine", "legacy", "t.md"])
        self.assertIn("已冻结", err.getvalue())
        self.assertIn("仅用于回滚与差异验证", err.getvalue())

    def test_notice_never_pollutes_stdout(self):
        """stdout 只留给 REVIEW_RESULT；提示混进去会让服务端解析失败。"""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            review._take_engine(["--engine", "legacy", "t.md"])
        self.assertEqual(out.getvalue(), "")
        self.assertNotEqual(err.getvalue(), "")

    def test_legacy_via_env_also_warns(self):
        err = io.StringIO()
        with mock.patch.dict("os.environ", {review.ENGINE_ENV: "legacy"}), \
                redirect_stderr(err):
            self.assertEqual(review._take_engine(["t.md"])[0], "legacy")
        self.assertIn("已冻结", err.getvalue())

    def test_v1_and_shadow_do_not_warn(self):
        for value in ("v1", "shadow"):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                review._take_engine(["--engine", value, "t.md"])
            self.assertEqual(err.getvalue(), "", msg=value)
            self.assertEqual(out.getvalue(), "", msg=value)

    def test_unknown_engine_still_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            review._take_engine(["--engine", "v2", "t.md"])
        self.assertEqual(ctx.exception.code, 2)

    def test_run_one_is_marked_frozen(self):
        """源码层留痕：下一个人打开 run_one 应能立刻看到冻结边界。"""
        import inspect
        body = inspect.getsource(review.run_one)
        self.assertIn("FROZEN", body)
        self.assertIn("legacy", body)


if __name__ == "__main__":
    unittest.main()
