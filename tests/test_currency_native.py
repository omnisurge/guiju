"""固定时钟离线验证 TTL / stale / 备源来源 / 当前准入与取消。"""

import unittest
from unittest import mock

from guiju.currency.cache import CACHE_NAMESPACE, DEFAULT_STALE_LIMIT, DEFAULT_TTL, CacheEntry
from guiju.currency.fallback import FallbackCurrency
from guiju.currency.model import QueryCancelled, Record
from guiju.currency.native import NativeCurrency, NoCurrencySource, Outcome, human_age
from guiju.currency.registry import Registry, Route
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable

CODE = "GB 1-2000"
NOW = 10_000_000.0


class Stub:
    def __init__(self, name="p", rows=None, error=None):
        self.name, self.error = name, error
        self.rows = [] if rows is None else rows
        self.calls = 0
        self.action = None

    def lookup(self, code):
        self.calls += 1
        if self.action is not None:
            return self.action(code)
        if self.error is not None:
            raise self.error
        return list(self.rows)


class MemoryCache:
    def __init__(self, clock, ttl=DEFAULT_TTL):
        self.clock, self.ttl = clock, ttl
        self.entries = {}
        self.calls = []

    def get(self, code, *, namespace=CACHE_NAMESPACE):
        self.calls.append("get")
        entry = self.entries.get((namespace, code))
        return entry if entry is not None and entry.fresh(self.ttl, self.clock()) else None

    def get_any(self, code, *, namespace=CACHE_NAMESPACE):
        self.calls.append("get_any")
        return self.entries.get((namespace, code))

    def put(self, code, records, *, namespace=CACHE_NAMESPACE):
        self.calls.append("put")
        self.entries[namespace, code] = CacheEntry(self.clock(), tuple(records))


class NativeQueryTest(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.record = Record(CODE, "current", "标准", "2000-01-01", "p", publish_date="1999-01-01", raw="证据")
        self.source = Stub(rows=[self.record])
        self.registry = Registry()
        self.registry.register(Route("p", ("GB",), lambda: self.source))
        self.cache = MemoryCache(lambda: self.now)
        self.native = NativeCurrency(self.registry, self.cache, clock=lambda: self.now)
        self.namespace = self.registry.namespace_for(CODE, self.source)

    def seed(self, age, rows=None):
        entry = CacheEntry(self.now - age, tuple([self.record] if rows is None else rows))
        self.cache.entries[self.namespace, CODE] = entry
        return entry

    def test_miss_success_put_then_fresh_hit_never_requeries(self):
        first = self.native.query(CODE)
        self.assertFalse(first.from_cache)
        self.now += 5
        second = self.native.query(CODE)
        self.assertEqual(second.results, (self.record,))
        self.assertTrue(second.from_cache)
        self.assertFalse(second.stale)
        self.assertEqual(second.cache_age, 5)
        self.assertEqual(second.critical_notes(), [])
        self.assertIn("命中本地缓存", second.notes()[0])
        self.assertEqual(self.source.calls, 1)
        self.assertTrue(self.native.from_cache)
        self.assertEqual(self.native.cache_age_days, 5 / 86400)

    def test_ttl_exact_boundary_is_expired(self):
        self.source.error = SourceUnreachable("offline")
        for age, from_upstream in ((DEFAULT_TTL - 0.001, False), (DEFAULT_TTL, True), (DEFAULT_TTL + 0.001, True)):
            self.seed(age)
            self.source.calls = 0
            with self.subTest(age=age):
                result = self.native.query(CODE)
                self.assertEqual(result.stale, from_upstream)
                self.assertEqual(self.source.calls, int(from_upstream))

    def test_stale_thirty_day_boundary_is_inclusive(self):
        self.source.error = SourceUnreachable("offline")
        for age in (DEFAULT_STALE_LIMIT - 0.001, DEFAULT_STALE_LIMIT):
            self.seed(age)
            result = self.native.query(CODE)
            self.assertTrue(result.stale)
            self.assertTrue(result.from_cache)
            self.assertIs(result.upstream_error, self.source.error)
            self.assertIn("可能已过期", result.critical_notes()[0])
            self.assertIn("降级", result.annotation())
        self.seed(DEFAULT_STALE_LIMIT + 0.001)
        with self.assertRaises(SourceUnreachable):
            self.native.query(CODE)
        self.assertFalse(self.native.from_cache)
        self.assertIsNone(self.native.last_outcome)

    def test_success_empty_replaces_old_nonempty_and_is_negative_cached(self):
        self.seed(3 * 86400)
        self.source.rows = []
        result = self.native.query(CODE)
        self.assertEqual(result.results, ())
        self.assertFalse(result.stale)
        self.assertNotIn("get_any", self.cache.calls)
        result = self.native.query(CODE)
        self.assertTrue(result.from_cache)
        self.assertEqual(self.source.calls, 1)

    def test_stale_empty_cache_is_not_current_authoritative_negative(self):
        self.seed(3 * 86400, [])
        self.source.error = SourceUnreachable("offline")
        result = self.native.query(CODE)
        self.assertEqual(result.results, ())
        self.assertTrue(result.stale)
        self.assertTrue(result.critical_notes())

    def test_no_cache_and_cache_io_failure_do_not_break_success(self):
        self.assertEqual(NativeCurrency(self.registry).lookup(CODE), [self.record])
        cache = mock.Mock()
        cache.get.side_effect = OSError("unreadable")
        cache.put.side_effect = OSError("readonly")
        self.assertEqual(NativeCurrency(self.registry, cache).lookup(CODE), [self.record])

    def test_no_source_or_disabled_route_never_reads_cache(self):
        cache = mock.Mock()
        for reg in (Registry(), self._disabled_registry()):
            with self.assertRaises(NoCurrencySource):
                NativeCurrency(reg, cache).query(CODE)
        cache.get.assert_not_called()
        cache.get_any.assert_not_called()

    def _disabled_registry(self):
        reg = Registry()
        reg.register(Route("p", ("GB",), lambda: self.source, disabled=True))
        return reg

    def test_primary_40_is_not_stale_and_does_not_touch_old_entry(self):
        original = self.seed(3 * 86400)
        denied = ComplianceDenied("captcha")
        self.source.error = denied
        with self.assertRaises(ComplianceDenied) as ctx:
            self.native.query(CODE)
        self.assertIs(ctx.exception, denied)
        self.assertNotIn("get_any", self.cache.calls)
        self.assertNotIn("put", self.cache.calls)
        self.assertIs(self.cache.entries[self.namespace, CODE], original)

    def test_current_admission_precedes_even_fresh_negative_cache(self):
        state = {"deny": False}

        def admit(_code):
            if state["deny"]:
                raise ComplianceDenied("policy revoked")

        native = NativeCurrency(self.registry, self.cache, clock=lambda: self.now, admit=admit)
        self.source.rows = []
        native.query(CODE)
        state["deny"] = True
        self.cache.calls.clear()
        with self.assertRaises(ComplianceDenied):
            native.query(CODE)
        self.assertEqual(self.source.calls, 1)
        self.assertEqual(self.cache.calls, [])
        self.assertFalse(native.from_cache)

    def test_source_policy_change_during_query_prevents_put(self):
        state = {"deny": False}

        def admit(_code):
            if state["deny"]:
                raise ComplianceDenied("policy revoked")

        def action(_code):
            state["deny"] = True
            return [self.record]

        self.source.action = action
        native = NativeCurrency(self.registry, self.cache, admit=admit)
        with self.assertRaises(ComplianceDenied):
            native.query(CODE)
        self.assertNotIn("put", self.cache.calls)

    def test_precancelled_never_returns_fresh_or_stale_cache(self):
        for age in (1, 3 * 86400):
            self.seed(age)
            self.cache.calls.clear()
            with self.assertRaises(QueryCancelled):
                NativeCurrency(self.registry, self.cache, cancelled=lambda: True).query(CODE)
            self.assertEqual(self.cache.calls, [])
        self.assertEqual(self.source.calls, 0)

    def test_cancel_during_source_error_prevents_stale_and_write(self):
        state = {"cancel": False}
        self.seed(3 * 86400)

        def action(_code):
            state["cancel"] = True
            raise SourceUnreachable("timeout")

        self.source.action = action
        native = NativeCurrency(self.registry, self.cache, cancelled=lambda: state["cancel"])
        with self.assertRaises(QueryCancelled):
            native.query(CODE)
        self.assertNotIn("get_any", self.cache.calls)
        self.assertNotIn("put", self.cache.calls)

    def test_recoverable_parse_error_can_use_marked_stale_cache(self):
        self.seed(2 * 86400)
        self.source.error = NeedsHuman("upstream changed")
        outcome = self.native.query(CODE)
        self.assertTrue(outcome.stale)
        self.assertIs(outcome.upstream_error, self.source.error)

    def test_programming_error_and_keyboard_interrupt_are_never_recovered(self):
        self.seed(3 * 86400)
        for error in (ValueError("bug"), KeyboardInterrupt()):
            self.cache.calls.clear()
            self.source.error = error
            with self.assertRaises(type(error)):
                self.native.query(CODE)
            self.assertNotIn("get_any", self.cache.calls)

    def test_invalid_future_or_zero_timestamp_cannot_restore_success(self):
        self.source.error = SourceUnreachable("offline")
        for stamp in (0, -1, self.now + 1, float("inf"), float("nan")):
            self.cache.entries[self.namespace, CODE] = CacheEntry(stamp, (self.record,))
            with self.subTest(stamp=stamp), self.assertRaises(SourceUnreachable):
                self.native.query(CODE)

    def test_cached_record_must_match_family_status_and_current_sources(self):
        self.source.error = SourceUnreachable("offline")
        for record in (Record("GB2", "current", source="p"), Record(CODE, "mystery", source="p"),
                       Record(CODE, "current", source="revoked"), Record(CODE, "current", source="p", fallback="x")):
            self.seed(3 * 86400, [record])
            with self.subTest(record=record), self.assertRaises(SourceUnreachable):
                self.native.query(CODE)

    def test_route_namespace_change_does_not_reuse_fresh_old_policy(self):
        self.native.query(CODE)
        reg = Registry()
        reg.register(Route("p", ("GB",), lambda: self.source, cache_namespace="new-policy"))
        self.source.rows = []
        result = NativeCurrency(reg, self.cache).query(CODE)
        self.assertEqual(result.results, ())
        self.assertFalse(result.from_cache)
        self.assertEqual(self.source.calls, 2)

    def test_backup_40_is_never_recovered_from_outer_stale(self):
        last = Stub("last")
        denied = ComplianceDenied("blocked backup")
        chain = FallbackCurrency("p", Stub("p", error=SourceUnreachable("down")),
                                 Stub("b", error=denied), last)
        reg = Registry()
        reg.register(Route("p", ("GB",), lambda: chain))
        namespace = reg.namespace_for(CODE, chain)
        self.cache.entries[namespace, CODE] = CacheEntry(self.now - 3 * 86400, (self.record,))
        with self.assertRaises(ComplianceDenied) as ctx:
            NativeCurrency(reg, self.cache, clock=lambda: self.now).query(CODE)
        self.assertIs(ctx.exception, denied)
        self.assertNotIn("get_any", self.cache.calls)
        self.assertEqual(last.calls, 0)

    def test_fallback_provenance_survives_fresh_and_stale_replay(self):
        primary = Stub("p", error=SourceUnreachable("down"))
        backup = Stub("b", [Record(CODE, "current", "标准", source="b")])
        chain = FallbackCurrency("p", primary, backup)
        reg = Registry()
        reg.register(Route("p", ("GB",), lambda: chain))
        native = NativeCurrency(reg, self.cache, clock=lambda: self.now)
        first = native.query(CODE)
        fresh = native.query(CODE)
        self.assertEqual(first.fallback_source, "b")
        self.assertEqual(fresh.fallback_source, "b")
        self.assertTrue(fresh.from_cache)
        self.assertIn("备用源: b", fresh.annotation())
        self.now += 3 * 86400
        backup.error = SourceUnreachable("backup down")
        stale = native.query(CODE)
        self.assertTrue(stale.stale)
        self.assertEqual(stale.fallback_source, "b")
        self.assertEqual(len(stale.critical_notes()), 2)
        self.assertIn("降级", stale.annotation())
        self.assertIn("备用源: b", stale.annotation())

    def test_cache_entry_freshness_and_age_rendering(self):
        entry = CacheEntry(NOW, ())
        self.assertFalse(entry.fresh(0, NOW))
        self.assertFalse(entry.fresh(DEFAULT_TTL, NOW - 1))
        self.assertTrue(entry.fresh(DEFAULT_TTL, NOW + DEFAULT_TTL - 1))
        self.assertFalse(entry.fresh(DEFAULT_TTL, NOW + DEFAULT_TTL))
        for seconds, text in ((0, "不到 1 分钟"), (61, "1 分钟"), (3600, "1 小时"), (86400, "1 天")):
            self.assertEqual(human_age(seconds), text)
        self.assertEqual(Outcome((), "p").notes(), [])
        self.assertEqual(Outcome((), "p").annotation(), "")


if __name__ == "__main__":
    unittest.main()
