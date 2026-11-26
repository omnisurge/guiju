"""Structural gates for the single outbound layer (C5)."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED = {"urllib.request", "urllib.error", "http.client", "ssl"}
NET_ONLY_CALLS = {"urlopen", "build_opener", "HTTPConnection", "HTTPSConnection", "create_connection"}


def _files():
    return [p for p in (ROOT / "guiju").rglob("*.py") if "__pycache__" not in p.parts]


def _module_name(node):
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


class NetBoundaryTest(unittest.TestCase):
    def test_only_body_and_cache_use_os_replace(self):
        allowed = {"guiju/net/body.py", "guiju/store/currency.py"}
        hits = set()
        for path in (ROOT / "guiju").rglob("*.py"):
            if "os.replace" in path.read_text("utf-8"):
                hits.add(str(path.relative_to(ROOT)))
        self.assertEqual(hits, allowed)

    def test_pull_has_no_direct_replace_and_uses_finalize(self):
        # B4：pull 编排在 application；结构门禁看真实实现。
        text = (ROOT / "guiju/application/pull.py").read_text("utf-8")
        self.assertNotIn("os.replace", text)
        self.assertIn("finalize_body", text)

    def test_body_has_no_post_json(self):
        text = (ROOT / "guiju/net/body.py").read_text("utf-8")
        self.assertNotIn("post_json", text)

    def test_one_redirect_guard_class(self):
        classes = []
        for path in (ROOT / "guiju/net").glob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            classes.extend(n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        self.assertEqual(classes.count("RedirectGuard"), 1)

    def test_no_opener_outside_net(self):
        bad = []
        for path in _files():
            rel = str(path.relative_to(ROOT))
            if rel.startswith("guiju/net/"):
                continue
            if "_opener" in path.read_text("utf-8"):
                bad.append(rel)
        self.assertEqual(bad, [])

    def test_body_finalize_is_sole_body_replace(self):
        text = (ROOT / "guiju/net/body.py").read_text("utf-8")
        self.assertEqual(text.count("os.replace"), 2)

    def test_banned_transport_imports_only_in_net(self):
        bad = []
        for path in _files():
            rel = str(path.relative_to(ROOT))
            if rel.startswith("guiju/net/"):
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                for name in _module_name(node):
                    if name in BANNED:
                        bad.append(f"{rel}:{node.lineno}:{name}")
        self.assertEqual(bad, [])

    def test_http_server_only_mcp(self):
        bad = []
        for path in _files():
            rel = str(path.relative_to(ROOT))
            if "http.server" in path.read_text("utf-8") and rel != "guiju/mcp.py":
                bad.append(rel)
        self.assertEqual(bad, [])

    def test_net_does_not_import_product_layers(self):
        bad = []
        for path in (ROOT / "guiju/net").glob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                for name in _module_name(node):
                    if any(part in name for part in ("guiju.domain", "guiju.sources", "guiju.store", "guiju.currency", "guiju.cli")):
                        bad.append(f"{path.name}:{node.lineno}:{name}")
        self.assertEqual(bad, [])

    def test_transport_calls_only_in_net(self):
        bad = []
        for path in _files():
            rel = str(path.relative_to(ROOT))
            if rel.startswith("guiju/net/"):
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in NET_ONLY_CALLS:
                        bad.append(f"{rel}:{node.lineno}:{name}")
        self.assertEqual(bad, [])

    def test_duplicate_private_implementations_are_gone(self):
        banned_defs = {"_last_call", "_throttle", "_check_url", "_detect_block", "_GuardedRedirect", "_MetadataRedirect"}
        bad = []
        for path in _files():
            rel = str(path.relative_to(ROOT))
            if rel.startswith("guiju/net/"):
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in banned_defs:
                    bad.append(f"{rel}:{node.lineno}:{node.name}")
        self.assertEqual(bad, [])

    def test_redirect_factories_only_in_net(self):
        banned = {"_GuardedRedirect", "_MetadataRedirect"}
        bad = []
        for path in _files():
            rel = str(path.relative_to(ROOT))
            if rel.startswith("guiju/net/"):
                continue
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in banned:
                    bad.append(f"{rel}:{node.lineno}:{node.name}")
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name == "RedirectGuard":
                        bad.append(f"{rel}:{node.lineno}:RedirectGuard")
        self.assertEqual(bad, [])

    def test_currency_httpx_facade_retired(self):
        self.assertFalse((ROOT / "guiju/currency/httpx.py").exists())
        self.assertFalse((ROOT / "guiju/currency/httpx").exists())
        bad = []
        py_files = [
            p
            for base in (ROOT / "guiju", ROOT / "tests")
            for p in base.rglob("*.py")
            if "__pycache__" not in p.parts
        ]
        for path in py_files:
            rel = str(path.relative_to(ROOT))
            in_currency = path.is_relative_to(ROOT / "guiju/currency")
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "guiju.currency.httpx" or alias.name.startswith("guiju.currency.httpx."):
                            bad.append(f"{rel}:{node.lineno}:{alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if (
                        mod == "guiju.currency.httpx"
                        or mod.startswith("guiju.currency.httpx.")
                        or (mod == "guiju.currency" and any(a.name == "httpx" for a in node.names))
                        or (
                            in_currency
                            and node.level > 0
                            and (
                                mod == "httpx"
                                or mod.startswith("httpx.")
                                or (mod == "" and any(a.name == "httpx" for a in node.names))
                            )
                        )
                    ):
                        bad.append(f"{rel}:{node.lineno}:from {mod or '.'}")
        self.assertEqual(bad, [])

    def test_compliance_facade_retired(self):
        self.assertFalse((ROOT / "guiju/compliance.py").exists())
        self.assertFalse((ROOT / "guiju/compliance").exists())
        bad = []
        py_files = [
            p
            for base in (ROOT / "guiju", ROOT / "tests")
            for p in base.rglob("*.py")
            if "__pycache__" not in p.parts
        ]
        for path in py_files:
            rel = str(path.relative_to(ROOT))
            in_guiju = path.is_relative_to(ROOT / "guiju")
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "guiju.compliance" or alias.name.startswith("guiju.compliance."):
                            bad.append(f"{rel}:{node.lineno}:{alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if (
                        mod == "guiju.compliance"
                        or mod.startswith("guiju.compliance.")
                        or (mod in ("", "guiju") and any(a.name == "compliance" for a in node.names))
                        or (in_guiju and node.level > 0 and (mod == "compliance" or mod.startswith("compliance.")))
                    ):
                        bad.append(f"{rel}:{node.lineno}:from {mod or '.'}")
        self.assertEqual(bad, [])

    def test_cli_shims_and_module_aliases_retired(self):
        self.assertFalse((ROOT / "guiju/cli/lookup.py").exists())
        self.assertFalse((ROOT / "guiju/cli/lookup").exists())
        self.assertFalse((ROOT / "guiju/cli/render.py").exists())
        self.assertFalse((ROOT / "guiju/cli/render").exists())

        bad_imports = []
        py_files = [
            p
            for base in (ROOT / "guiju", ROOT / "tests")
            for p in base.rglob("*.py")
            if "__pycache__" not in p.parts
        ]
        for path in py_files:
            rel = str(path.relative_to(ROOT))
            in_cli = path.is_relative_to(ROOT / "guiju/cli")
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in ("guiju.cli.lookup", "guiju.cli.render") or alias.name.startswith(
                            ("guiju.cli.lookup.", "guiju.cli.render.")
                        ):
                            bad_imports.append(f"{rel}:{node.lineno}:{alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if (
                        mod in ("guiju.cli.lookup", "guiju.cli.render")
                        or mod.startswith(("guiju.cli.lookup.", "guiju.cli.render."))
                        or (mod in ("", "guiju.cli") and any(a.name in ("lookup", "render") for a in node.names))
                        or (
                            in_cli
                            and node.level > 0
                            and (
                                mod in ("lookup", "render")
                                or mod.startswith(("lookup.", "render."))
                            )
                        )
                    ):
                        bad_imports.append(f"{rel}:{node.lineno}:from {mod or '.'}")
        self.assertEqual(bad_imports, [])

        bad_aliases = []
        for path in _files():
            rel = str(path.relative_to(ROOT))
            text = path.read_text("utf-8")
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and isinstance(target.value.value, ast.Name)
                        and target.value.value.id == "sys"
                        and target.value.attr == "modules"
                        and isinstance(target.slice, ast.Name)
                        and target.slice.id == "__name__"
                    ):
                        bad_aliases.append(f"{rel}:{node.lineno}")
            if "sys.modules[__name__]" in text:
                bad_aliases.append(f"{rel}:text")
        self.assertEqual(bad_aliases, [])

    def test_application_has_no_implicit_adapter_construction(self):
        banned_symbols = {
            "Store",
            "CachedCurrency",
            "default_currency",
            "NullCurrency",
            "default_backend",
            "registry",
        }
        banned_env_helpers = {"default_out_dir", "default_home"}
        bad = []
        for path in (ROOT / "guiju/application").glob("*.py"):
            rel = str(path.relative_to(ROOT))
            is_context = path.name == "context.py"
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in ("guiju.currency", "guiju.sources", "guiju.store.sqlite"):
                            bad.append(f"{rel}:{node.lineno}:import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    names = {a.name for a in node.names}
                    if names & banned_symbols:
                        bad.append(f"{rel}:{node.lineno}:from {mod} import {sorted(names & banned_symbols)}")
                    if mod == "" and node.level > 0 and (names & {"currency", "sources"}):
                        bad.append(f"{rel}:{node.lineno}:from . import {sorted(names & {'currency', 'sources'})}")
                    if mod in ("guiju.currency", "guiju.sources") or (
                        node.level > 0 and mod in ("currency", "sources")
                    ):
                        bad.append(f"{rel}:{node.lineno}:from {mod}")
                    if not is_context and (names & banned_env_helpers):
                        bad.append(f"{rel}:{node.lineno}:env helper {sorted(names & banned_env_helpers)}")
                elif isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in banned_symbols or (not is_context and name in banned_env_helpers):
                        bad.append(f"{rel}:{node.lineno}:call {name}")
        self.assertEqual(bad, [])

        retired_defs = {
            "guiju/application/cite.py": {"_check_currency"},
            "guiju/application/resolve.py": {"_CurrencyModuleProxy", "currency_mod", "_sources_or_registry"},
            "guiju/application/pull.py": {"_cached_currency"},
            "guiju/application/context.py": {"currency_or_default"},
        }
        leftovers = []
        for rel, names in retired_defs.items():
            tree = ast.parse((ROOT / rel).read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
                    leftovers.append(f"{rel}:{node.lineno}:{node.name}")
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in names:
                            leftovers.append(f"{rel}:{node.lineno}:{target.id}")
        self.assertEqual(leftovers, [])

    def test_mcp_service_surface_purified(self):
        from guiju import mcp as mcp_mod

        tree = ast.parse((ROOT / "guiju/mcp.py").read_text("utf-8"))
        defs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        def_names = {node.name for node in defs}
        self.assertNotIn("_mcp_io_root", def_names)
        self.assertNotIn("_safe_mcp_path", def_names)

        ro_cls = next(node for node in defs if isinstance(node, ast.ClassDef) and node.name == "_ReadOnlyStore")
        ro_assigns = {
            target.id
            for node in ast.walk(ro_cls)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertIn("_ALLOWED_READS", ro_assigns)
        self.assertNotIn("_WRITES", ro_assigns)

        qr_fn = next(node for node in defs if isinstance(node, ast.FunctionDef) and node.name == "_query_result")
        lookup_calls = [
            node
            for node in ast.walk(qr_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "lookup"
        ]
        self.assertEqual(lookup_calls, [])

        batch_tool = next(t for t in mcp_mod.TOOLS if t["name"] == "batch")
        props = set(batch_tool["inputSchema"]["properties"])
        self.assertEqual(props, {"codes"})
        self.assertEqual(batch_tool["inputSchema"]["required"], ["codes"])


if __name__ == "__main__":
    unittest.main()
