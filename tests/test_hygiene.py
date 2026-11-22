"""仓库约束 —— 把这些约定写成测试，而不是写在文档里等自觉。

覆盖"曾经出过事/容易出事"的事：
1. CI 的版本矩阵必须覆盖 pyproject 声明的最低版本（0.7.1 就是这么发现的）
2. 联网验收只能手动触发，不能挂在 push 上（不拿政府站点当压测对象）
3. 规范正文绝不入仓（体积 + 版权）
4. 发版文档的版本/计数/平台声明与 _version.py、CHANGELOG 同源
5. check-release 的 --docs-only 进默认 CI，--artifacts 只走发版 workflow
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _current_version() -> str:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', _read("guiju/_version.py"))
    if not match:
        raise AssertionError("guiju/_version.py 里找不到 __version__")
    return match.group(1)


def _pretty_version(version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    if not match:
        raise AssertionError(f"无法解析版本号：{version}")
    base, pre, number = match.groups()
    if not pre:
        return base
    labels = {"a": "alpha", "b": "beta", "rc": "rc"}
    return f"{base}-{labels[pre]}.{number}"


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

    def test_c0_gate_honors_python_override(self):
        baseline = _read("scripts/ci-c0-baseline.sh")
        self.assertIn('PY="${PYTHON:-python3}"', baseline)
        self.assertIn('"$PY" -m unittest -v tests.char_baseline_20261121', baseline)
        self.assertNotRegex(baseline, r"(?m)^python3 -m unittest", "C0 gate 不得硬编码 python3")
        ci = _read("scripts/ci.sh")
        self.assertGreaterEqual(ci.count('PYTHON="$PY" scripts/ci-c0-baseline.sh'), 2)


class IgnoreConstraintTest(unittest.TestCase):
    def test_standards_never_enter_repo(self):
        ignore = _read(".gitignore")
        for pattern in ("*.pdf", "library/", "*.db"):
            self.assertIn(pattern, ignore, f".gitignore 缺少 {pattern}")


class DocConstraintTest(unittest.TestCase):
    def test_version_single_source(self):
        """版本号只有一处来源；CHANGELOG 最新版本条目必须与它一致。

        不能只检查“任意旧版本出现过”：1.1.0 时仍保留旧 ## 1.1.0 会掩盖
        候选版本未 bump 的漂移（发版评审 2026-11-20 发现）。
        """
        version = re.search(r'__version__\s*=\s*"([^"]+)"', _read("guiju/_version.py")).group(1)
        changelog = _read("CHANGELOG.md")
        headings = re.findall(r"^##\s+(\S+)", changelog, flags=re.MULTILINE)
        released = [heading for heading in headings if heading != "Unreleased"]
        self.assertTrue(released, "CHANGELOG 没有任何版本条目")
        self.assertEqual(released[0], version,
                         f"CHANGELOG 最新版本是 {released[0]}，与 _version.py 的 {version} 不一致")


class ReleaseVersionConstraintTest(unittest.TestCase):
    """发版文档一致性：版本/计数/39/39 必须与 _version.py 同源（2026-11-22 新增）。"""

    def test_current_version_consistent_across_release_docs(self):
        pretty = _pretty_version(_current_version())
        self.assertIn(f"| 版本 | `{pretty}`（schema v2） |", _read("README.md"))
        handoff_head = "\n".join(_read("HANDOFF.md").splitlines()[:10])
        self.assertIn(f"状态：**v{pretty} 已发布**", handoff_head)
        self.assertIn(f"# guiju v{pretty} 安装与自部署", _read("INSTALL.md"))
        acceptance_head = "\n".join(_read("docs/ACCEPTANCE.md").splitlines()[:8])
        self.assertIn(f"最新发布验收：**v{pretty}", acceptance_head)
        # 发布文档不得把旧版本写成当前版本（历史段允许保留，见下面 history 测试）
        for rel in ("README.md", "HANDOFF.md", "INSTALL.md"):
            self.assertNotIn("1.2.0-alpha.3", _read(rel), f"{rel} 仍把旧版本写成当前")
        self.assertNotIn("1.2.0-alpha.3", acceptance_head.splitlines()[2])

    def test_current_test_count_consistent(self):
        readme = re.search(r"\|\s*单测 / 端到端\s*\|\s*(\d+) 项单测", _read("README.md"))
        handoff = re.search(
            r"单测 \*\*(\d+) 项\*\*",
            "\n".join(_read("HANDOFF.md").splitlines()[:10]),
        )
        acceptance = re.search(
            r"\*\*(\d+) 项单测\*\*",
            "\n".join(_read("docs/ACCEPTANCE.md").splitlines()[:8]),
        )
        self.assertIsNotNone(readme, "README 缺少当前单测计数")
        self.assertIsNotNone(handoff, "HANDOFF 首段缺少当前单测计数")
        self.assertIsNotNone(acceptance, "ACCEPTANCE 顶部缺少当前单测计数")
        counts = {int(readme.group(1)), int(handoff.group(1)), int(acceptance.group(1))}
        self.assertEqual(len(counts), 1, f"README/HANDOFF/ACCEPTANCE 的当前单测计数不一致：{counts}")
        self.assertGreater(counts.pop(), 0)

    def test_current_e2e_claims_39_39(self):
        self.assertIn("39/39", _read("README.md"), "README 未写完整联网 e2e 39/39")
        self.assertIn(
            "39/39",
            "\n".join(_read("HANDOFF.md").splitlines()[:10]),
            "HANDOFF 首段未写完整联网 e2e 39/39",
        )
        self.assertIn(
            "39/39",
            "\n".join(_read("docs/ACCEPTANCE.md").splitlines()[:8]),
            "ACCEPTANCE 顶部未写完整联网 e2e 39/39",
        )

    def test_changelog_top_matches_current_version(self):
        version = _current_version()
        text = _read("CHANGELOG.md")
        headings = re.findall(r"^##\s+(\S+)", text, flags=re.MULTILINE)
        self.assertEqual(headings[0], version, "CHANGELOG 最新版本与 _version.py 不一致")
        top_heading = next(line for line in text.splitlines() if line.startswith("## "))
        self.assertIn(_pretty_version(version), top_heading, "CHANGELOG 顶部条目未写预发布别名")

    def test_pyproject_is_posix_only(self):
        pyproject = _read("pyproject.toml")
        self.assertNotIn("Operating System :: OS Independent", pyproject)
        self.assertIn("Operating System :: POSIX :: Linux", pyproject)
        self.assertIn("Operating System :: MacOS", pyproject)
        self.assertIn("Linux / macOS", _read("INSTALL.md"), "INSTALL 未明确 Linux / macOS")

    def test_release_audit_record_and_notes_exist(self):
        version = _current_version()
        pretty = _pretty_version(version)
        audits = sorted((ROOT / "docs/audit").glob(f"release-{version}-*.md"))
        self.assertTrue(audits, f"缺少 docs/audit/release-{version}-*.md 发版记录")
        text = audits[0].read_text(encoding="utf-8")
        self.assertIn(f"v{pretty}", text)
        self.assertIn("39/39", text)
        self.assertTrue((ROOT / "docs/releases" / f"v{pretty}.md").is_file(), "缺少 release notes")

    def test_history_readings_preserved(self):
        """B3 与 remediation 的当时读数不得被发版文档改写。"""
        self.assertIn("1010 tests OK", _read("docs/audit/b3-w2-w3-w4-20261122.md"))
        self.assertIn("1010 tests OK", _read("docs/remediation-2026-11-18.md"))
        remediation_head = "\n".join(_read("docs/remediation-2026-11-18.md").splitlines()[:15])
        self.assertIn("后续状态", remediation_head)
        self.assertIn("release-1.2.0a4-20261122.md", remediation_head)


class ReleaseWorkflowConstraintTest(unittest.TestCase):
    """发版门接线：docs-only 进默认 CI，artifacts 只在发版 workflow。"""

    def test_docs_only_gate_wired(self):
        self.assertIn("check-release.py --docs-only", _read("scripts/ci.sh"))
        self.assertIn("check-release.py --docs-only", _read(".github/workflows/ci.yml"))

    def test_artifacts_gate_is_release_only(self):
        self.assertNotIn("--artifacts", _read(".github/workflows/ci.yml"))
        release = _read(".github/workflows/release-check.yml")
        self.assertIn("workflow_dispatch", release)
        self.assertIn("tags:", release)
        self.assertIn("v*", release)
        self.assertNotIn("pull_request", release)
        self.assertIn("check-release.py --artifacts", release)

    def test_check_release_script_exists_and_docs_only_passes(self):
        import subprocess
        import sys

        script = ROOT / "scripts" / "check-release.py"
        self.assertTrue(script.is_file())
        proc = subprocess.run(
            [sys.executable, str(script), "--docs-only"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class IndexCommandRemovedTest(unittest.TestCase):
    """ADR 0001 的 index 已彻底移除（v0.13.0）：既不暴露命令，也不残留推荐文案。"""

    def test_index_is_not_a_subcommand(self):
        import argparse
        from pathlib import Path as _P

        from guiju.cli import build_parser

        parser = build_parser()
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        self.assertNotIn("index", subs[0].choices)
        self.assertFalse(
            (_P(__file__).resolve().parent.parent / "guiju/cli/commands/index.py").exists(),
            "index.py 应当已删除",
        )

    def test_no_text_recommends_index(self):
        import re
        from pathlib import Path as _P

        root = _P(__file__).resolve().parent.parent / "guiju"
        offenders = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"guiju index", text):
                offenders.append(f"{path.name}:{text[: match.start()].count(chr(10)) + 1}")
        self.assertEqual(offenders, [], f"仍有文案在推荐 guiju index：{offenders}")


class ErrorPayloadConstraintTest(unittest.TestCase):
    """P3-7 的同类扫描：GuijuError 的 raise 不得传 cite= 成功字段。"""

    def test_no_error_raise_passes_cite_keyword(self):
        import ast

        root = ROOT / "guiju"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                    continue
                for keyword in node.exc.keywords:
                    if keyword.arg == "cite":
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], f"错误 raise 不应携带 cite=：{offenders}")


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
