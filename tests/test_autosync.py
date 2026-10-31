"""进程内目录保鲜：新鲜不联网、过期刷新一次、失败后冷却（不依赖任何定时任务）。"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from guiju import autosync
from guiju.store.sqlite import Store


class AutoSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)

    def _seed(self, n: int = 1) -> None:
        for i in range(n):
            self.store.upsert_announcement(
                f"https://www.mohurd.gov.cn/x/{i}", "2026-01-01", f"标准{i}", f"标准{i}", "other"
            )
        self.store.commit()

    def _synced(self, days_ago: float) -> None:
        ts = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.set_meta("synced_at", ts)
        self.store.commit()

    def test_fresh_catalog_makes_no_requests(self):
        self._seed()
        self._synced(1)
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            self.assertIsNone(autosync.maybe_refresh(self.store, command="search"))

    def test_offline_never_refreshes(self):
        self._seed()
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            self.assertIsNone(autosync.maybe_refresh(self.store, command="search", offline=True))

    def test_env_switch_disables(self):
        self._seed()
        with (
            mock.patch.dict(os.environ, {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")),
        ):
            self.assertIsNone(autosync.maybe_refresh(self.store, command="search"))

    def test_unrelated_command_never_refreshes(self):
        self._seed()
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            self.assertIsNone(autosync.maybe_refresh(self.store, command="verify"))

    def test_stale_catalog_refreshes_once(self):
        self._seed()
        self._synced(30)
        calls = []

        def fake_crawl(store, **_kwargs):
            calls.append(1)
            self._synced(0)
            return {
                "new": 1,
                "total_in_db": 2,
                "scanned": 1,
                "total_reported": 2,
                "synced_at": "now",
                "latest_published_on": None,
                "db": str(store.path),
            }

        with mock.patch("guiju.syncengine.crawl", side_effect=fake_crawl):
            note = autosync.maybe_refresh(self.store, command="search")
        self.assertEqual(len(calls), 1)
        self.assertIn("目录已更新", note or "")

    def test_failure_sets_cooldown(self):
        self._seed()
        with mock.patch("guiju.syncengine.crawl", side_effect=RuntimeError("boom")):
            note = autosync.maybe_refresh(self.store, command="search")
        self.assertIn("刷新失败", note or "")
        self.assertTrue(self.store.get_meta("auto_sync_failed_at"))
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("冷却期内不该再试")):
            self.assertIsNone(autosync.maybe_refresh(self.store, command="search"))

    def test_empty_catalog_triggers_first_sync(self):
        calls = []

        def fake_crawl(store, **_kwargs):
            calls.append(1)
            store.upsert_announcement("https://www.mohurd.gov.cn/y", "2026-02-02", "甲", "甲", "other")
            self._synced(0)
            return {"new": 1, "total_in_db": 1, "scanned": 1, "total_reported": 1,
                    "synced_at": "now", "latest_published_on": None, "db": str(store.path)}

        with mock.patch("guiju.syncengine.crawl", side_effect=fake_crawl):
            note = autosync.maybe_refresh(self.store, command="pull")
        self.assertEqual(len(calls), 1)
        self.assertIn("新增 1 条", note or "")


if __name__ == "__main__":
    unittest.main()
