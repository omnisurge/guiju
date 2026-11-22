"""1.2.0a4 W2-1/W2-2 回归：完成证据、status 顶层聚合、唯一 URL 计数、重复页。

这些用例按修复前缺陷编写：在 f98e0d2（1.2.0a3）上应当失败。
W2-1：v1.1.0 升级库没有 per-source/legacy complete，不能凭 synced_at 年龄当 fresh；
W2-2：完成判定必须按唯一 URL 计数，重复页不能把缺口标成 complete。
"""

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
from guiju.cli import render
from guiju.cli.commands import status
from guiju.cli.commands import sync as sync_mod
from guiju.store.sqlite import Store


def _ts(days_ago: float = 0) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _item(n: int, base: str = "https://www.mohurd.gov.cn/a/") -> dict:
    return {"url": base + str(n), "date": "2026-01-01", "title": f"标准公告{n}"}


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


class _StoreCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GUIJU_NO_AUTO_SYNC", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)
        self.addCleanup(render.configure, as_json=False, quiet=True)

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


class UpgradeFromV110Test(_StoreCase):
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
        # sync.py 是 from ...syncengine import crawl，必须 patch 命令模块里的绑定，
        # 否则会真的走到 registry 去联网抓目录。
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
        # 旧库显式写了 catalog_complete=1 就是完整证据，不能被升级库场景连带强制重抓。
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


class StatusAggregateTest(_StoreCase):
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


class UniqueUrlCrawlTest(_StoreCase):
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


if __name__ == "__main__":
    unittest.main()
