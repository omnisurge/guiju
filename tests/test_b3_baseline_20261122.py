"""B3.0A：只锁定既有稳定可观测行为，必须直接通过。

刻意不断言键集合封闭：B3 后续会合法新增 sources_all / aggregate /
catalog_meta_corrupt 等字段；也不在单测内部跑 discover（避免递归）。
"""

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

from guiju import autosync
from guiju import report as render
from guiju.cli.commands import status
from guiju.domain.search import rank, tokens_of
from guiju.store.sqlite import Store


class CatalogPayloadBaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)
        self.addCleanup(render.configure, as_json=False, quiet=False)
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/x.html", "2026-01-01", "标题", "名称", "other"
        )
        self.store.set_meta("sync:mohurd:complete", "1")
        self.store.set_meta("sync:mohurd:total", "1")
        self.store.set_meta("sync:mohurd:seen", "1")
        self.store.commit()

    def test_snap_has_existing_fields(self):
        snap = autosync._snap(self.store, state="fresh", source="mohurd")
        for key in (
            "state", "message", "source", "complete", "complete_reason",
            "age_days", "synced_at", "announcements", "new",
            "retry_after_seconds", "sources",
        ):
            self.assertIn(key, snap)
        self.assertEqual(snap["state"], "fresh")
        self.assertEqual(snap["source"], "mohurd")
        self.assertEqual(snap["announcements"], 1)
        self.assertIsInstance(snap["sources"], list)
        self.assertIn("complete", snap["sources"][0])

    def test_status_has_existing_fields(self):
        buf = io.StringIO()
        render.configure(as_json=True, quiet=True)
        with redirect_stdout(buf):
            rc = status.run(self.store, Namespace())
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        for key in (
            "ok", "db", "schema_version", "announcements", "editions",
            "mentions", "scanned", "synced_at", "catalog_age_days",
            "catalog_stale", "sources", "latest_published_on", "coverage",
            "by_year",
        ):
            self.assertIn(key, payload)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["sources"])


class StoreBaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)

    def test_upsert_and_search_baseline(self):
        ann_id, fresh = self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "防火规范", "建筑设计防火规范", "standard"
        )
        self.assertTrue(fresh)
        self.assertGreater(ann_id, 0)
        rows, _ = rank(self.store.catalog_entries(), tokens_of("防火"))
        self.assertEqual(len(rows), 1)
        same_id, fresh2 = self.store.upsert_announcement(
            "https://x/1", "2026-02-01", "新防火规范", "建筑设计防火规范", "standard"
        )
        self.assertEqual(same_id, ann_id)
        self.assertFalse(fresh2)


if __name__ == "__main__":
    unittest.main()
