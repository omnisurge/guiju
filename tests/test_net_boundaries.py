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

    def test_facades_have_no_opener(self):
        for name in ("guiju/compliance.py", "guiju/currency/httpx.py"):
            self.assertNotIn("_opener", (ROOT / name).read_text("utf-8"))

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

    def test_compliance_and_currency_redirect_factories_absent(self):
        from guiju import compliance
        from guiju.currency import httpx
        self.assertNotIn("_GuardedRedirect", compliance.__all__)
        self.assertFalse(hasattr(compliance, "_GuardedRedirect"))
        self.assertFalse(hasattr(httpx, "_MetadataRedirect"))


if __name__ == "__main__":
    unittest.main()
