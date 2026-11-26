"""时效引擎韧性、缓存回退、admit 记账、memo、stale 标记与异常配置回归测试。"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import guiju.currency as currency_pkg
from guiju.currency import (
    CachedCurrency,
    MisconfiguredCurrency,
    endpoint_of,
    native_currency,
)
from guiju.currency.cache import CACHE_NAMESPACE, DEFAULT_STALE_LIMIT, DEFAULT_TTL, CacheEntry
from guiju.currency.ccsn import CcsnCurrency
from guiju.currency.codes import query_with_base_fallback
from guiju.currency.contract import Completeness
from guiju.currency.dbba import DbbaCurrency
from guiju.currency.fallback import FallbackCurrency
from guiju.currency.jtst import JtstCurrency
from guiju.currency.model import QueryCancelled, Record
from guiju.currency.native import NativeCurrency, NoCurrencyRecord, NoCurrencySource
from guiju.currency.registry import Registry, Route, default_registry
from guiju.currency.stdsamr import StdSamrCurrency
from guiju.errors import GuijuError, NotIndexed, SourceUnreachable
from guiju.store.sqlite import Store

CODE = "GB 1-2000"
CODE_W3 = "GB 50011-2010"
NOW = 10_000_000.0
NOW_W3 = 1_700_000_000.0


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


class _FakeInner:
    def __init__(self, url, rows=None, error=None):
        self.name = "mcp"
        self.url = url
        self.rows = rows if rows is not None else [_record(source="mcp")]
        self.error = error
        self.from_cache = False
        self.stale = False
        self.upstream_error = None
        self.cache_age_days = None
        self.calls = 0

    def lookup(self, code):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


class _SequenceClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _take(self, url):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError(f"fake client 没有更多响应：{url}")
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, *, accept=None):
        return self._take(url)

    def post_form(self, url, fields, *, accept=None):
        return self._take(url)

    def post_json(self, url, payload):
        return self._take(url)

    def close(self):
        pass


def _samr_body(rows=(), total=0):
    return json.dumps({"total": total, "rows": list(rows)}, ensure_ascii=False).encode("utf-8")


def _dbba_body(records=(), total=0):
    return json.dumps({"total": total, "records": list(records)}, ensure_ascii=False).encode("utf-8")


def _ccsn_body(rows=(), total=0, has_next=False):
    return json.dumps(
        {"Total": total, "List": list(rows), "HasNextPage": has_next},
        ensure_ascii=False,
    ).encode("utf-8")


def _samr_row(code="GB/T 9999-2020"):
    return {"STATE": "现行", "C_STD_CODE": code, "C_C_NAME": "某标准", "ISSUE_DATE": "", "ACT_DATE": ""}


def _dbba_row(code="GB/T 9999-2020"):
    return {"code": code, "chName": "某标准", "status": "现行", "reviseStdCodes": "",
            "actDate": 0, "issueDate": 0, "fzDate": 0}


def _ccsn_row(code="GB/T 9999-2020"):
    return {"StandardCode": code, "StandardCNName": "某标准", "StandardState": "现行",
            "AbolishDate": "", "ReplaceStandardCode": "", "PublishDate": "", "PerformDate": ""}


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(
            socket.socket, "connect",
            side_effect=AssertionError("offline test attempted a real socket connection"),
        )
        guard.start()
        self.addCleanup(guard.stop)


class L2CacheOffTest(unittest.TestCase):
    def test_native_currency_cache_off_returns_no_cache_adapter(self):
        with mock.patch.dict(currency_pkg.os.environ, {"GUIJU_CURRENCY_CACHE_OFF": "1"}):
            adapter = native_currency()
        self.assertIsInstance(adapter, NativeCurrency)
        self.assertIsNone(adapter.cache)

    def test_native_currency_cache_off_outranks_explicit_dir(self):
        # 钉住文档语义：GUIJU_CURRENCY_CACHE_OFF=1 优先级最高，
        # 连 GUIJU_CURRENCY_CACHE=<目录> / 参数级目录都不能绕过它。
        with mock.patch.dict(currency_pkg.os.environ,
                             {"GUIJU_CURRENCY_CACHE_OFF": "1", "GUIJU_CURRENCY_CACHE": "/tmp/x"}):
            adapter = native_currency(cache_dir="/tmp/x")
        self.assertIsInstance(adapter, NativeCurrency)
        self.assertIsNone(adapter.cache)

    def test_native_currency_cache_off_zero_keeps_cache(self):
        with mock.patch.dict(currency_pkg.os.environ, {"GUIJU_CURRENCY_CACHE_OFF": "0", "GUIJU_HOME": "/tmp/guiju-test-home"}):
            adapter = native_currency()
        self.assertIsNotNone(adapter.cache)


class L2MisconfigHintTest(unittest.TestCase):
    def test_hint_explains_native_forms(self):
        bad = MisconfiguredCurrency("cmd", "二进制不存在")
        try:
            bad.lookup(CODE)
        except SourceUnreachable as exc:
            hint = getattr(exc, "hint", "") or str(exc)
        else:
            self.fail("MisconfiguredCurrency.lookup 必须 SourceUnreachable（不静默换源）")
        self.assertIn("native", hint)
        self.assertIn("off", hint)


class L4AdmitAccountingTest(unittest.TestCase):
    def setUp(self):
        self.now = [NOW]

    def test_lookup_charges_admit_at_entry_and_pre_put(self):
        # 钉住真实契约：上游未命中的一次 lookup 恰计 2 次 admit（入口 + put 前复验），不随 _guard 重复计费。
        admits = []
        native, _source, _cache, _ = _make_native(self.now, admit=lambda code: admits.append(code))
        native.lookup(CODE)
        self.assertEqual(admits, [CODE, CODE])
        native.lookup(CODE)  # 缓存命中：只入口 admit，没有 put 前那次
        self.assertEqual(admits, [CODE, CODE, CODE])

    def test_guard_itself_never_charges_admit(self):
        # _guard 被调用多次但只查 cancelled；已计费的 admit 不应再被守卫重放。
        guards = []
        admits = []

        def cancelled():
            return (guards.append(1), False)[1]

        native, _source, _, _ = _make_native(self.now, cancelled=cancelled,
                                             admit=lambda code: admits.append(code))
        native.lookup(CODE)
        self.assertGreater(len(guards), 2)   # 守卫确实跑了多次
        self.assertEqual(len(admits), 2)     # 但 admit 不随守卫线性膨胀

    def test_cancelled_query_never_reaches_admit(self):
        admits = []
        native, source, _, _ = _make_native(self.now, cancelled=lambda: True,
                                            admit=lambda code: admits.append(code))
        with self.assertRaises(QueryCancelled):
            native.lookup(CODE)
        self.assertEqual(admits, [])   # 已取消的查询不许占准入名额
        self.assertEqual(source.calls, 0)


class L7NoPruneExpiredTest(unittest.TestCase):
    def test_store_layer_has_no_prune_expired(self):
        import guiju.store.currency as store_currency
        self.assertFalse(hasattr(store_currency, "prune_expired"))


class L8FallbackAdmitTest(unittest.TestCase):
    def test_admit_called_once_per_lookup(self):
        admits = []
        fb = FallbackCurrency("x", Stub(name="p", rows=[_record()]),
                              Stub(name="b", rows=[_record()]),
                              admit=lambda code: admits.append(code))
        fb.lookup(CODE)
        fb.lookup(CODE)
        self.assertEqual(admits, [CODE, CODE])

    def test_admit_called_before_backup_too(self):
        admits = []
        fb = FallbackCurrency("x", Stub(error=SourceUnreachable("down")),
                              Stub(name="b", rows=[_record()]),
                              admit=lambda code: admits.append(code))
        rows = fb.lookup(CODE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(admits, [CODE])

    def test_cancelled_lookup_skips_admit(self):
        admits = []
        fb = FallbackCurrency("x", Stub(), cancelled=lambda: True,
                              admit=lambda code: admits.append(code))
        with self.assertRaises(QueryCancelled):
            fb.lookup(CODE)
        self.assertEqual(admits, [])

    def test_default_registry_accepts_admit_without_wiring(self):
        admits = []
        registry = default_registry(admit=lambda code: admits.append(code))
        adapter = registry.match(CODE)
        self.assertIsNotNone(adapter)          # 能构建，不要求真实 admit 去向
        self.assertIsNone(registry.match("XX 999"))


class L8CacheKeyNoNormalizationTest(unittest.TestCase):
    def test_memory_cache_layer_sees_raw_code(self):
        cache = MemoryCache(lambda: NOW)
        native, _source, _, _ = _make_native([NOW], cache=cache)
        native.lookup("GB 1-2000")
        keys = [code for (_ns, code) in cache.entries]
        self.assertEqual(keys, ["GB 1-2000"])  # 原样落键，未大写/去空格


class R4NoCurrencyRecordChainTest(unittest.TestCase):
    """34 no_currency_record 是 GuijuError 直属分支，不再挂在 31 not_indexed 下；
    NoCurrencySource 旧类名保留，assertRaises 继续生效。"""

    def test_chain(self):
        self.assertTrue(issubclass(NoCurrencyRecord, GuijuError))
        self.assertTrue(issubclass(NoCurrencySource, NoCurrencyRecord))
        self.assertFalse(issubclass(NoCurrencyRecord, NotIndexed))  # 34 与 31 分家

    def test_exit_and_reason(self):
        self.assertEqual(NoCurrencyRecord.exit, 34)
        self.assertEqual(NoCurrencySource.exit, 34)
        self.assertEqual(NoCurrencyRecord.reason, "no_currency_record")
        self.assertNotEqual(NoCurrencyRecord.exit, NotIndexed.exit)

    def test_raised_from_unrouted_code_and_still_catchable_by_old_name(self):
        cache = mock.Mock()
        with self.assertRaises(NoCurrencySource) as ctx:   # 旧名兼容路径
            NativeCurrency(Registry(), cache).query("XX 999")
        self.assertEqual(ctx.exception.exit, 34)
        cache.get.assert_not_called()
        cache.get_any.assert_not_called()
        with self.assertRaises(NoCurrencyRecord):          # 新基类同样可捕
            NativeCurrency(Registry()).query("XX 999")


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

        native, _source, cache, _ = _make_native(self.now, admit=admit)
        with self.assertRaises(SourceUnreachable):
            native.query(CODE)
        self.assertEqual(cache.calls.count("put"), 0)


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


class M11CachedMemoTest(unittest.TestCase):
    def test_lookup_resets_and_forwards_inner_flags(self):
        inner = _FakeInner("https://a.example/mcp")
        cur = CachedCurrency(inner)
        cur.from_cache = True
        cur.stale = True
        cur.upstream_error = SourceUnreachable("old")
        cur.cache_age_days = 5.0

        cur.lookup(CODE)
        self.assertFalse(cur.from_cache)
        self.assertFalse(cur.stale)
        self.assertIsNone(cur.upstream_error)
        self.assertIsNone(cur.cache_age_days)

        inner.from_cache = True
        inner.stale = True
        inner.upstream_error = SourceUnreachable("down")
        inner.cache_age_days = 3.5
        cur.lookup(CODE)
        self.assertTrue(cur.from_cache)
        self.assertTrue(cur.stale)
        self.assertIsNotNone(cur.upstream_error)
        self.assertEqual(cur.cache_age_days, 3.5)

    def test_endpoint_of_uses_url(self):
        inner = _FakeInner("https://a.example/mcp")
        self.assertEqual(endpoint_of(inner), "https://a.example/mcp")


class BaseFallbackCompletenessTest(OfflineTestCase):
    """W3-1：第一次检索不完整时，base fallback 的 complete=True 不能把它洗白。"""

    def test_first_page_incomplete_wins_over_complete_base(self):
        cases = (
            (StdSamrCurrency, lambda rows, total: _samr_body(rows, total)),
            (DbbaCurrency, lambda rows, total: _dbba_body(rows, total)),
            (CcsnCurrency, lambda rows, total: _ccsn_body(rows, total)),
        )
        for adapter_cls, body in cases:
            with self.subTest(adapter=adapter_cls.__name__):
                client = _SequenceClient(
                    body([_samr_row() if adapter_cls is StdSamrCurrency else
                          _dbba_row() if adapter_cls is DbbaCurrency else
                          _ccsn_row()], total=2),
                    body([], total=0),
                )
                cur = adapter_cls(client)
                self.assertEqual(cur.lookup(CODE_W3), [])
                self.assertEqual(len(client.calls), 2, "第一次空结果后必须再查基础号")
                self.assertFalse(cur.complete, "第一次 total>rows 的不完整证据不能被 base 的 complete=True 覆盖")
                self.assertIn("2", cur.incomplete_reason, "不完整原因应来自第一次检索（源声称共 2 条）")

    def test_first_page_complete_stays_complete_when_base_empty(self):
        cases = (
            (StdSamrCurrency, lambda rows, total: _samr_body(rows, total)),
            (DbbaCurrency, lambda rows, total: _dbba_body(rows, total)),
            (CcsnCurrency, lambda rows, total: _ccsn_body(rows, total)),
        )
        for adapter_cls, body in cases:
            with self.subTest(adapter=adapter_cls.__name__):
                client = _SequenceClient(body([], total=0), body([], total=0))
                cur = adapter_cls(client)
                self.assertEqual(cur.lookup(CODE_W3), [])
                self.assertTrue(cur.complete)

    def test_jtst_base_fallback_keeps_incomplete_state(self):
        first = '<table><tr><td><a><span class="en-code">JTG D99-2020</span>某标准</a></td></tr></table>'.encode()
        second = b"<table></table>"
        client = _SequenceClient(first, second)
        cur = JtstCurrency(client)
        self.assertEqual(cur.lookup("JTG D60-2015"), [])
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(cur.complete, "jtst 没有总数证据，base 的空页也不能证明完整")

    def test_snapshot_restore_contract_and_legacy_call_without_callbacks(self):
        state = {"value": Completeness(has_more=False)}

        def fetch(term):
            if term == CODE_W3:
                state["value"] = Completeness(total=9, has_more=True, reason="第一次未取全")
                return []
            state["value"] = Completeness(total=0, has_more=False)
            return []

        rows = query_with_base_fallback(
            CODE_W3, fetch,
            snapshot=lambda: state["value"],
            restore=lambda value: state.__setitem__("value", value),
        )
        self.assertEqual(rows, [])
        self.assertFalse(state["value"].complete)
        self.assertEqual(state["value"].reason, "第一次未取全")

        # 旧调用方不传任何回调也必须继续工作（不得 TypeError）。
        legacy = query_with_base_fallback(
            CODE_W3,
            lambda term: [Record("GB 50011-2010", "current")] if term == "GB 50011" else [],
        )
        self.assertEqual([r.code for r in legacy], ["GB 50011-2010"])

    def test_incomplete_base_fallback_result_is_not_written_to_cache(self):
        class RecordingCache:
            def __init__(self):
                self.puts = 0
                self.entries = {}

            def get(self, code, *, namespace=None):
                return self.entries.get((namespace, code))

            def get_any(self, code, *, namespace=None):
                return self.entries.get((namespace, code))

            def put(self, code, records, *, namespace=None):
                self.puts += 1
                self.entries[(namespace, code)] = (NOW_W3, tuple(records))

        client = _SequenceClient(_samr_body([_samr_row()], total=2), _samr_body([], total=0))
        adapter = StdSamrCurrency(client)
        registry = Registry()
        registry.register(Route("std_samr", ("GB",), lambda: adapter))
        native = NativeCurrency(registry, RecordingCache(), clock=lambda: NOW_W3)
        outcome = native.query(CODE_W3)
        self.assertEqual(outcome.results, ())
        self.assertFalse(outcome.complete)
        self.assertEqual(native.cache.puts, 0)


class MisconfiguredCurrencyCacheTest(unittest.TestCase):
    """W3-7：显式配置错误时必须报 20，不能拿旧缓存回答。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)

    def test_misconfigured_currency_never_reads_cached_records(self):
        payload = json.dumps([
            {"code": "GB 50016-2014", "status": "current", "name": "旧缓存里的规范",
             "implement_date": "", "source": "csres", "replaces": "", "fallback": ""}
        ], ensure_ascii=False)
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS currency_cache ("
            "code_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
        )
        self.store.db.execute(
            "INSERT OR REPLACE INTO currency_cache VALUES(?,?,?)",
            ("GB50016-2014", payload, "2026-01-01 00:00:00"),
        )
        self.store.commit()
        cur = CachedCurrency(
            inner=MisconfiguredCurrency("xyz(未知)", "GUIJU_CURRENCY=xyz 无法识别"),
            store=self.store,
        )
        with self.assertRaises(SourceUnreachable) as ctx:
            cur.lookup("GB 50016-2014")
        self.assertIn("无法识别", str(ctx.exception))
        self.assertFalse(cur.from_cache)


if __name__ == "__main__":
    unittest.main()
