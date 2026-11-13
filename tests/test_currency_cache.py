"""时效缓存：查过一次的编号，服务断了也能答（并如实说明是缓存）。"""

import json
import tempfile
import unittest
from pathlib import Path

from guiju import currency as currency_mod
from guiju.currency import CachedCurrency, NullCurrency, Record
from guiju.errors import SourceUnreachable
from guiju.store.sqlite import Store

REC = Record(code="GB 55037-2022", status="current", name="建筑防火通用规范",
             implement_date="2023-06-01", source="csres")


class _Failing:
    name = "mcp"

    def lookup(self, code):
        raise SourceUnreachable("服务不可达")


class _Working:
    name = "mcp"

    def lookup(self, code):
        return [REC]


class CurrencyCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "c.db"))
        self.addCleanup(self.store.close)
        currency_mod._cache.clear()
        self.addCleanup(currency_mod._cache.clear)

    def test_success_persists_to_disk(self):
        cur = CachedCurrency(inner=_Working(), store=self.store)
        records = cur.lookup("GB55037-2022")
        self.assertEqual(records[0].name, "建筑防火通用规范")
        hit = self.store.cached_currency("GB55037-2022")
        self.assertIsNotNone(hit)
        self.assertIn("建筑防火通用规范", hit[0])

    def test_unreachable_falls_back_to_cache(self):
        CachedCurrency(inner=_Working(), store=self.store).lookup("GB55037-2022")
        currency_mod._cache.clear()
        cur = CachedCurrency(inner=_Failing(), store=self.store)
        records = cur.lookup("GB55037-2022")
        self.assertEqual(len(records), 1)
        self.assertTrue(cur.from_cache)
        self.assertIsNotNone(cur.cache_age_days)

    def test_unreachable_without_cache_still_raises(self):
        with self.assertRaises(SourceUnreachable):
            CachedCurrency(inner=_Failing(), store=self.store).lookup("GB 99999-2099")

    def test_null_currency_never_caches(self):
        cur = CachedCurrency(inner=NullCurrency(), store=self.store)
        self.assertEqual(cur.lookup("GB55037-2022"), [])
        self.assertIsNone(self.store.cached_currency("GB55037-2022"))

    def test_cache_roundtrip_payload(self):
        self.store.cache_currency("GB55037-2022", json.dumps([REC.as_payload()], ensure_ascii=False))
        payload, fetched = self.store.cached_currency("GB55037-2022")
        self.assertEqual(json.loads(payload)[0]["code"], "GB 55037-2022")
        self.assertTrue(fetched)

    def test_fallback_provenance_survives_cache_replay(self):
        # M2 回归：备源应答的 fallback/replaces 必须落库，重放时不得伪装成主源。
        rec = Record(code="GB 55037-2022", status="current", name="建筑防火通用规范",
                     implement_date="2023-06-01", source="ccsn",
                     replaces="GB 50016-2014", fallback="ccsn")

        class _FallbackHit:
            name = "fallback"

            def lookup(self, code):
                return [rec]

        CachedCurrency(inner=_FallbackHit(), store=self.store).lookup("GB55037-2022")
        # 落库的 payload 必须带溯源字段
        payload, _ = self.store.cached_currency("GB55037-2022")
        row = json.loads(payload)[0]
        self.assertEqual(row["fallback"], "ccsn")
        self.assertEqual(row["replaces"], "GB 50016-2014")
        # 断网重放：重建的 Record 保留 fallback/replaces
        currency_mod._cache.clear()
        records = CachedCurrency(inner=_Failing(), store=self.store).lookup("GB55037-2022")
        self.assertEqual(records[0].fallback, "ccsn")
        self.assertEqual(records[0].replaces, "GB 50016-2014")


if __name__ == "__main__":
    unittest.main()
