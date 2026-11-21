"""Structural gates for the single outbound layer (C5)."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NetBoundaryTest(unittest.TestCase):
    def test_only_body_and_cache_use_os_replace(self):
        allowed = {"guiju/net/body.py", "guiju/store/currency.py"}
        hits = set()
        for path in (ROOT / "guiju").rglob("*.py"):
            if "os.replace" in path.read_text("utf-8"):
                hits.add(str(path.relative_to(ROOT)))
        self.assertEqual(hits, allowed)

    def test_pull_has_no_direct_replace_and_uses_finalize(self):
        text = (ROOT / "guiju/cli/commands/pull.py").read_text("utf-8")
        self.assertNotIn("os.replace", text)
        self.assertIn("finalize_body", text)

    def test_body_has_no_post_json(self):
        text = (ROOT / "guiju/net/body.py").read_text("utf-8")
        self.assertNotIn("def post_json", text)
        self.assertNotIn("post_json", text)

    def test_one_redirect_guard_class(self):
        classes = []
        for path in (ROOT / "guiju/net").glob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            classes.extend(n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
        self.assertEqual(classes.count("RedirectGuard"), 1)

    def test_facades_have_no_opener(self):
        for name in ("guiju/compliance.py", "guiju/currency/httpx.py"):
            text = (ROOT / name).read_text("utf-8")
            self.assertNotIn("_opener", text)

    def test_body_finalize_is_sole_body_replace(self):
        text = (ROOT / "guiju/net/body.py").read_text("utf-8")
        self.assertEqual(text.count("os.replace"), 2)  # sidecar + finalize only


if __name__ == "__main__":
    unittest.main()
