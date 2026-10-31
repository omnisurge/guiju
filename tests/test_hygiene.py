"""仓库约束 —— 把这些约定写成测试，而不是写在文档里等自觉。

覆盖三件"曾经出过事/容易出事"的事：
1. CI 的版本矩阵必须覆盖 pyproject 声明的最低版本（0.7.1 就是这么发现的）
2. 联网验收只能手动触发，不能挂在 push 上（不拿政府站点当压测对象）
3. 规范正文绝不入仓（体积 + 版权）
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class CiConstraintTest(unittest.TestCase):
    def test_matrix_covers_declared_minimum(self):
        """声明支持 3.10，就必须在 3.10 上真的跑过。"""
        requires = re.search(r'requires-python\s*=\s*">=\s*([0-9.]+)"', _read("pyproject.toml"))
        self.assertIsNotNone(requires, "pyproject 里找不到 requires-python")
        declared_min = requires.group(1)

        matrix = re.search(r"python:\s*\[([^\]]+)\]", _read(".github/workflows/ci.yml"))
        self.assertIsNotNone(matrix, "ci.yml 里找不到版本矩阵")
        versions = re.findall(r"[\"']([0-9]+\.[0-9]+)[\"']", matrix.group(1))
        self.assertIn(declared_min, versions, f"CI 矩阵 {versions} 未覆盖声明的下限 {declared_min}")

    def test_live_e2e_is_manual_only(self):
        """联网验收不得随 push 触发。"""
        text = _read(".github/workflows/e2e-live.yml")
        self.assertIn("workflow_dispatch", text)
        triggers = text.split("jobs:")[0]
        self.assertNotRegex(triggers, r"(?m)^\s+push:", "联网验收不能挂在 push 上")

    def test_local_ci_script_exists(self):
        self.assertTrue((ROOT / "scripts/ci.sh").is_file())

    def test_pre_push_hook_is_executable(self):
        hook = ROOT / ".githooks/pre-push"
        self.assertTrue(hook.is_file(), "缺少 pre-push 钩子")
        self.assertTrue(os.access(hook, os.X_OK), "pre-push 钩子没有可执行权限")


class IgnoreConstraintTest(unittest.TestCase):
    def test_standards_never_enter_repo(self):
        ignore = _read(".gitignore")
        for pattern in ("*.pdf", "library/", "*.db"):
            self.assertIn(pattern, ignore, f".gitignore 缺少 {pattern}")


class DocConstraintTest(unittest.TestCase):
    def test_version_single_source(self):
        """版本号只有一处来源，CHANGELOG 顶部必须同版本。"""
        version = re.search(r'__version__\s*=\s*"([0-9.]+)"', _read("guiju/_version.py")).group(1)
        changelog = _read("CHANGELOG.md")
        self.assertIn(f"## {version}", changelog, f"CHANGELOG 缺少 {version} 条目")


class CommandWiringTest(unittest.TestCase):
    """每个子命令都必须绑好 func —— 否则 main() 会在运行时才炸（sync 曾漏过一次）。"""

    def test_every_subcommand_binds_func(self):
        import argparse

        from guiju.cli import build_parser

        parser = build_parser()
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        self.assertTrue(subs, "没有找到子命令解析器")
        missing = [
            name
            for name, sub in subs[0].choices.items()
            if sub.get_default("func") is None
        ]
        self.assertEqual(missing, [], f"这些子命令没绑 func：{missing}")


if __name__ == "__main__":
    unittest.main()
