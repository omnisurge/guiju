"""时效端口 —— 缓存与兜底行为必须稳（spec-currency 已退役，不再测 cmd/mcp 适配器）。"""

import unittest

from guiju.currency import CachedCurrency, NullCurrency, Record


class Fake:
    name = "fake"

    def __init__(self, records):
        self.records = records
        self.calls = 0

    def lookup(self, code):
        self.calls += 1
        return self.records


class CacheTest(unittest.TestCase):
    def test_second_lookup_does_not_hit_upstream(self):
        fake = Fake([Record(code="GB 1-2020", status="current", name="X")])
        cached = CachedCurrency(fake)
        cached.lookup("GB 1-2020")
        cached.lookup("GB 1-2020")
        self.assertEqual(fake.calls, 1)

    def test_null_currency_is_silent(self):
        self.assertEqual(NullCurrency().lookup("GB 1-2020"), [])


if __name__ == "__main__":
    unittest.main()
