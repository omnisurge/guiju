"""B1 W2：按源水位与 status 输出。"""

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

from guiju.cli import render
from guiju.cli.commands import status
from guiju.store.sqlite import Store


class SourceStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)
        self.addCleanup(render.configure, as_json=False, quiet=False)

    def test_source_meta_exposes_reason_and_counts(self):
        self.store.set_meta("sync:shzjw:complete", "0")
        self.store.set_meta("sync:shzjw:reason", "empty_page_before_total")
        self.store.set_meta("sync:shzjw:total", "10")
        self.store.set_meta("sync:shzjw:seen", "2")
        self.store.set_meta("sync:shzjw:synced_at", "2026-11-18 10:00:00")
        meta = self.store.source_meta("shzjw")
        self.assertFalse(meta["complete"])
        self.assertEqual(meta["reason"], "empty_page_before_total")
        self.assertEqual(meta["total"], 10)
        self.assertEqual(meta["seen"], 2)

    def test_status_lists_each_source(self):
        self.store.set_meta("sync:mohurd:complete", "1")
        self.store.set_meta("sync:mohurd:total", "3254")
        self.store.set_meta("sync:mohurd:seen", "3254")
        self.store.set_meta("sync:mohurd:synced_at", "2026-11-18 10:00:00")
        self.store.set_meta("sync:shzjw:complete", "0")
        self.store.set_meta("sync:shzjw:total", "10")
        self.store.set_meta("sync:shzjw:seen", "2")
        self.store.commit()
        buf = io.StringIO()
        render.configure(as_json=True, quiet=True)
        with redirect_stdout(buf):
            status.run(self.store, Namespace())
        payload = json.loads(buf.getvalue())
        by_id = {s["source"]: s for s in payload["sources"]}
        self.assertTrue(by_id["mohurd"]["complete"])
        self.assertFalse(by_id["shzjw"]["complete"])
        self.assertTrue(by_id["shzjw"]["stale"])
        self.assertEqual(by_id["shzjw"]["reason"], "")


if __name__ == "__main__":
    unittest.main()
