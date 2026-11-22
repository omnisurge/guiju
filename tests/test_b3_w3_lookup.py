"""B3-W3-8：_by_code 部分空结果不能伪装成 NotIndexed(31)。"""

import unittest

from guiju.cli import lookup
from guiju.currency.model import Record
from guiju.errors import NeedsHuman, NotIndexed


class _FakeStore:
    def find_by_code(self, key):
        return []

    def catalog_entries(self):
        return []


class _FakeCurrency:
    name = "fake"

    def __init__(self, records=None, complete=False, reason=""):
        self.records = records or []
        self.complete = complete
        self.incomplete_reason = reason

    def lookup(self, code):
        return list(self.records)


class ByCodeCompletenessTest(unittest.TestCase):
    def _run(self, currency):
        return lookup._by_code(_FakeStore(), "GB 9999-2020", currency)

    def test_incomplete_empty_is_needs_human_50(self):
        with self.assertRaises(NeedsHuman) as ctx:
            self._run(_FakeCurrency([], complete=False, reason="翻页截断"))
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("不完整", str(ctx.exception))
        self.assertEqual(ctx.exception.extra["currency_complete"], False)
        self.assertEqual(ctx.exception.extra["currency_incomplete_reason"], "翻页截断")

    def test_incomplete_nonempty_without_target_is_needs_human_50(self):
        records = [Record(code="GB 50011-2010", status="current", name="抗震规范")]
        with self.assertRaises(NeedsHuman) as ctx:
            self._run(_FakeCurrency(records, complete=False, reason="无总页数"))
        self.assertEqual(ctx.exception.exit, 50)

    def test_complete_empty_remains_not_indexed_31(self):
        with self.assertRaises(NotIndexed) as ctx:
            self._run(_FakeCurrency([], complete=True))
        self.assertEqual(ctx.exception.exit, 31)

    def test_unknown_completeness_remains_not_indexed_31(self):
        with self.assertRaises(NotIndexed) as ctx:
            self._run(_FakeCurrency([], complete=None))
        self.assertEqual(ctx.exception.exit, 31)


if __name__ == "__main__":
    unittest.main()
