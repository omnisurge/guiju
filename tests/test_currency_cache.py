"""时效缓存（v1.3.0）：原生 DiskCurrencyCache 承担 TTL/陈旧降级，Store 不再落旧 currency_cache。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from guiju import currency as currency_mod
from guiju.currency import CachedCurrency, NullCurrency, Record
from guiju.currency.native import NativeCurrency
from guiju.currency.registry import Registry, Route
from guiju.errors import SourceUnreachable
from guiju.store.currency import DiskCurrencyCache
from guiju.store.sqlite import Store

CODE = "GB 55037-2022"
REC = Record(
    code="GB 55037-2022",
    status="current",
    name="建筑防火通用规范",
    implement_date="2023-06-01",
    source="csres",
)


class _Adapter:
    name = "csres"
    source_names = ("csres", "ccsn")

    def __init__(self, records=None, *, fail: bool = False):
        self.records = list(records) if records is not None else [REC]
        self.fail = fail

    def lookup(self, _code):
        if self.fail:
            raise SourceUnreachable("服务不可达")
        return list(self.records)


class CurrencyCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "c.db")
        self.addCleanup(self.store.close)
        self.now = [1_700_000_000.0]
        self.cache = DiskCurrencyCache(self.root / "currency-cache", ttl=3600, clock=lambda: self.now[0])

    def test_legacy_store_currency_cache_and_module_memo_are_removed(self):
        self.assertFalse(hasattr(self.store, "cache_currency"))
        self.assertFalse(hasattr(self.store, "cached_currency"))
        self.assertFalse(hasattr(currency_mod, "_cache"))

    def test_native_disk_cache_persists_and_replays_stale_through_wrapper(self):
        adapter = _Adapter()
        reg = Registry()
        reg.register(Route("csres", ("GB",), lambda: adapter))
        cur = CachedCurrency(NativeCurrency(reg, self.cache, clock=lambda: self.now[0]))
        records = cur.lookup(CODE)
        self.assertEqual(records[0].name, "建筑防火通用规范")
        self.assertFalse(cur.from_cache)

        # 超过 TTL 后上游故障，在 30 天内降级使用磁盘旧缓存并标注 stale/from_cache
        self.now[0] += 3 * 86400
        adapter.fail = True
        stale_records = cur.lookup(CODE)
        self.assertEqual(len(stale_records), 1)
        self.assertTrue(cur.from_cache)
        self.assertTrue(cur.stale)
        self.assertIsNotNone(cur.cache_age_days)

    def test_unreachable_without_cache_still_raises(self):
        reg = Registry()
        reg.register(Route("csres", ("GB",), lambda: _Adapter(fail=True)))
        cur = CachedCurrency(NativeCurrency(reg, self.cache, clock=lambda: self.now[0]))
        with self.assertRaises(SourceUnreachable):
            cur.lookup("GB 99999-2099")

    def test_null_currency_never_caches(self):
        cur = CachedCurrency(inner=NullCurrency())
        self.assertEqual(cur.lookup(CODE), [])
        self.assertFalse((self.root / "currency-cache").exists())

    def test_fallback_provenance_survives_disk_cache_replay(self):
        rec = Record(
            code="GB 55037-2022",
            status="current",
            name="建筑防火通用规范",
            implement_date="2023-06-01",
            source="ccsn",
            replaces="GB 50016-2014",
            fallback="ccsn",
        )
        adapter = _Adapter([rec])
        reg = Registry()
        reg.register(Route("csres", ("GB",), lambda: adapter))
        cur = CachedCurrency(NativeCurrency(reg, self.cache, clock=lambda: self.now[0]))
        cur.lookup(CODE)

        self.now[0] += 3 * 86400
        adapter.fail = True
        records = cur.lookup(CODE)
        self.assertEqual(records[0].fallback, "ccsn")
        self.assertEqual(records[0].replaces, "GB 50016-2014")

    def test_legacy_orphan_currency_cache_table_is_preserved_harmlessly(self):
        db_path = self.root / "legacy-orphan.db"
        with Store(db_path) as s:
            s.db.execute(
                "CREATE TABLE currency_cache (code_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
            )
            s.db.execute(
                "INSERT INTO currency_cache VALUES ('GB55037-2022', '[]', '2026-01-01 00:00:00')"
            )
            s.commit()
        with Store(db_path) as reopened:
            reopened.upsert_announcement("https://x/1", "2026-01-01", "标题", "标准", "national")
            reopened.commit()
            self.assertEqual(reopened.count_announcements(), 1)
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT payload FROM currency_cache WHERE code_key='GB55037-2022'").fetchone()
            self.assertEqual(row[0], "[]")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
