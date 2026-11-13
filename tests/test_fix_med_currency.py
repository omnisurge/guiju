"""Medium 修复项回归测试：currency + ccsn 域（M1/M11/M12b/M18/M19/M20/M22；原 M7 mcp 测试随 spec-currency 退役移除）。

全部离线：不联网、不读真实凭据；用假 HttpClient / 打桩 urlopen。
"""

import json
import unittest
import warnings
from unittest import mock

from guiju.currency import CachedCurrency, _cache, endpoint_of
from guiju.currency.cache import CACHE_NAMESPACE, DEFAULT_STALE_LIMIT, DEFAULT_TTL, CacheEntry
from guiju.currency.ccsn import SEARCH_URL, CcsnCurrency, status_ccsn
from guiju.currency.model import QueryCancelled, Record
from guiju.currency.native import NativeCurrency
from guiju.currency.registry import Registry, Route
from guiju.errors import SourceUnreachable

CODE = "GB 1-2000"
NOW = 10_000_000.0


def _record(code=CODE, source="p", status="current"):
    return Record(code, status, "标准", "2000-01-01", source, publish_date="1999-01-01", raw="证据")


class Stub:
    def __init__(self, name="p", rows=None, error=None):
        self.name, self.error = name, error
        self.rows = [] if rows is None else rows
        self.calls = 0

    def lookup(self, code):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


class MemoryCache:
    def __init__(self, clock, ttl=DEFAULT_TTL):
        self.clock, self.ttl = clock, ttl
        self.entries = {}
        self.calls = []
        self.fail = None

    def get(self, code, *, namespace=CACHE_NAMESPACE):
        self.calls.append("get")
        if self.fail:
            raise self.fail
        entry = self.entries.get((namespace, code))
        return entry if entry is not None and entry.fresh(self.ttl, self.clock()) else None

    def get_any(self, code, *, namespace=CACHE_NAMESPACE):
        self.calls.append("get_any")
        if self.fail:
            raise self.fail
        return self.entries.get((namespace, code))

    def put(self, code, records, *, namespace=CACHE_NAMESPACE):
        self.calls.append("put")
        if self.fail:
            raise self.fail
        self.entries[namespace, code] = CacheEntry(self.clock(), tuple(records))


def _make_native(now_holder, source=None, cache=None, **kwargs):
    source = source if source is not None else Stub(rows=[_record()])
    registry = Registry()
    registry.register(Route("p", ("GB",), lambda: source))
    cache = cache if cache is not None else MemoryCache(lambda: now_holder[0])
    native = NativeCurrency(registry, cache, clock=lambda: now_holder[0], **kwargs)
    namespace = registry.namespace_for(CODE, source)
    return native, source, cache, namespace


# ---------- M1：缓存后端故障不破环查询，QueryCancelled/控制流不被吞 ----------

class M1CacheBackendResilienceTest(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]

    def test_get_raises_valueerror_falls_through_to_upstream(self):
        cache = MemoryCache(lambda: self.now[0])
        cache.fail = ValueError("corrupt backend")
        native, source, _, _ = _make_native(self.now, cache=cache)
        outcome = native.query(CODE)
        self.assertEqual(outcome.results, (_record(),))
        self.assertEqual(source.calls, 1)

    def test_put_raises_does_not_eat_success(self):
        cache = MemoryCache(lambda: self.now[0])
        def flaky_get(code, *, namespace=CACHE_NAMESPACE):
            return None
        def flaky_put(code, records, *, namespace=CACHE_NAMESPACE):
            raise OSError("disk full")
        cache.get = flaky_get
        cache.put = flaky_put
        native, _source, _, _ = _make_native(self.now, cache=cache)
        outcome = native.query(CODE)
        self.assertEqual(outcome.results, (_record(),))
        self.assertFalse(outcome.from_cache)

    def test_querycancelled_inside_cache_read_still_propagates(self):
        cache = MemoryCache(lambda: self.now[0])
        cache.get = lambda code, *, namespace=CACHE_NAMESPACE: (_ for _ in ()).throw(
            QueryCancelled("cancelled"))
        native, _, _, _ = _make_native(self.now, cache=cache)
        with self.assertRaises(QueryCancelled):
            native.query(CODE)


# ---------- M22：admit 入口一次 + put 前复验，不随每个 guard 重复计费 ----------

class M22AdmitAccountingTest(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]
        self.admits = []

    def _native(self, **kw):
        return _make_native(self.now, admit=self.admits.append, **kw)

    def test_fresh_miss_admits_at_most_twice(self):
        native, _source, _cache, _ = self._native()
        native.query(CODE)
        self.assertLessEqual(len(self.admits), 2)
        self.assertGreaterEqual(len(self.admits), 1)

    def test_ttl_hit_admits_once_and_never_requeries(self):
        native, source, cache, namespace = self._native()
        cache.entries[namespace, CODE] = CacheEntry(self.now[0] - 5, (_record(),))
        outcome = native.query(CODE)
        self.assertTrue(outcome.from_cache)
        self.assertEqual(len(self.admits), 1)
        self.assertEqual(source.calls, 0)

    def test_pre_cancelled_never_admits_nor_reads_cache(self):
        native, source, cache, _ = self._native(cancelled=lambda: True)
        with self.assertRaises(QueryCancelled):
            native.query(CODE)
        self.assertEqual(self.admits, [])
        self.assertEqual(cache.calls, [])
        self.assertEqual(source.calls, 0)

    def test_admit_rejection_before_put_means_no_write(self):
        calls = {"n": 0}
        def admit(code):
            calls["n"] += 1
            self.admits.append(code)
            if calls["n"] > 1:
                raise SourceUnreachable("route revoked mid-query")
        native, _source, cache, _ = _make_native(
            self.now, admit=admit)
        with self.assertRaises(SourceUnreachable):
            native.query(CODE)
        self.assertEqual(cache.calls.count("put"), 0)


# ---------- M11：进程级 memo 重放完整 flag，且不遮蔽取消 ----------

class M11ProcessMemoTest(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]

    def _no_cache_native(self, source=None, **kw):
        # 有缓存时 TTL 命中本身就是去重；memo 只在无缓存后端时补位。
        # 注意不能走 _make_native：helper 会把 None 又补成 MemoryCache。
        source = source if source is not None else Stub(rows=[_record()])
        registry = Registry()
        registry.register(Route("p", ("GB",), lambda: source))
        native = NativeCurrency(registry, None, clock=lambda: self.now[0], **kw)
        return native, source, None, None

    def test_second_query_without_cache_is_memo_hit(self):
        native, source, _, _ = self._no_cache_native()
        first = native.query(CODE)
        second = native.query(CODE)
        self.assertEqual(second.results, first.results)
        self.assertFalse(second.from_cache)
        self.assertEqual(source.calls, 1)

    def test_memo_repopulates_last_outcome(self):
        native, source, _, _ = self._no_cache_native()
        native.query(CODE)
        self.assertFalse(native.from_cache)
        native.last_outcome = None  # 模拟中间被别次查询覆盖
        second = native.query(CODE)
        self.assertIs(native.last_outcome, second)
        self.assertFalse(second.stale)
        self.assertIsNone(second.upstream_error)
        self.assertEqual(source.calls, 1)

    def test_memo_does_not_hide_cancellation(self):
        state = {"cancelled": False}
        native, _, _, _ = self._no_cache_native(cancelled=lambda: state["cancelled"])
        native.query(CODE)
        state["cancelled"] = True
        with self.assertRaises(QueryCancelled):
            native.query(CODE)


# ---------- M12b：stale/upstream_error 标记（which --json 消费） ----------

class M12bFlagExposureTest(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]

    def test_ttl_hit_is_from_cache_but_not_stale(self):
        native, _, cache, namespace = _make_native(self.now)
        cache.entries[namespace, CODE] = CacheEntry(self.now[0] - 5, (_record(),))
        native.query(CODE)
        self.assertTrue(native.from_cache)
        self.assertFalse(native.stale)
        self.assertIsNone(native.upstream_error)

    def test_stale_fallback_sets_both_flags(self):
        cache = MemoryCache(lambda: self.now[0])
        source = Stub(error=SourceUnreachable("down"))
        native, _, _, namespace = _make_native(self.now, source=source, cache=cache)
        cache.entries[namespace, CODE] = CacheEntry(
            self.now[0] - (DEFAULT_TTL + 1), (_record(),))
        native.query(CODE)
        self.assertTrue(native.stale)
        self.assertIsInstance(native.upstream_error, SourceUnreachable)

    def test_flags_reset_each_query(self):
        cache = MemoryCache(lambda: self.now[0])
        source = Stub(error=SourceUnreachable("down"))
        native, _, _, namespace = _make_native(self.now, source=source, cache=cache)
        cache.entries[namespace, CODE] = CacheEntry(
            self.now[0] - (DEFAULT_TTL + 1), (_record(),))
        native.query(CODE)
        self.assertTrue(native.stale)
        source.error = None
        self.now[0] += DEFAULT_STALE_LIMIT + DEFAULT_TTL  # 让缓存彻底不可用作兜底
        cache.entries.clear()
        native.query(CODE)
        self.assertFalse(native.stale)
        self.assertIsNone(native.upstream_error)
        self.assertFalse(native.from_cache)




# ---------- M11（CachedCurrency 侧）：memo key 带端点、flag 元组重放 ----------

class _FakeInner:
    def __init__(self, url, rows=None, error=None):
        self.name = "mcp"
        self.url = url
        self.rows = rows if rows is not None else [_record(source="mcp")]
        self.error = error
        self.calls = 0

    def lookup(self, code):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


class M11CachedMemoTest(unittest.TestCase):
    def setUp(self):
        _cache.clear()
        self.addCleanup(_cache.clear)

    def test_memo_key_includes_endpoint(self):
        a = CachedCurrency(_FakeInner("https://a.example/mcp"))
        b = CachedCurrency(_FakeInner("https://b.example/mcp"))
        a.lookup(CODE)
        b.lookup(CODE)
        self.assertEqual(a.inner.calls, 1)
        self.assertEqual(b.inner.calls, 1)  # 端点不同不得命中 a 的 memo

    def test_second_lookup_replays_flags(self):
        inner = _FakeInner("https://a.example/mcp")
        cur = CachedCurrency(inner)
        cur.lookup(CODE)
        cur.from_cache = True  # 外力污染
        cur.lookup(CODE)
        self.assertFalse(cur.from_cache)
        self.assertFalse(cur.stale)
        self.assertIsNone(cur.upstream_error)
        self.assertEqual(inner.calls, 1)

    def test_unreachable_disk_fallback_marks_stale(self):
        inner = _FakeInner("https://a.example/mcp", error=SourceUnreachable("down"))
        cur = CachedCurrency(inner, store=object())
        with mock.patch.object(CachedCurrency, "_load", return_value=([_record(source="mcp")], 3.5)):
            cur.lookup(CODE)
        self.assertTrue(cur.from_cache)
        self.assertTrue(cur.stale)
        self.assertIsNotNone(cur.upstream_error)
        self.assertEqual(cur.cache_age_days, 3.5)

    def test_endpoint_of_uses_url(self):
        inner = _FakeInner("https://a.example/mcp")
        self.assertEqual(endpoint_of(inner), "https://a.example/mcp")


# ---------- M18/M19/M20：ccsn ----------

class _FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.posts = []
        self.closed = 0

    def post_form(self, url, form, accept=None):
        self.posts.append((url, dict(form)))
        return self.payload

    def close(self):
        self.closed += 1


def _ccsn_payload(total=1, rows=1):
    return json.dumps({
        "Total": total,
        "List": [{
            "StandardCode": f"CECS 1{i}-2020",
            "StandardCNName": "标准",
            "StandardState": "现行",
            "AbolishDate": "",
            "ReplaceStandardCode": "",
            "PublishDate": "2020-01-01",
            "PerformDate": "2020-06-01",
        } for i in range(rows)],
    }).encode()


class M18CcsnPaginationTest(unittest.TestCase):
    def test_total_missing_means_no_has_more(self):
        client = _FakeClient(json.dumps({"List": []}).encode())
        cur = CcsnCurrency(client)
        cur.lookup("CECS 1-2020")
        self.assertEqual(cur.pagination["total"], 0)
        self.assertFalse(cur.pagination["has_more"])

    def test_more_than_page_warns_and_marks_has_more(self):
        client = _FakeClient(_ccsn_payload(total=40, rows=20))
        cur = CcsnCurrency(client)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cur.lookup("CECS 1-2020")
        self.assertTrue(cur.pagination["has_more"])
        self.assertEqual(cur.pagination["fetched_pages"], 1)
        self.assertTrue(any("不完整" in str(w.message) for w in caught))

    def test_assx_url_kept_as_is(self):
        self.assertIn(".assx", SEARCH_URL)

    def test_close_and_context_manager(self):
        client = _FakeClient(json.dumps({"Total": 0, "List": []}).encode())
        with CcsnCurrency(client) as cur:
            cur.lookup("CECS 1-2020")
        self.assertEqual(client.closed, 1)


class M20bCcsnStatusTest(unittest.TestCase):
    def test_burevision_done_is_unknown_not_superseded(self):
        # 「局修完成」只说明一次局部修订流程结束：既不能证明被代替也不能证明仍现行。
        self.assertEqual(status_ccsn("局修完成", "2016-08-01"), "unknown")
        self.assertEqual(status_ccsn("局修完成", ""), "unknown")

    def test_other_unknown_words_keep_abolish_fallback(self):
        self.assertEqual(status_ccsn("奇怪状态", "2016-08-01"), "superseded")
        self.assertEqual(status_ccsn("奇怪状态", ""), "unknown")


if __name__ == "__main__":
    unittest.main()
