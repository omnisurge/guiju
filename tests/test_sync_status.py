"""syncengine 目录抓取完整性、唯一 URL 计数、升级兼容、status 聚合与元数据损坏检测。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from guiju import autosync, syncengine
from guiju import report as render
from guiju.cli.commands import status
from guiju.cli.commands import sync as sync_mod
from guiju.sources import mohurd
from guiju.store.sqlite import SCHEMA_VERSION, Store, now


def _ts(days_ago: float = 0) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _item(n: int = 1, base: str = "https://www.mohurd.gov.cn/a/") -> dict:
    return {"url": base + str(n), "date": "2026-01-01", "title": f"标准公告{n}"}


class _FakeSource:
    """会数自己被抓了几页的假目录源。"""

    def __init__(self, pages, total=None):
        self._pages = dict(pages)
        self._total = total
        self.calls: list[int] = []

    def catalog_page(self, page_no, page_size):
        self.calls.append(page_no)
        items = self._pages.get(page_no) or []
        total = self._total if self._total is not None else sum(
            len(v) for v in self._pages.values() if v
        )
        return list(items), total


class _PagedSource:
    def __init__(self, pages: dict, total=None):
        self.pages = dict(pages)
        self.total = total
        self.calls: list[int] = []

    def catalog_page(self, page_no, page_size):
        self.calls.append(page_no)
        return list(self.pages.get(page_no, [])), self.total


class _RepeatSource:
    """分页参数失效：每一页都返回同一批条目。"""

    def __init__(self, items: list[dict], total=None, limit: int = 12):
        self.items = list(items)
        self.total = total
        self.limit = limit
        self.calls: list[int] = []

    def catalog_page(self, page_no, page_size):
        self.calls.append(page_no)
        if len(self.calls) > self.limit:
            # 防御：修复前没有重复页/页数上限逻辑，不能让它无限翻页把测试挂死。
            raise AssertionError("crawl 未在重复页/页数上限处停止")
        return list(self.items), self.total


class _SyncStatusCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GUIJU_NO_AUTO_SYNC", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)
        self.addCleanup(render.configure, as_json=False, quiet=False)

    def _seed(self, n: int = 1) -> None:
        for i in range(n):
            self.store.upsert_announcement(
                f"https://www.mohurd.gov.cn/x/{i}", "2026-01-01",
                f"标题{i}", f"名称{i}", "other",
            )
        self.store.commit()

    def _stamp(self, *, source: str | None = None, days_ago: float = 0.0,
               complete: bool | None = None) -> None:
        ts = _ts(days_ago)
        if source:
            self.store.set_meta(f"sync:{source}:synced_at", ts)
        else:
            self.store.set_meta("synced_at", ts)
        if complete is not None:
            self.store.set_meta(
                f"sync:{source or 'mohurd'}:complete", "1" if complete else "0"
            )
        self.store.commit()

    def _status_payload(self) -> dict:
        buf = io.StringIO()
        render.configure(as_json=True, quiet=True)
        with redirect_stdout(buf):
            rc = status.run(self.store, Namespace())
        self.assertEqual(rc, 0)
        return json.loads(buf.getvalue())


class CrawlCompleteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)

    def _crawl(self, src, **kw):
        with mock.patch("guiju.syncengine.registry", return_value={"fake": src}):
            return syncengine.crawl(self.store, source="fake", page_size=2, **kw)

    def _pages(self, start, stop):
        """第 start..stop 页，每页 2 条。"""
        return {p: [_item(p * 10), _item(p * 10 + 1)] for p in range(start, stop + 1)}

    def test_truncated_run_does_not_write_synced_at_or_complete(self):
        """--pages 截停的轮次：不写 synced_at、不置 catalog_complete（否则缺口被标新鲜）。"""
        src = _FakeSource(self._pages(1, 4))  # total=8, page_size=2 -> 需 4 页
        res = self._crawl(src, pages=1)
        self.assertEqual(src.calls, [1])
        self.assertTrue(res["truncated"])
        self.assertFalse(res["complete"])
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")
        self.assertNotEqual(self.store.get_meta("sync:fake:complete"), "1")

    def test_incomplete_catalog_does_not_quiet_stop(self):
        """缺口的增量轮不得静默早停：前 2 条已入库、catalog_complete 未置，
        旧实现第二页就停；修复后必须一直爬到缺口补齐。"""
        src = _FakeSource(self._pages(1, 4))
        for it in self._pages(1, 1)[1]:
            self.store.upsert_announcement(it["url"], it["date"], it["title"], it["title"], "other")
        self.store.commit()

        res = self._crawl(src)
        self.assertEqual(src.calls, [1, 2, 3, 4])   # 爬满，不是 [1, 2]
        self.assertEqual(res["scanned"], 8)
        self.assertTrue(res["complete"])
        self.assertEqual(self.store.get_meta("sync:fake:complete"), "1")
        self.assertNotEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_complete_catalog_allows_quiet_stop(self):
        """目录已完整后，增量轮允许连续两页 0 新增即停。"""
        src = _FakeSource(self._pages(1, 4))
        self._crawl(src)                            # 首轮爬到底
        self.assertEqual(self.store.get_meta("sync:fake:complete"), "1")

        src2 = _FakeSource(self._pages(1, 4))       # 目录不变
        res = self._crawl(src2)
        self.assertEqual(src2.calls, [1, 2])        # 两页 0 新增即停
        self.assertEqual(res["new"], 0)
        self.assertTrue(res["complete"])
        self.assertNotEqual(self.store.get_meta("sync:fake:synced_at"), "")  # 早停也刷新

    def test_truncate_then_next_run_recovers_gap(self):
        """截断轮之后的下一轮：不带 pages 限制时仍爬到底并置完整。"""
        src = _FakeSource(self._pages(1, 4))
        self._crawl(src, pages=1)
        res = self._crawl(src)
        self.assertTrue(res["complete"])
        self.assertEqual(self.store.get_meta("sync:fake:complete"), "1")

    def test_empty_page_before_total_is_incomplete(self):
        """空页不再直接 complete：total=10、只见到 2 条时是缺口。"""
        src = _FakeSource({1: [_item(1), _item(2)], 2: []}, total=10)
        res = self._crawl(src)
        self.assertEqual(src.calls, [1, 2])
        self.assertFalse(res["complete"])
        self.assertEqual(res["complete_reason"], "empty_page_before_total")
        self.assertEqual(self.store.get_meta("sync:fake:complete"), "0")
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_page_size_99_uses_actual_seen_not_page_times_size(self):
        """站点每页 99（请求 100）时，page*page_size 会虚高；必须按 seen 判断。"""
        pages = {
            1: [_item(n) for n in range(1, 100)],
            2: [_item(n) for n in range(100, 199)],
            3: [_item(199)],
        }
        src = _FakeSource(pages, total=199)
        res = self._crawl(src)
        self.assertEqual(src.calls, [1, 2, 3])
        self.assertEqual(res["scanned"], 199)
        self.assertTrue(res["complete"])
        self.assertEqual(res["complete_reason"], "seen_ge_total")

    def test_empty_catalog_total_zero_is_complete(self):
        src = _FakeSource({}, total=0)
        res = self._crawl(src)
        self.assertTrue(res["complete"])
        self.assertEqual(res["complete_reason"], "empty_catalog")
        self.assertEqual(self.store.get_meta("sync:fake:total"), "0")
        self.assertEqual(self.store.get_meta("sync:fake:seen"), "0")

    def test_items_with_total_zero_then_empty_is_incomplete(self):
        """计数元素失效（total=0）但已见到条目：空页不能证明空目录，必须不完整。"""
        src = _FakeSource({1: [_item(1)], 2: []}, total=0)
        res = self._crawl(src)
        self.assertFalse(res["complete"])
        self.assertEqual(res["complete_reason"], "total_unknown")
        self.assertEqual(self.store.get_meta("sync:fake:complete"), "0")
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_source_complete_is_isolated(self):
        """alpha 完整不能把 beta 授权成完整；全局 catalog_complete 不再替所有源背书。"""
        alpha = _FakeSource({1: [_item(1)]}, total=1)
        beta = _FakeSource({1: [_item(2)]}, total=1)
        with mock.patch("guiju.syncengine.registry", return_value={"alpha": alpha, "beta": beta}):
            syncengine.crawl(self.store, source="alpha", page_size=2)
        self.assertTrue(self.store.source_meta("alpha")["complete"])
        self.assertFalse(self.store.source_meta("beta")["complete"])
        self.assertEqual(self.store.get_meta("catalog_complete"), "")


class MohurdCatalogPageTest(unittest.TestCase):
    def _call(self, fragment):
        payload = {"success": True, "data": {"html": fragment}}
        fake_client = mock.Mock()
        fake_client.get_page.return_value = mock.Mock(text=json.dumps(payload))
        with mock.patch.object(mohurd, "_body_client", return_value=fake_client):
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
        fake = _PagedSource({1: []}, total=None)
        result = self._crawl("fake", fake)
        self.assertFalse(result["complete"])
        self.assertEqual(result["complete_reason"], "total_unknown")
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_mohurd_does_not_write_legacy_complete(self):
        fake = _PagedSource({1: [_item()]}, total=1)
        result = self._crawl("mohurd", fake)
        self.assertTrue(result["complete"])
        self.assertEqual(self.store.get_meta("sync:mohurd:complete"), "1")
        self.assertEqual(self.store.get_meta("catalog_complete"), "")
        self.assertNotEqual(self.store.get_meta("sync:mohurd:synced_at"), "")


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


class UpgradeFromV110Test(_SyncStatusCase):
    """v1.1.0 升级库：只有全局 synced_at，两个 complete 键都缺。"""

    def setUp(self):
        super().setUp()
        self._seed()
        self._stamp(days_ago=0)
        self.crawl_calls: list[int] = []

    def _fake_crawl(self, source: str):
        def crawl(store, **_kwargs):
            self.crawl_calls.append(1)
            store.set_meta(f"sync:{source}:complete", "1")
            store.set_meta(f"sync:{source}:synced_at", _ts(0))
            store.commit()
            return {"new": 0, "total_in_db": store.count_announcements()}
        return crawl

    def test_autosync_forces_one_complete_sync(self):
        with mock.patch("guiju.syncengine.crawl", side_effect=self._fake_crawl("mohurd")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(len(self.crawl_calls), 1)
        self.assertEqual(snap["state"], "refreshed")

    def test_sync_if_stale_does_not_skip(self):
        args = Namespace(if_stale=7, source="mohurd", page_size=100, pages=0)
        with mock.patch.object(
            sync_mod, "crawl", side_effect=self._fake_crawl("mohurd")
        ) as crawl:
            rc = sync_mod.run(self.store, args)
        self.assertEqual(rc, 0)
        self.assertEqual(crawl.call_count, 1)

    def test_status_top_level_is_stale_and_age_unknown(self):
        payload = self._status_payload()
        self.assertTrue(payload["catalog_stale"])
        self.assertIsNone(payload["catalog_age_days"])
        self.assertIn("未完整", payload["catalog_message"])
        by_id = {s["source"]: s for s in payload["sources"]}
        self.assertFalse(by_id["mohurd"]["complete"])
        self.assertTrue(by_id["mohurd"]["stale"])

    def test_legacy_catalog_complete_one_is_trusted(self):
        self.store.set_meta("catalog_complete", "1")
        self.store.commit()
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "fresh")

        buf = io.StringIO()
        render.configure(as_json=True, quiet=True)
        args = Namespace(if_stale=7, source="mohurd", page_size=100, pages=0)
        with mock.patch.object(sync_mod, "crawl") as crawl, redirect_stdout(buf):
            rc = sync_mod.run(self.store, args)
        self.assertEqual(rc, 0)
        crawl.assert_not_called()
        self.assertTrue(json.loads(buf.getvalue())["skipped"])

    def test_non_mohurd_missing_complete_crawls(self):
        self._stamp(source="shzjw", days_ago=0)
        with mock.patch("guiju.syncengine.crawl", side_effect=self._fake_crawl("shzjw")):
            snap = autosync.evaluate(self.store, command="pull", source="shzjw")
        self.assertEqual(len(self.crawl_calls), 1)
        self.assertEqual(snap["state"], "refreshed")
        self.assertEqual(snap["source"], "shzjw")

    def test_disabled_or_offline_reports_stale_not_fresh(self):
        with (
            mock.patch.dict(os.environ, {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")),
        ):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "stale")
        self.assertIn("未标记完整", snap["message"])
        with mock.patch("guiju.syncengine.crawl", side_effect=AssertionError("不该联网")):
            snap = autosync.evaluate(self.store, command="search", offline=True)
        self.assertEqual(snap["state"], "stale")


class StatusAggregateTest(_SyncStatusCase):
    def test_two_known_ages_with_one_incomplete_has_no_age(self):
        self._seed()
        self._stamp(source="mohurd", days_ago=1, complete=True)
        self._stamp(source="shzjw", days_ago=2, complete=False)
        payload = self._status_payload()
        self.assertTrue(payload["catalog_stale"])
        self.assertIsNone(payload["catalog_age_days"])
        self.assertIn("未完整", payload["catalog_message"])

    def test_all_complete_uses_oldest_age(self):
        self._seed()
        self._stamp(source="mohurd", days_ago=1, complete=True)
        self._stamp(source="shzjw", days_ago=9, complete=True)
        payload = self._status_payload()
        self.assertTrue(payload["catalog_stale"])
        self.assertAlmostEqual(payload["catalog_age_days"], 9.0, delta=0.05)
        self.assertIn("未更新", payload["catalog_message"])

    def test_all_complete_and_fresh_is_not_stale(self):
        self._seed()
        self._stamp(source="mohurd", days_ago=1, complete=True)
        self._stamp(source="shzjw", days_ago=2, complete=True)
        payload = self._status_payload()
        self.assertFalse(payload["catalog_stale"])
        self.assertAlmostEqual(payload["catalog_age_days"], 2.0, delta=0.05)
        self.assertEqual(payload["catalog_message"], "")

    def test_top_level_corrupt_from_any_source(self):
        self._seed()
        self.store.set_meta("synced_at", _ts(0))
        self.store.set_meta("sync:mohurd:synced_at", "垃圾值")
        self.store.set_meta("sync:shzjw:synced_at", _ts(0))
        self.store.commit()
        payload = self._status_payload()
        self.assertIn("损坏", payload["catalog_meta_corrupt"])
        self.assertTrue(payload["catalog_stale"])


class UniqueUrlCrawlTest(_SyncStatusCase):
    def _crawl(self, source: str, fake, **kwargs):
        page_size = kwargs.pop("page_size", 2)
        with mock.patch("guiju.syncengine.registry", return_value={source: fake}):
            return syncengine.crawl(
                self.store, source=source, page_size=page_size, **kwargs
            )

    def test_same_page_duplicate_url_is_counted_once(self):
        item = _item(1)
        fake = _PagedSource({1: [dict(item), dict(item)], 2: []}, total=2)
        result = self._crawl("fake", fake)
        self.assertFalse(result["complete"])
        self.assertEqual(result["scanned"], 1)
        self.assertEqual(result["total_in_db"], 1)
        self.assertEqual(self.store.get_meta("sync:fake:seen"), "1")
        self.assertEqual(self.store.get_meta("sync:fake:complete"), "0")
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_cross_page_duplicates_do_not_fake_completeness(self):
        page = [_item(1), _item(2)]
        fake = _PagedSource({1: page, 2: page, 3: []}, total=4)
        result = self._crawl("fake", fake)
        self.assertFalse(result["complete"])
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(fake.calls, [1, 2, 3])
        self.assertEqual(self.store.count_announcements(), 2)
        self.assertEqual(self.store.get_meta("sync:fake:complete"), "0")
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_prior_complete_duplicate_page_is_not_quiet_stop(self):
        self.store.set_meta("sync:fake:complete", "1")
        self.store.commit()
        page = [_item(1), _item(2)]
        fake = _PagedSource({1: page, 2: page, 3: []}, total=4)
        result = self._crawl("fake", fake)
        self.assertFalse(result["complete"])
        self.assertEqual(result["complete_reason"], "empty_page_before_total")

    def test_duplicate_page_then_new_page_still_completes(self):
        fake = _PagedSource(
            {1: [_item(1), _item(2)], 2: [_item(1), _item(2)], 3: [_item(3), _item(4)]},
            total=4,
        )
        result = self._crawl("fake", fake)
        self.assertTrue(result["complete"])
        self.assertEqual(result["complete_reason"], "seen_ge_total")
        self.assertEqual(result["scanned"], 4)
        self.assertEqual(result["total_in_db"], 4)

    def test_endless_duplicate_pages_stop_with_duplicate_page(self):
        fake = _RepeatSource([_item(1), _item(2)], total=10)
        result = self._crawl("fake", fake)
        self.assertFalse(result["complete"])
        self.assertEqual(result["complete_reason"], "duplicate_page")
        self.assertEqual(fake.calls, [1, 2, 3, 4])
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_unknown_total_duplicate_pages_stop_no_progress(self):
        fake = _RepeatSource([_item(1), _item(2)], total=None)
        result = self._crawl("fake", fake)
        self.assertFalse(result["complete"])
        self.assertEqual(result["complete_reason"], "no_progress")
        self.assertEqual(fake.calls, [1, 2, 3])
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_page_limit_truncates_without_completeness(self):
        pages = {p: [_item(p)] for p in range(1, 205)}
        fake = _PagedSource(pages, total=None)
        result = self._crawl("fake", fake, page_size=1)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["complete_reason"], "page_limit")
        self.assertEqual(len(fake.calls), syncengine.MAX_PAGES)
        self.assertEqual(self.store.get_meta("sync:fake:synced_at"), "")

    def test_on_page_and_meta_use_unique_counts(self):
        seen: list[tuple] = []
        fake = _PagedSource({1: [_item(1), dict(_item(1)), _item(2)], 2: []}, total=2)
        result = self._crawl(
            "fake", fake,
            on_page=lambda page, count, fresh, seen_n, total: seen.append(
                (page, count, fresh, seen_n, total)
            ),
        )
        self.assertEqual(seen[0][1], 3)   # 本页原始条目数
        self.assertEqual(seen[0][3], 2)   # 唯一 URL 数
        self.assertEqual(result["scanned"], 2)
        self.assertEqual(self.store.get_meta("sync:fake:seen"), "2")
        self.assertTrue(result["complete"])

    def test_normal_unique_pages_complete(self):
        fake = _PagedSource({1: [_item(1), _item(2)], 2: [_item(3)]}, total=3)
        result = self._crawl("fake", fake)
        self.assertTrue(result["complete"])
        self.assertEqual(result["scanned"], 3)
        self.assertEqual(result["total_in_db"], 3)


class TestCatalogMetaCorrupt(unittest.TestCase):
    """synced_at 解析失败要和'没同步过'区分开。"""

    def test_corrupt_synced_at_signalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "c.db"))
            try:
                store.set_meta("synced_at", "垃圾值")
                store.commit()
                self.assertIsNone(store.catalog_age_days())
                corrupt = store.catalog_meta_corrupt()
                self.assertIsNotNone(corrupt)
                self.assertIn("损坏", corrupt)
            finally:
                store.close()

    def test_no_meta_not_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "c.db"))
            try:
                self.assertIsNone(store.catalog_age_days())
                self.assertIsNone(store.catalog_meta_corrupt())
            finally:
                store.close()

    def test_valid_meta_not_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "c.db"))
            try:
                store.set_meta("synced_at", "2026-11-01 10:00:00")
                store.commit()
                self.assertIsNotNone(store.catalog_age_days())
                self.assertIsNone(store.catalog_meta_corrupt())
            finally:
                store.close()


class TestSyncIfStaleCountsOnce(unittest.TestCase):
    """sync --if-stale 命中新鲜目录时 count_announcements 只查一次。"""

    def test_single_count_call(self):
        # render 是进程级单例：别的测试若留下 as_json=True，MagicMock 字段会被
        # json.dumps 撞成 TypeError。这里与执行顺序解耦，只验证 count 只查一次。
        render.configure(as_json=False, quiet=True)
        self.addCleanup(render.configure, as_json=False, quiet=False)

        args = mock.MagicMock()
        args.if_stale = 7
        store = mock.MagicMock()
        store.count_announcements.return_value = 3254
        store.catalog_age_days.return_value = 0.5
        store.get_meta.return_value = "2026-11-12 09:00:00"
        store.path = Path("/tmp/x.db")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = sync_mod.run(store, args)
        self.assertEqual(rc, 0)
        self.assertEqual(store.count_announcements.call_count, 1)


if __name__ == "__main__":
    unittest.main()
