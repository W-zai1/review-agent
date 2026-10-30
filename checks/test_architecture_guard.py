#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checks/test_architecture_guard.py —— 依赖方向的可执行约束。

架构文档里的「六层依赖规则」如果只写在 markdown 里，三个月后一定失效。
这里用 AST 扫 review_agent/ 的 import，把三条硬规则变成测试：

    domain         不得依赖 runtime / capabilities / 第三方库 / 旧代码
    capabilities   不得依赖 runtime / 旧代码
    runtime        不得依赖 capabilities / 旧代码

只查 import 方向，不查调用关系（动态 import 不在覆盖范围；真需要时再补）。
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "review_agent"

FORBIDDEN = {
    "domain": ("review_agent.runtime", "review_agent.capabilities",
               "review_agent.application", "review_agent.infrastructure",
               "review_agent.engine", "review_agent.profiles",
               "review_agent.adapters",
               "review", "server", "tools", "agent_spec"),
    "capabilities": ("review_agent.runtime", "review_agent.application",
                     "review_agent.infrastructure", "review_agent.engine",
                     "review", "server", "tools"),
    "runtime": ("review_agent.capabilities", "review_agent.application",
                "review_agent.engine", "review", "server", "tools"),
}

STDLIB = set(sys.stdlib_module_names)


def package_of(path: Path) -> str:
    """文件所属的 review_agent 子包（domain / capabilities / runtime / ''）。"""
    rel = path.relative_to(ROOT).parts
    if len(rel) >= 3 and rel[0] == "review_agent":
        return rel[1]
    return ""


def absolute_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            out.append((node.lineno, node.module))
            for alias in node.names:
                out.append((node.lineno, f"{node.module}.{alias.name}"))
    return out


class DependencyDirectionTests(unittest.TestCase):
    def test_package_files_exist(self):
        files = sorted(PKG.rglob("*.py"))
        self.assertGreater(len(files), 5, "review_agent/ 不存在或为空")

    def test_no_forbidden_imports(self):
        problems = []
        for path in sorted(PKG.rglob("*.py")):
            pkg = package_of(path)
            rules = FORBIDDEN.get(pkg, ())
            for lineno, module in absolute_imports(path):
                for bad in rules:
                    if module == bad or module.startswith(bad + "."):
                        problems.append(
                            f"{path.relative_to(ROOT).as_posix()}:{lineno} "
                            f"import {module}（{pkg} 层禁止依赖 {bad}）")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_domain_has_no_third_party_dependency(self):
        problems = []
        for path in sorted((PKG / "domain").rglob("*.py")):
            for lineno, module in absolute_imports(path):
                first = module.split(".")[0]
                if first == "review_agent" or first in STDLIB:
                    continue
                problems.append(
                    f"{path.relative_to(ROOT).as_posix()}:{lineno} import {module}")
        self.assertEqual(
            problems, [],
            "领域层只允许标准库与自身包（见 ADR-001）：\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
