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
        # B4：pull 编排已迁到 application；CLI 只剩兼容 alias，结构门禁看真实实现。
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


if __name__ == "__main__":
    unittest.main()
