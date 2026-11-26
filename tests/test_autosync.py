"""进程内目录保鲜：新鲜不联网、过期刷一次、失败冷却、锁被占用要如实反馈、W2 按源聚合。"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from guiju import autosync
from guiju import report as render
from guiju.cli.commands import status
from guiju.errors import NeedsHuman
from guiju.store.sqlite import Store


class AutoSyncTest(unittest.TestCase):
    def setUp(self):
        # 测试必须与调用方 shell 的 GUIJU_NO_AUTO_SYNC 隔离；否则整个套件
        # 在"自动刷新已禁用"的父环境下会把刷新分支全部测成 stale。
        self._env_guard = mock.patch.dict(os.environ, {}, clear=False)
        self._env_guard.start()
        self.addCleanup(self._env_guard.stop)
        os.environ.pop("GUIJU_NO_AUTO_SYNC", None)
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

    def _synced(self, days_ago: float, *, complete: bool | None = None) -> None:
        # 只写全局 synced_at 的形态 = v1.1.0 升级库（没有完成证据），不能当 fresh；
        # 要构造"新鲜且完整"必须同时给 per-source complete（complete=True）。
        ts = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.set_meta("synced_at", ts)
        if complete is not None:
            self.store.set_meta("sync:mohurd:complete", "1" if complete else "0")
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

    def test_lock_open_failure_non_strict_degrades_to_none(self):
        with mock.patch("builtins.open", side_effect=PermissionError(13, "denied")), \
                autosync._try_lock(self.store) as locked:
            self.assertIsNone(locked)

    def test_safe_sync_open_failure_raises_needs_human(self):
        with mock.patch("builtins.open", side_effect=PermissionError(13, "denied")), \
                self.assertRaises(NeedsHuman) as ctx, autosync.safe_sync(self.store):
            pass
        self.assertEqual(ctx.exception.exit, 50)

    def test_lock_open_failure_reports_refresh_failed(self):
        self._seed()
        with mock.patch("builtins.open", side_effect=PermissionError(13, "denied")), \
                mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refresh_failed")
        self.assertIn("同步锁文件", snap["message"])

    # ---- 不刷新的情形 ----

    def test_fresh_catalog_makes_no_requests(self):
        self._seed()
        self._synced(1, complete=True)
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

    def _synced_source(self, source: str, days_ago: float = 0, complete: bool = True):
        ts = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
        self.store.set_meta(f"sync:{source}:synced_at", ts)
        self.store.set_meta(f"sync:{source}:complete", "1" if complete else "0")
        self.store.commit()

    def test_other_source_complete_does_not_authorize_shzjw(self):
        """mohurd 的新鲜/完整不能把 shzjw 授权成 fresh。"""
        self._seed()
        self._synced(1)
        calls: list = []
        with mock.patch("guiju.syncengine.crawl", side_effect=self._fake_crawl(calls)):
            snap = autosync.evaluate(self.store, command="pull", source="shzjw")
        self.assertEqual(len(calls), 1)
        self.assertEqual(snap["state"], "refreshed")
        self.assertEqual(snap["source"], "shzjw")

    def test_shzjw_complete_and_fresh_makes_no_requests(self):
        self._seed()
        self._synced_source("shzjw", 1, complete=True)
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="pull", source="shzjw")
        self.assertEqual(snap["state"], "fresh")
        self.assertEqual(snap["source"], "shzjw")

    def test_mohurd_fresh_ignores_incomplete_shzjw(self):
        self._seed()
        self._synced(1, complete=True)
        self._synced_source("shzjw", 1, complete=False)
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "fresh")
        self.assertEqual(snap["source"], "mohurd")

    def test_legacy_global_incomplete_triggers_refresh(self):
        """旧全局 catalog_complete=0 且无 per-source 键：仍必须刷新，不能被 synced_at 骗成 fresh。"""
        self._seed()
        self._synced(1)
        self.store.set_meta("catalog_complete", "0")
        self.store.commit()
        calls: list = []
        with mock.patch("guiju.syncengine.crawl", side_effect=self._fake_crawl(calls)):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(len(calls), 1)
        self.assertEqual(snap["state"], "refreshed")

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

    def setUp(self):
        # render 是全局单例；其他测试类可能留下 catalog，反向顺序跑会污染断言。
        render.set_catalog(None)

    def tearDown(self):
        render.configure(as_json=False, quiet=False)
        render.set_catalog(None)

    def _emit(self, payload):
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


class AutosyncFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = Store(f"{self.tmp.name}/catalog.db")
        self.addCleanup(self.store.close)

    def test_cooldown_write_failure_does_not_escape(self):
        with mock.patch("guiju.syncengine.crawl", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.store, "set_meta", side_effect=sqlite3.OperationalError("readonly")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refresh_failed")
        self.assertIn("目录刷新失败", snap["message"])

    def test_commit_failure_does_not_escape(self):
        with mock.patch("guiju.syncengine.crawl", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.store, "commit", side_effect=sqlite3.OperationalError("disk full")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refresh_failed")

    def test_clear_failure_does_not_block_refreshed(self):
        with mock.patch("guiju.syncengine.crawl", return_value={"new": 3}), \
                mock.patch.object(self.store, "set_meta", side_effect=sqlite3.OperationalError("readonly")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refreshed")
        self.assertEqual(snap["new"], 3)

    def test_clear_commit_failure_does_not_block_refreshed(self):
        with mock.patch("guiju.syncengine.crawl", return_value={"new": 3}), \
                mock.patch.object(self.store, "commit", side_effect=sqlite3.OperationalError("disk full")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refreshed")


class AutosyncNotifyTest(unittest.TestCase):
    def setUp(self) -> None:
        # 与 AutoSyncTest 同因：父 shell 的 GUIJU_NO_AUTO_SYNC 不能改变单测分支。
        self._env_guard = mock.patch.dict(os.environ, {}, clear=False)
        self._env_guard.start()
        self.addCleanup(self._env_guard.stop)
        os.environ.pop("GUIJU_NO_AUTO_SYNC", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_autosync_notify_uses_fresh_empty(self) -> None:
        store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(store.close)
        # 锁外判定时空目录；锁内复查已被"别的进程"补满 → 提示应为增量而非首次同步
        counts = iter([0, 5, 5, 5, 5])
        msgs: list[str] = []
        with mock.patch.object(store, "count_announcements", lambda: next(counts)), \
             mock.patch.object(store, "catalog_age_days", lambda: 9.5), \
             mock.patch("guiju.syncengine.crawl", lambda s: {"new": 2}):
            autosync.evaluate(store, command="search", notify=msgs.append)
        self.assertTrue(msgs)
        self.assertIn("增量同步", msgs[0])
        self.assertNotIn("首次同步", msgs[0])


class AutoSyncW2Test(unittest.TestCase):
    def setUp(self):
        # 每个用例清掉宿主/外层测试可能带进来的关闭开关，避免 autosync 不执行。
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GUIJU_NO_AUTO_SYNC", None)
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

    def test_aggregate_honors_current_stale_state(self):
        self._set_source("mohurd", complete=True, days_ago=1)
        self._set_source("shzjw", complete=True, days_ago=1)
        snap = autosync._snap(self.store, state="stale", source="mohurd")
        self.assertEqual(snap["state"], "stale")
        self.assertTrue(snap["aggregate"]["stale"])
        self.assertEqual(snap["aggregate"]["state"], "stale")
        current = next(
            item for item in snap["sources_all"] if item["source"] == "mohurd"
        )
        self.assertTrue(current["stale"])

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


if __name__ == "__main__":
    unittest.main()
