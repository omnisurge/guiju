"""fix_20261112 回归：syncengine 静默停爬掩盖缺口 + Throttle/RedirectGuard 节流并发/重定向缺口。

两个修复的公共语义：任何"看起来差不多"的提前退出都必须有完整目录佐证，
任何"换 host"的出站都必须重新计节拍。
"""

import itertools
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from guiju import syncengine
from guiju.net.block import BlockDetector as _BD
from guiju.net.policy import BodyPolicy as _BP
from guiju.net.redirect import RedirectGuard
from guiju.net.throttle import Throttle, default_throttle
from guiju.store.sqlite import Store


class _FakeSource:
    """会数自己被抓了几页的假目录源。"""

    def __init__(self, pages, total=None):
        # pages: 1-based 页号 -> [{url,date,title}]，None 表示无此页（返回空）
        self._pages = dict(pages)
        self._total = total
        self.calls = []

    def catalog_page(self, page_no, page_size):
        self.calls.append(page_no)
        items = self._pages.get(page_no) or []
        total = self._total if self._total is not None else sum(
            len(v) for v in self._pages.values() if v
        )
        return list(items), total


def _item(n, base="https://www.mohurd.gov.cn/a/"):
    return {"url": base + str(n), "date": "2026-01-01", "title": "标准公告" + str(n)}


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


class ThrottleTest(unittest.TestCase):
    def test_throttle_lock_exists(self):
        """节流读-睡-写必须由锁互斥（并发绕过修复的存在性验证）。"""
        self.assertIsInstance(default_throttle()._lock, type(threading.Lock()))

    def test_concurrent_throttle_same_host_serializes(self):
        """同 host 并发请求必须串行间隔 >= 阈值，不能齐刷刷同刻通过。"""
        interval = 0.05
        throttle = Throttle(min_interval=interval)
        stamps = []
        gate = threading.Barrier(5)

        def worker():
            gate.wait()
            throttle.wait("parallel.gov.cn")
            stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stamps.sort()
        gaps = [b - a for a, b in itertools.pairwise(stamps)]
        self.assertTrue(
            all(g >= interval * 0.8 for g in gaps),
            "并发同 host 的节流间隔应互斥展开，实测 gaps=" + repr(gaps),
        )

    def test_redirect_target_host_is_throttled(self):
        """白名单过关后，重定向目标 host 也必须先过 Throttle.wait 再放行。"""
        req = urllib.request.Request("https://a.gov.cn/x")
        throttle = mock.Mock()
        handler = RedirectGuard(_BP(), throttle, _BD())
        handler.redirect_request(
            req, None, 302, "Found", {}, "https://b.gov.cn/y"
        )
        throttle.wait.assert_called_once_with("b.gov.cn")

    def test_redirect_same_host_is_throttled(self):
        """同 host 重定向照样再计一拍（限速按请求数，不按 host 去重）。"""
        req = urllib.request.Request("https://a.gov.cn/x")
        throttle = mock.Mock()
        handler = RedirectGuard(_BP(), throttle, _BD())
        handler.redirect_request(
            req, None, 302, "Found", {}, "/x2"
        )
        throttle.wait.assert_called_once_with("a.gov.cn")


if __name__ == "__main__":
    unittest.main()
