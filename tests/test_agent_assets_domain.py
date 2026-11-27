"""v1.5.0 S1：使用态单一真源资产、内容净化、退出码契约与纯领域状态机单测。"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import re
import unittest
from pathlib import Path

from guiju import errors as errors_mod
from guiju import mcp as mcp_mod
from guiju.agent_assets import (
    load_current_hashes,
    load_history_hashes,
    load_mcp_instructions,
    load_skill_markdown,
    read_contract_markdown,
    read_history_hashes_json,
    read_skill_frontmatter,
)
from guiju.application import agent as agent_app
from guiju.cli import build_parser
from guiju.domain import availability as avail_domain
from guiju.domain.agent_assets import (
    DiskSnapshot,
    ReceiptRecord,
    extract_mcp_instructions,
    inspect_target_status,
    parse_history_hashes,
    plan_install,
    plan_uninstall,
    render_skill_markdown,
    sha256_text,
    strip_skill_frontmatter,
    validate_file_hashes,
    validate_relpath,
    validate_skill_frontmatter,
)
from guiju.errors import (
    AGENT_CONFLICT_REASONS,
    EXIT_CONFLICT,
    EXIT_INTERNAL,
    EXIT_NO_CURRENCY_RECORD,
    EXIT_TABLE,
    AgentConflict,
    GuijuError,
)

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_CONSUMER_TOKENS = (
    "../",
    "./docs/",
    "AGENTS.md",
    "README.md",
    "CONTRIBUTING.md",
    "HANDOFF.md",
    "INSTALL.md",
    "pyproject.toml",
    "BodyPolicy",
    "MetadataPolicy",
    "RedirectGuard",
    "Throttle",
    "_SOURCE_HOSTS",
    "red-team-review",
    "git push",
    "ci.sh",
)

FORBIDDEN_CONSUMER_REGEXES = (
    re.compile(r"(?<![A-Za-z0-9_.-])(?:docs|scripts|tests)/"),
    re.compile(r"(?<!\.)\bguiju/(?:net|sources|domain|store|application|currency|cli|ports|adapters|agent_assets)\b"),
    re.compile(r"\b[A-Za-z0-9_]+\.py\b"),
)


def _dump_help_surfaces(parser: argparse.ArgumentParser, prefix: str = "guiju") -> dict[str, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        parser.print_help()
    surfaces = {f"help:{prefix}": buf.getvalue()}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in sorted(action.choices.items()):
                surfaces.update(_dump_help_surfaces(sub, f"{prefix} {name}"))
    return surfaces


def _extract_hint_strings_from_ast() -> dict[str, str]:
    collected: dict[str, list[str]] = {}
    for py_path in sorted((ROOT / "guiju").rglob("*.py")):
        if "__pycache__" in py_path.parts:
            continue
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        rel = str(py_path.relative_to(ROOT))
        for node in ast.walk(tree):
            expr: ast.AST | None = None
            lineno = 0
            if isinstance(node, ast.keyword) and node.arg == "hint":
                expr = node.value
                lineno = node.value.lineno
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in ("hint", "_HINT", "atlas_hint"):
                        expr = node.value
                        lineno = node.lineno
                        break
            if expr is None:
                continue
            pieces: list[str] = []
            for sub in ast.walk(expr):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value:
                    pieces.append(sub.value)
            if pieces:
                collected.setdefault(f"ast_hint:{rel}:{lineno}", []).append("".join(pieces))
    return {k: "\n".join(v) for k, v in collected.items()}


def _table_exit_codes(markdown: str, heading: str) -> set[int]:
    pattern = re.compile(rf"^##\s+{re.escape(heading)}\s*$", flags=re.MULTILINE)
    match = pattern.search(markdown)
    if not match:
        raise AssertionError(f"找不到章节：{heading}")
    tail = markdown[match.end() :]
    end = re.search(r"^(?:##|---)\s+", tail, flags=re.MULTILINE)
    section = tail[: end.start()] if end else tail
    return {
        int(row.group(1))
        for row in re.finditer(r"^\|\s*(\d+)\s*\|", section, flags=re.MULTILINE)
    }


class PackageAssetsAndSSOTTest(unittest.TestCase):
    def test_pyproject_declares_agent_assets_package_data(self):
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"guiju.agent_assets" = ["*.md", "*.yaml", "*.json"]', pyproject)
        self.assertTrue((ROOT / "guiju/agent_assets/__init__.py").is_file())

    def test_skill_derivation_byte_for_byte_identity(self):
        contract = read_contract_markdown()
        frontmatter = read_skill_frontmatter()
        rendered = load_skill_markdown()
        self.assertNotIn("\r", rendered)
        self.assertEqual(strip_skill_frontmatter(rendered), contract)
        self.assertEqual(rendered, render_skill_markdown(frontmatter, contract))

    def test_mcp_instructions_verbatim_from_contract_markers(self):
        contract = read_contract_markdown()
        instructions = load_mcp_instructions()
        self.assertTrue(instructions)
        self.assertIn(instructions, contract)
        self.assertNotIn("mcp-summary", instructions)

    def test_consumer_surfaces_clean_of_dev_leaks_and_dead_links(self):
        class _DummyBackend:
            def query(self, _code: str):
                raise RuntimeError("unused")

        init_result = mcp_mod.McpServer(_DummyBackend()).handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        )["result"]

        tool_texts: dict[str, str] = {}
        for tool in mcp_mod.TOOLS:
            name = tool["name"]
            tool_texts[f"mcp_tool:{name}"] = tool["description"]
            for prop_name, prop_spec in (tool.get("inputSchema", {}).get("properties") or {}).items():
                desc = prop_spec.get("description")
                if desc:
                    tool_texts[f"mcp_tool:{name}.{prop_name}"] = desc

        error_class_hints: dict[str, str] = {}
        for obj in vars(errors_mod).values():
            if isinstance(obj, type) and issubclass(obj, GuijuError) and obj.hint:
                error_class_hints[f"error_class:{obj.__name__}.hint"] = obj.hint

        surfaces: dict[str, str] = {
            "asset:contract.md": read_contract_markdown(),
            "asset:skill_frontmatter.yaml": read_skill_frontmatter(),
            "asset:history_hashes.json": read_history_hashes_json(),
            "asset:SKILL.md": load_skill_markdown(),
            "mcp:initialize.instructions": init_result["instructions"],
            "cli:EXIT_TABLE": "\n".join(
                f"{code} {meaning} {action}" for code, (meaning, action) in sorted(EXIT_TABLE.items())
            ),
        }
        surfaces.update(tool_texts)
        surfaces.update(_dump_help_surfaces(build_parser()))
        surfaces.update(error_class_hints)
        surfaces.update(_extract_hint_strings_from_ast())
        for reason, hint_text in agent_app._CONFLICT_HINTS.items():
            surfaces[f"agent_conflict_hint:{reason}"] = hint_text
        for win in (
            avail_domain.Window.PRE_WINDOW,
            avail_domain.Window.IN_WINDOW,
            avail_domain.Window.UNKNOWN,
        ):
            surfaces[f"availability_note:{win.value}"] = avail_domain.note(win, "2020-01-01")

        for name, text in surfaces.items():
            for token in FORBIDDEN_CONSUMER_TOKENS:
                with self.subTest(surface=name, token=token):
                    self.assertNotIn(token, text)
            for pattern in FORBIDDEN_CONSUMER_REGEXES:
                with self.subTest(surface=name, pattern=pattern.pattern):
                    self.assertNotRegex(text, pattern)

    def test_docs_agent_usage_is_minimal_pointer_page(self):
        pointer = (ROOT / "docs/AGENT-USAGE.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(pointer.splitlines()), 20)
        self.assertIn("../guiju/agent_assets/contract.md", pointer)
        self.assertIn("guiju agent show --kind contract", pointer)
        self.assertNotIn("mohurd.py", pointer)
        self.assertNotIn("BodyPolicy", pointer)
        self.assertNotRegex(pointer, r"(?m)^\|\s*\d+\s*\|")

    def test_exit_codes_parity_across_errors_readme_and_contract(self):
        expected = set(EXIT_TABLE.keys()) | {EXIT_INTERNAL}
        self.assertIn(EXIT_CONFLICT, EXIT_TABLE)
        self.assertIn(EXIT_NO_CURRENCY_RECORD, EXIT_TABLE)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        readme_codes = _table_exit_codes(readme, "退出码（稳定契约）")
        contract_codes = _table_exit_codes(read_contract_markdown(), "三、退出码 → 你的动作")
        self.assertEqual(expected, readme_codes)
        self.assertEqual(expected, contract_codes)

    def test_agent_conflict_error_contract(self):
        for reason in sorted(AGENT_CONFLICT_REASONS):
            err = AgentConflict("目标异常", reason=reason, target_path="/tmp/x")
            payload = err.payload()
            self.assertEqual(err.exit, EXIT_CONFLICT)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["exit"], EXIT_CONFLICT)
            self.assertEqual(payload["reason"], reason)
            self.assertEqual(payload["target_path"], "/tmp/x")
            self.assertTrue(payload["hint"])

        with self.assertRaises(ValueError):
            AgentConflict("bad", reason="not_a_real_reason")

    def test_history_hashes_bundled_matches_current_and_strict_schema(self):
        current = load_current_hashes()
        history = load_history_hashes()
        self.assertIn("1.5.0", history)
        self.assertEqual(history["1.5.0"], current)
        # 确保原始 JSON 通过严格解析且不含重复键
        self.assertEqual(parse_history_hashes(read_history_hashes_json()), history)

        bad_payloads = [
            '{"schema": 2, "versions": {}}',
            '{"schema": 1, "versions": {}, "versions": {}}',
            '{"schema": 1}',
            '{"schema": 1, "versions": {"invalid": {"SKILL.md": "' + "a" * 64 + '"}}}',
            '{"schema": 1, "versions": {"1.5.0": {"../evil": "' + "a" * 64 + '"}}}',
            '{"schema": 1, "versions": {"1.5.0": {"SKILL.md": "not-sha256"}}}',
            '{"schema": 1, "versions": {"1.5.0": {}}}',
        ]
        for raw in bad_payloads:
            with self.subTest(raw=raw[:50]), self.assertRaises(ValueError):
                parse_history_hashes(raw)

    def test_frontmatter_relpath_and_marker_guards(self):
        with self.assertRaises(ValueError):
            validate_skill_frontmatter("name: guiju-standards\ndescription: x\n---")
        with self.assertRaises(ValueError):
            validate_skill_frontmatter("name: other\ndescription: x")
        with self.assertRaises(ValueError):
            validate_skill_frontmatter("name: guiju-standards\ndescription: x\r\n")
        with self.assertRaises(ValueError):
            strip_skill_frontmatter("# no frontmatter")
        with self.assertRaises(ValueError):
            extract_mcp_instructions("no markers")
        with self.assertRaises(ValueError):
            extract_mcp_instructions("<!-- guiju:mcp-summary:begin --><!-- guiju:mcp-summary:end -->")
        with self.assertRaises(ValueError):
            extract_mcp_instructions(
                "<!-- guiju:mcp-summary:end -->x<!-- guiju:mcp-summary:begin -->"
            )
        for bad_rel in ("", "/abs", "a/", "../x", "a/./b", "a\\b", "a\x00b"):
            with self.subTest(bad_rel=bad_rel), self.assertRaises(ValueError):
                validate_relpath(bad_rel)
        with self.assertRaises(ValueError):
            validate_file_hashes({})


class AgentAssetsStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.h150 = {"SKILL.md": sha256_text("skill-1.5.0")}
        self.h151 = {"SKILL.md": sha256_text("skill-1.5.1")}
        self.h152 = {"SKILL.md": sha256_text("skill-1.5.2")}
        self.h_unknown = {"SKILL.md": sha256_text("unknown-or-user-edited")}
        self.h_extra = {"SKILL.md": self.h150["SKILL.md"], "notes.md": sha256_text("user notes")}
        self.history = {"1.5.0": self.h150, "1.5.1": self.h151}

    def _receipt(self, version: str, hashes: dict[str, str]) -> ReceiptRecord:
        return ReceiptRecord(
            skill="guiju-standards",
            target="agents",
            scope="project",
            package_version=version,
            installed_at="2026-11-27 14:00:00",
            files=hashes,
        )

    def test_symlink_rejected_everywhere_including_force_and_missing_target(self):
        for exists in (False, True):
            disk = DiskSnapshot(exists=exists, has_symlink=True, files=self.h150 if exists else {})
            for force in (False, True):
                ins = plan_install(
                    disk,
                    receipt=self._receipt("1.5.0", self.h150),
                    receipt_corrupt=force,
                    current_version="1.5.0",
                    current_hashes=self.h150,
                    history_hashes=self.history,
                    force=force,
                )
                self.assertFalse(ins.ok)
                self.assertEqual(ins.conflict_reason, "agent_symlink_rejected")

                uni = plan_uninstall(
                    disk,
                    receipt=self._receipt("1.5.0", self.h150),
                    receipt_corrupt=False,
                    current_hashes=self.h150,
                    history_hashes=self.history,
                    force=force,
                )
                self.assertFalse(uni.ok)
                self.assertEqual(uni.conflict_reason, "agent_symlink_rejected")

            st = inspect_target_status(
                disk,
                receipt=self._receipt("1.5.0", self.h150),
                current_hashes=self.h150,
                history_hashes=self.history,
            )
            self.assertEqual(st.state, "symlink")

    def test_corrupt_receipt_matrix(self):
        absent = DiskSnapshot(exists=False, has_symlink=False, files={})
        refused = plan_install(
            absent,
            receipt=None,
            receipt_corrupt=True,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes=self.history,
            force=False,
        )
        self.assertFalse(refused.ok)
        self.assertEqual(refused.conflict_reason, "agent_receipt_corrupt")

        forced = plan_install(
            absent,
            receipt=None,
            receipt_corrupt=True,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes=self.history,
            force=True,
        )
        self.assertTrue(forced.ok)
        self.assertEqual(forced.action, "installed")
        self.assertTrue(forced.backup_corrupt_receipt)
        self.assertTrue(forced.write_files)
        self.assertTrue(forced.write_receipt)

        clean_disk = DiskSnapshot(exists=True, has_symlink=False, files=self.h150)
        forced_adopt = plan_install(
            clean_disk,
            receipt=None,
            receipt_corrupt=True,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes=self.history,
            force=True,
        )
        self.assertTrue(forced_adopt.ok)
        self.assertEqual(forced_adopt.action, "adopted")
        self.assertFalse(forced_adopt.write_files)
        self.assertTrue(forced_adopt.write_receipt)
        self.assertTrue(forced_adopt.backup_corrupt_receipt)

        for force in (False, True):
            uni = plan_uninstall(
                clean_disk,
                receipt=None,
                receipt_corrupt=True,
                current_hashes=self.h150,
                history_hashes=self.history,
                force=force,
            )
            self.assertFalse(uni.ok)
            self.assertEqual(uni.conflict_reason, "agent_receipt_corrupt")

    def test_registered_target_lifecycle_and_r1_team_sync(self):
        absent = DiskSnapshot(exists=False, has_symlink=False, files={})
        first = plan_install(
            absent,
            receipt=None,
            receipt_corrupt=False,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes=self.history,
        )
        self.assertTrue(first.ok)
        self.assertEqual(first.action, "installed")

        # 无变更
        same = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h150),
            receipt=self._receipt("1.5.0", self.h150),
            receipt_corrupt=False,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes=self.history,
        )
        self.assertTrue(same.ok)
        self.assertEqual(same.action, "unchanged")
        self.assertFalse(same.write_files)
        self.assertFalse(same.write_receipt)

        # R1：团队成员提交 1.5.1 后，A git pull 并升级 guiju 到 1.5.1，A 本机旧收据仍是 1.5.0
        pulled_current = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h151),
            receipt=self._receipt("1.5.0", self.h150),
            receipt_corrupt=False,
            current_version="1.5.1",
            current_hashes=self.h151,
            history_hashes=self.history,
        )
        self.assertTrue(pulled_current.ok)
        self.assertEqual(pulled_current.action, "unchanged")
        self.assertFalse(pulled_current.write_files)
        self.assertTrue(pulled_current.write_receipt)
        self.assertEqual(
            inspect_target_status(
                DiskSnapshot(exists=True, has_symlink=False, files=self.h151),
                receipt=self._receipt("1.5.0", self.h150),
                current_hashes=self.h151,
                history_hashes=self.history,
            ).state,
            "clean",
        )

        # R1：git checkout 切回旧版 1.5.0 分支，本机收据是 1.5.1，当前包是 1.5.2 -> 无需 --force 平滑升级
        checked_out_old = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h150),
            receipt=self._receipt("1.5.1", self.h151),
            receipt_corrupt=False,
            current_version="1.5.2",
            current_hashes=self.h152,
            history_hashes=self.history,
        )
        self.assertTrue(checked_out_old.ok)
        self.assertEqual(checked_out_old.action, "upgraded")
        self.assertTrue(checked_out_old.write_files)
        self.assertTrue(checked_out_old.write_receipt)
        self.assertEqual(
            inspect_target_status(
                DiskSnapshot(exists=True, has_symlink=False, files=self.h150),
                receipt=self._receipt("1.5.1", self.h151),
                current_hashes=self.h152,
                history_hashes=self.history,
            ).state,
            "upgradable",
        )

        # R1 反例：本机停留在 1.5.0（不知道 1.5.2 哈希），磁盘被拉取成 1.5.2 -> 拒绝返回 agent_user_modified
        lagging_pkg = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h152),
            receipt=self._receipt("1.5.0", self.h150),
            receipt_corrupt=False,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes={"1.5.0": self.h150},
        )
        self.assertFalse(lagging_pkg.ok)
        self.assertEqual(lagging_pkg.conflict_reason, "agent_user_modified")
        self.assertEqual(
            inspect_target_status(
                DiskSnapshot(exists=True, has_symlink=False, files=self.h152),
                receipt=self._receipt("1.5.0", self.h150),
                current_hashes=self.h150,
                history_hashes={"1.5.0": self.h150},
            ).state,
            "modified",
        )

    def test_unregistered_adopt_and_unmanaged_conflict(self):
        adopt_cur = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h151),
            receipt=None,
            receipt_corrupt=False,
            current_version="1.5.1",
            current_hashes=self.h151,
            history_hashes=self.history,
        )
        self.assertTrue(adopt_cur.ok)
        self.assertEqual(adopt_cur.action, "adopted")
        self.assertFalse(adopt_cur.write_files)
        self.assertTrue(adopt_cur.write_receipt)
        self.assertEqual(
            inspect_target_status(
                DiskSnapshot(exists=True, has_symlink=False, files=self.h151),
                receipt=None,
                current_hashes=self.h151,
                history_hashes=self.history,
            ).state,
            "adoptable_current",
        )

        adopt_old = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h150),
            receipt=None,
            receipt_corrupt=False,
            current_version="1.5.1",
            current_hashes=self.h151,
            history_hashes=self.history,
        )
        self.assertTrue(adopt_old.ok)
        self.assertEqual(adopt_old.action, "adopted_and_upgraded")
        self.assertTrue(adopt_old.write_files)
        self.assertTrue(adopt_old.write_receipt)
        self.assertEqual(
            inspect_target_status(
                DiskSnapshot(exists=True, has_symlink=False, files=self.h150),
                receipt=None,
                current_hashes=self.h151,
                history_hashes=self.history,
            ).state,
            "adoptable_upgrade",
        )

        unmanaged = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h_extra),
            receipt=None,
            receipt_corrupt=False,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes=self.history,
            force=False,
        )
        self.assertFalse(unmanaged.ok)
        self.assertEqual(unmanaged.conflict_reason, "agent_unmanaged_conflict")
        self.assertEqual(
            inspect_target_status(
                DiskSnapshot(exists=True, has_symlink=False, files=self.h_extra),
                receipt=None,
                current_hashes=self.h150,
                history_hashes=self.history,
            ).state,
            "unmanaged_conflict",
        )

        forced_unmanaged = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h_extra),
            receipt=None,
            receipt_corrupt=False,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes=self.history,
            force=True,
        )
        self.assertTrue(forced_unmanaged.ok)
        self.assertEqual(forced_unmanaged.action, "installed")
        self.assertEqual(forced_unmanaged.removed_extra_files, ("notes.md",))

    def test_force_extra_file_idempotence_and_uninstall_boundaries(self):
        forced_reg = plan_install(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h_extra, non_regular_entries=("stray.fifo",)),
            receipt=self._receipt("1.5.0", self.h150),
            receipt_corrupt=False,
            current_version="1.5.0",
            current_hashes=self.h150,
            history_hashes=self.history,
            force=True,
        )
        self.assertTrue(forced_reg.ok)
        self.assertEqual(forced_reg.removed_extra_files, ("notes.md", "stray.fifo"))
        self.assertEqual(forced_reg.files_to_remove, ("notes.md", "stray.fifo"))

        # 卸载：未受管目录即使 --force 也绝不强删
        for disk in (
            DiskSnapshot(exists=False, has_symlink=False, files={}),
            DiskSnapshot(exists=True, has_symlink=False, files=self.h_unknown),
            DiskSnapshot(exists=True, has_symlink=False, files=self.h_extra),
        ):
            for force in (False, True):
                res = plan_uninstall(
                    disk,
                    receipt=None,
                    receipt_corrupt=False,
                    current_hashes=self.h150,
                    history_hashes=self.history,
                    force=force,
                )
                self.assertFalse(res.ok)
                self.assertEqual(res.conflict_reason, "agent_not_managed")

        # 卸载：无收据但精准匹配官方版本 -> 允许安全删除
        clean_unreg = plan_uninstall(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h150),
            receipt=None,
            receipt_corrupt=False,
            current_hashes=self.h151,
            history_hashes=self.history,
        )
        self.assertTrue(clean_unreg.ok)
        self.assertFalse(clean_unreg.remove_receipt)
        self.assertEqual(clean_unreg.files_to_remove, ("SKILL.md",))

        # R4：有收据但目录已被 rm -rf -> 清理陈旧收据并退出 0
        missing_reg = plan_uninstall(
            DiskSnapshot(exists=False, has_symlink=False, files={}),
            receipt=self._receipt("1.5.0", self.h150),
            receipt_corrupt=False,
            current_hashes=self.h150,
            history_hashes=self.history,
        )
        self.assertTrue(missing_reg.ok)
        self.assertTrue(missing_reg.remove_receipt)
        self.assertEqual(missing_reg.files_to_remove, ())
        self.assertEqual(missing_reg.preserved_files, ())
        self.assertFalse(missing_reg.remove_dir_if_empty)

        # R1：有收据、经 git pull 同步为历史版（非 receipt 版）-> 直接卸载不需 --force
        pulled_uninst = plan_uninstall(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h151),
            receipt=self._receipt("1.5.0", self.h150),
            receipt_corrupt=False,
            current_hashes=self.h152,
            history_hashes=self.history,
        )
        self.assertTrue(pulled_uninst.ok)
        self.assertEqual(pulled_uninst.files_to_remove, ("SKILL.md",))

        # 已受管但含额外文件：不带 --force 拒绝，带 --force 仅删受管并保留 notes.md
        mod_refused = plan_uninstall(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h_extra),
            receipt=self._receipt("1.5.0", self.h150),
            receipt_corrupt=False,
            current_hashes=self.h150,
            history_hashes=self.history,
            force=False,
        )
        self.assertFalse(mod_refused.ok)
        self.assertEqual(mod_refused.conflict_reason, "agent_user_modified")

        mod_forced = plan_uninstall(
            DiskSnapshot(exists=True, has_symlink=False, files=self.h_extra),
            receipt=self._receipt("1.5.0", self.h150),
            receipt_corrupt=False,
            current_hashes=self.h150,
            history_hashes=self.history,
            force=True,
        )
        self.assertTrue(mod_forced.ok)
        self.assertTrue(mod_forced.remove_receipt)
        self.assertEqual(mod_forced.files_to_remove, ("SKILL.md",))
        self.assertEqual(mod_forced.preserved_files, ("notes.md",))
        self.assertFalse(mod_forced.remove_dir_if_empty)


if __name__ == "__main__":
    unittest.main()
