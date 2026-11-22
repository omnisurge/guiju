"""B3 W2：目录完成判定、按源元数据、autosync 聚合与 status。"""

import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from guiju import autosync, syncengine
from guiju.cli import render
from guiju.cli.commands import status
from guiju.sources import mohurd
from guiju.store.sqlite import SCHEMA_VERSION, Store


def _item(n=1):
    return {
        "url": f"https://www.mohurd.gov.cn/a/{n}",
        "date": "2026-01-01",
        "title": f"标准公告{n}",
    }


class _FakeSource:
    def __init__(self, pages, total=None):
        self.pages = pages
        self.total = total
        self.calls = []

    def catalog_page(self, page_no, page_size):
        self.calls.append(page_no)
        return list(self.pages.get(page_no, [])), self.total


class MohurdCatalogPageTest(unittest.TestCase):
    def _call(self, fragment):
        payload = {"success": True, "data": {"html": fragment}}
        with mock.patch.object(
            mohurd.compliance, "get", return_value=mock.Mock(text=json.dumps(payload))
        ):
            return mohurd.catalog_page(1, 100)

    def test_empty_fragment_has_unknown_total_not_zero(self):
        items, total = self._call("")
        self.assertEqual(items, [])
        self.assertIsNone(total)

    def test_missing_count_with_item_has_unknown_total(self):
        item = (
            '<li class="date">'
            '<a href="/gongkai/zc/wjk/art/2026/art_17339_1.html" title="标题">x</a>'
            '<span class="date-info">2026-01-01</span>'
            "</li>"
        )
        items, total = self._call(item)
        self.assertEqual(len(items), 1)
        self.assertIsNone(total)

    def test_explicit_zero_count_is_zero(self):
        items, total = self._call('count="0"')
        self.assertEqual(items, [])
        self.assertEqual(total, 0)


class SyncEngineW2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)

    def _crawl(self, source, fake):
        with mock.patch("guiju.syncengine.registry", return_value={source: fake}):
            return syncengine.crawl(self.store, source=source, page_size=2)

    def test_empty_fragment_source_is_incomplete(self):
        fake = _FakeSource({1: []}, total=None)
        # 用显式 None 覆盖 _FakeSource 的自动求和，模拟 mohurd 空 fragment。
        fake.total = None
        result = self._crawl("fake", fake)
        self.assertFalse(result["complete"])
        self.assertEqual(result["complete_reason"], "total_unknown")
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_mohurd_does_not_write_legacy_complete(self):
        fake = _FakeSource({1: [_item()]}, total=1)
        result = self._crawl("mohurd", fake)
        self.assertTrue(result["complete"])
        self.assertEqual(self.store.get_meta("sync:mohurd:complete"), "1")
        self.assertEqual(self.store.get_meta("catalog_complete"), "")
        self.assertNotEqual(self.store.get_meta("sync:mohurd:synced_at"), "")


class AutoSyncW2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)

    def _set_source(self, source, *, complete=True, days_ago=1, reason=""):
        ts = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.set_meta(f"sync:{source}:complete", "1" if complete else "0")
        self.store.set_meta(f"sync:{source}:reason", reason)
        self.store.set_meta(f"sync:{source}:synced_at", ts)
        self.store.commit()

    def test_catalog_sources_all_and_aggregate(self):
        self._set_source("mohurd", complete=True, days_ago=1)
        self._set_source("shzjw", complete=True, days_ago=1)
        snap = autosync._snap(self.store, state="refreshed", source="mohurd")
        self.assertEqual(snap["source"], "mohurd")
        self.assertEqual([s["source"] for s in snap["sources_all"]], ["mohurd", "shzjw"])
        self.assertEqual(snap["aggregate"]["state"], "fresh")
        self.assertTrue(snap["aggregate"]["complete"])
        self.assertEqual(snap["aggregate"]["reason"], "")
        self.assertAlmostEqual(snap["aggregate"]["age_days"], 1.0, delta=0.02)
        self.assertEqual(len(snap["sources"]), 1)
        self.assertEqual(snap["sources"][0]["source"], "mohurd")

    def test_aggregate_is_worst_and_reason_is_stable(self):
        self._set_source("mohurd", complete=True, days_ago=1)
        self._set_source("shzjw", complete=False, days_ago=1, reason="empty_page_before_total")
        snap = autosync._snap(self.store, state="fresh", source="mohurd")
        self.assertEqual(snap["aggregate"]["state"], "stale")
        self.assertTrue(snap["aggregate"]["stale"])
        self.assertFalse(snap["aggregate"]["complete"])
        self.assertEqual(snap["aggregate"]["reason"], "shzjw=empty_page_before_total")

    def test_aggregate_reason_empty_source_uses_unknown(self):
        self._set_source("mohurd", complete=True, days_ago=1)
        self._set_source("shzjw", complete=False, days_ago=1, reason="")
        snap = autosync._snap(self.store, state="fresh", source="mohurd")
        self.assertEqual(snap["aggregate"]["reason"], "shzjw=unknown")

    def test_per_source_cooldown(self):
        self._set_source("shzjw", complete=False, days_ago=1, reason="empty_page_before_total")
        old = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.set_meta("auto_sync_failed_at:shzjw", old)
        self.store.commit()
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="search", source="shzjw")
        self.assertEqual(snap["state"], "refresh_failed")
        self.assertGreater(snap["retry_after_seconds"], 0)

    def test_legacy_mohurd_cooldown_still_applies(self):
        self._set_source("mohurd", complete=False, days_ago=1)
        old = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.set_meta("auto_sync_failed_at", old)
        self.store.commit()
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="search", source="mohurd")
        self.assertEqual(snap["state"], "refresh_failed")


class StatusW2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)
        self.addCleanup(render.configure, as_json=False, quiet=False)

    def test_status_uses_constants_and_reports_future_corrupt(self):
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.set_meta("synced_at", future)
        self.store.set_meta("sync:mohurd:synced_at", future)
        self.store.commit()
        buf = io.StringIO()
        render.configure(as_json=True, quiet=True)
        with redirect_stdout(buf):
            rc = status.run(self.store, Namespace())
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertIsNone(payload["catalog_age_days"])
        self.assertTrue(payload["catalog_stale"])
        self.assertIn("未来时间戳", payload["catalog_meta_corrupt"])
        by_id = {s["source"]: s for s in payload["sources"]}
        self.assertIn("未来时间戳", by_id["mohurd"]["catalog_meta_corrupt"])
        self.assertTrue(by_id["mohurd"]["stale"])


if __name__ == "__main__":
    unittest.main()
