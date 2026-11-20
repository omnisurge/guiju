"""B1 W2：按源水位与 status 输出。"""

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from guiju.cli import render
from guiju.cli.commands import status
from guiju.cli.commands import sync as sync_mod
from guiju.store.sqlite import Store, now


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

    def test_if_stale_does_not_skip_legacy_incomplete(self):
        self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "标题", "名称", "other"
        )
        self.store.set_meta("synced_at", now())
        self.store.set_meta("catalog_complete", "0")
        self.store.commit()
        args = Namespace(if_stale=7, source="mohurd", page_size=100, pages=0)
        with mock.patch.object(
            sync_mod, "crawl", return_value={
                "new": 0, "total_in_db": 1, "scanned": 0, "complete": False,
                "truncated": False, "source": "mohurd",
            }
        ) as crawl:
            sync_mod.run(self.store, args)
        self.assertEqual(crawl.call_count, 1)

    def test_sync_lock_held_reports_refreshing_without_crawling(self):
        args = Namespace(if_stale=None, source="mohurd", page_size=100, pages=0)
        buf = io.StringIO()
        render.configure(as_json=True, quiet=True)
        with mock.patch.object(sync_mod, "safe_sync") as fake_lock, \
                mock.patch.object(sync_mod, "crawl") as crawl, \
                redirect_stdout(buf):
            fake_lock.return_value.__enter__.return_value = False
            rc = sync_mod.run(self.store, args)
        self.assertEqual(rc, 0)
        crawl.assert_not_called()
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["skipped"])
        self.assertEqual(payload["reason"], "refreshing")
        self.assertEqual(payload["state"], "refreshing")
        self.assertEqual(payload["retry_after_seconds"], 30)

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
