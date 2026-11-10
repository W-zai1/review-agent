#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_tool_inventory.py —— 工具能力分类清单的守门测试（阶段 3 第一刀）。

`docs/第三阶段-工具能力分类.md` 是 47 个 `tools/*.py` 的唯一分类事实源。
文档一旦和代码脱节，分类就会退化成「写完就没人看」的表格，所以这里把它钉住：

    · 覆盖：文档模块集合 == `tools/*.py` 集合，不多不少、不重复
    · 归类：只允许 12 个白名单类别 + 工程运维
    · 可达性：文档标「是」的模块，必须真的被 `review.py`/`server.py` import；
      标「否」的不得被 import；标「间接」的必须被某个 `tools/` 模块 import
    · 注册表：文档标注的工具名集合 == 实际注册表（含条件注册的 `research.web`）
    · 处置：只允许三种取值，不允许出现未定义处置

只读检查：不 import `tools/` 里的具体模块，除注册表外不触发任何审查逻辑。
"""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import review  # noqa: E402

DOC = ROOT / "docs" / "第三阶段-工具能力分类.md"
TOOLS_DIR = ROOT / "tools"

CATEGORIES = {
    "确定性检查器", "文本扫描器", "规则执行器", "模型适配器", "模型基础设施",
    "外部核验服务", "检索服务", "法条检索", "知识治理", "规则学习",
    "整改建议", "审查配置", "工程运维",
}
DISPOSITIONS = {"保留", "保留·兼容壳", "保留·运维"}
REACH_VALUES = {"是", "否", "间接"}

# 运行期条件注册：`review.py::_run_v1_core` 里按开关注册，不在 build_capability_registry() 内
CONDITIONAL_TOOLS = {"research.web"}

_CELL_TOOL_RE = re.compile(r"`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)`")


def _split_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def parse_rows() -> list[dict]:
    """解析 §3 主表。表宽固定 9 列，单元格内不允许出现 `|`。"""
    text = DOC.read_text(encoding="utf-8")
    if "## 3 逐模块清单" not in text:
        raise AssertionError(f"{DOC.name} 缺少 §3 主表")
    section = text.split("## 3 逐模块清单")[1].split("\n## ")[0]
    rows = []
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = _split_cells(line)
        if len(cells) != 9:
            continue
        if cells[0] in {"#", ""} or set(cells[0]) <= {"-", ":", " "}:
            continue
        if not cells[0].isdigit():
            continue
        rows.append({
            "module": cells[1].strip("`"),
            "duty": cells[2],
            "category": cells[3],
            "reach": cells[4],
            "tools_cell": cells[5],
            "dup": cells[6],
            "disposition": cells[7],
            "evidence": cells[8],
        })
    return rows


def _imported_names(path: Path) -> set[str]:
    """顶层 import 名（含 `from tools import x` 的 x，以及 `from tools import x` 形式）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "tools":
                names.update(alias.name for alias in node.names)
            else:
                names.add(node.module.split(".")[0])
    return names


def _tools_modules() -> set[str]:
    return {p.stem for p in TOOLS_DIR.glob("*.py")}


class ToolInventoryDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = parse_rows()
        cls.by_module = {r["module"]: r for r in cls.rows}
        cls.modules = _tools_modules()
        cls.review_imports = _imported_names(ROOT / "review.py")
        cls.server_imports = _imported_names(ROOT / "server.py")
        cls.tools_imports = {
            p.stem: _imported_names(p) & cls.modules
            for p in TOOLS_DIR.glob("*.py")
        }

    # ---- 覆盖 -------------------------------------------------------------
    def test_has_47_rows(self):
        self.assertEqual(len(self.rows), 47, f"文档应有 47 行，实际 {len(self.rows)} 行")

    def test_modules_unique(self):
        self.assertEqual(len(self.by_module), len(self.rows), "文档里有重复模块行")

    def test_modules_match_tools_dir(self):
        self.assertEqual(sorted(self.modules), sorted(self.by_module),
                         "文档模块集合与 tools/*.py 不一致：新增/删除模块后要同步本表")

    # ---- 归类与处置 -------------------------------------------------------
    def test_categories_in_whitelist(self):
        bad = {r["module"]: r["category"] for r in self.rows if r["category"] not in CATEGORIES}
        self.assertEqual(bad, {}, f"出现白名单外的归类：{bad}")

    def test_dispositions_defined(self):
        bad = {r["module"]: r["disposition"] for r in self.rows
               if r["disposition"] not in DISPOSITIONS}
        self.assertEqual(bad, {}, f"未定义的处置：{bad}")

    def test_duty_and_evidence_not_empty(self):
        empty = [r["module"] for r in self.rows if not r["duty"] or not r["evidence"]]
        self.assertEqual(empty, [], f"主职责/证据为空：{empty}")

    # ---- 可达性 -----------------------------------------------------------
    def test_reach_values_legal(self):
        bad = {r["module"]: r["reach"] for r in self.rows if r["reach"] not in REACH_VALUES}
        self.assertEqual(bad, {}, f"可达性取值非法：{bad}")

    def test_reach_matches_actual_imports(self):
        direct = self.review_imports | self.server_imports
        for module, row in sorted(self.by_module.items()):
            with self.subTest(module=module):
                reach = row["reach"]
                if reach == "是":
                    self.assertIn(module, direct,
                                  f"{module} 标「是」但 review.py/server.py 未 import")
                elif reach == "否":
                    self.assertNotIn(module, direct,
                                     f"{module} 标「否」但被 review.py/server.py import")
                else:  # 间接
                    self.assertNotIn(module, direct, f"{module} 标「间接」但被入口直接 import")
                    importers = [m for m, imps in self.tools_imports.items() if module in imps]
                    self.assertTrue(importers, f"{module} 标「间接」但没有任何 tools/ 模块引用它")

    # ---- 注册表 -----------------------------------------------------------
    def test_registry_column_matches_actual_registry(self):
        claimed = set()
        for row in self.rows:
            claimed.update(_CELL_TOOL_RE.findall(row["tools_cell"]))
        actual = set(review.build_capability_registry().names()) | CONDITIONAL_TOOLS
        self.assertEqual(sorted(claimed), sorted(actual),
                         "文档标注的工具集合与 build_capability_registry() 不一致")

    def test_conditional_tool_marked(self):
        row = self.by_module.get("research")
        self.assertIsNotNone(row, "缺少 research 模块行")
        self.assertIn("research.web", row["tools_cell"])
        self.assertIn("条件注册", row["tools_cell"], "research.web 必须标注为条件注册")

    def test_registered_modules_are_reachable(self):
        """标了注册表工具的行，模块必须是运行期可达（是/间接）。"""
        for row in self.rows:
            if "—" == row["tools_cell"].strip():
                continue
            with self.subTest(module=row["module"]):
                self.assertIn(row["reach"], {"是", "间接"},
                              f"{row['module']} 注册了工具却标为不可达")


if __name__ == "__main__":
    unittest.main()
