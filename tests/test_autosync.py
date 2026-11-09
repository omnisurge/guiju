"""进程内目录保鲜：新鲜不联网、过期刷一次、失败冷却、锁被占用要如实反馈。"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from guiju import autosync
from guiju.cli import render
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

    def _fake_crawl(self, calls: list):
        def crawl(store, **_kwargs):
            calls.append(1)
            store.upsert_announcement(
                "https://www.mohurd.gov.cn/new", "2026-03-03", "新增", "新增", "other"
            )
            self._synced(0)
            return {
                "new": 1,
                "total_in_db": store.count_announcements(),
                "scanned": 1,
                "total_reported": 1,
                "synced_at": "now",
                "latest_published_on": None,
                "db": str(store.path),
            }

        return crawl

    # ---- 不刷新的情形 ----

    def test_fresh_catalog_makes_no_requests(self):
        self._seed()
        self._synced(1)
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "fresh")
        self.assertEqual(snap["message"], "")

    def test_offline_reports_stale_not_silence(self):
        self._seed()
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="search", offline=True)
        self.assertEqual(snap["state"], "stale")
        self.assertIn("离线", snap["message"])

    def test_env_switch_reports_stale(self):
        self._seed()
        with (
            mock.patch.dict(os.environ, {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")),
        ):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "stale")
        self.assertIn("禁用", snap["message"])

    def test_unrelated_command_reports_stale(self):
        self._seed()
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="status")
        self.assertEqual(snap["state"], "stale")
        self.assertIn("不触发", snap["message"])

    # ---- 刷新的情形 ----

    def test_stale_catalog_refreshes_once(self):
        self._seed()
        self._synced(30)
        calls: list = []
        with mock.patch("guiju.syncengine.crawl", side_effect=self._fake_crawl(calls)):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(len(calls), 1)
        self.assertEqual(snap["state"], "refreshed")
        self.assertEqual(snap["new"], 1)
        self.assertIn("新目录", snap["message"])

    def test_empty_catalog_triggers_first_sync(self):
        calls: list = []
        with mock.patch("guiju.syncengine.crawl", side_effect=self._fake_crawl(calls)):
            snap = autosync.evaluate(self.store, command="pull")
        self.assertEqual(len(calls), 1)
        self.assertEqual(snap["state"], "refreshed")

    def test_failure_reports_and_cools_down(self):
        self._seed()
        with mock.patch("guiju.syncengine.crawl", side_effect=RuntimeError("boom")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refresh_failed")
        self.assertIn("刷新失败", snap["message"])
        self.assertEqual(snap["retry_after_seconds"], autosync.RETRY_AFTER_SECONDS)
        self.assertTrue(self.store.get_meta("auto_sync_failed_at"))

        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("冷却期内不该再试")):
            again = autosync.evaluate(self.store, command="search")
        self.assertEqual(again["state"], "refresh_failed")
        self.assertIn("冷却", again["message"])
        self.assertGreater(again["retry_after_seconds"], 0)

    def test_lock_held_reports_refreshing_with_retry_hint(self):
        self._seed()
        with mock.patch.object(autosync, "_try_lock") as fake_lock:
            fake_lock.return_value.__enter__.return_value = False
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refreshing")
        self.assertEqual(snap["retry_after_seconds"], 30)
        self.assertIn("重试", snap["message"])


class RenderInjectionTest(unittest.TestCase):
    """catalog 由 emit 统一注入 —— 任何命令都不能漏掉目录新鲜度。"""

    def tearDown(self):
        render.configure(as_json=False, quiet=False)
        render.set_catalog(None)

    def _emit(self, payload):
        import io
        import json
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            render.emit(payload)
        return json.loads(buf.getvalue())

    def test_injects_catalog(self):
        render.configure(as_json=True, quiet=True)
        render.set_catalog({"state": "fresh", "announcements": 3254})
        out = self._emit({"ok": True})
        self.assertEqual(out["catalog"]["state"], "fresh")
        self.assertTrue(out["ok"])

    def test_keeps_explicit_catalog(self):
        render.configure(as_json=True, quiet=True)
        render.set_catalog({"state": "fresh"})
        out = self._emit({"ok": True, "catalog": {"state": "refreshing"}})
        self.assertEqual(out["catalog"]["state"], "refreshing")

    def test_no_catalog_means_unchanged_payload(self):
        render.configure(as_json=True, quiet=True)
        out = self._emit({"ok": True})
        self.assertNotIn("catalog", out)


if __name__ == "__main__":
    unittest.main()
