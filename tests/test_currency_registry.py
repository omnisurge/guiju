"""M2 路由：最长前缀、注册顺序、禁用与号族边界。"""

import unittest
from unittest import mock

from guiju.currency.fallback import FallbackCurrency
from guiju.currency.registry import Registry, Route, default_registry, has_code_prefix


def item(name):
    adapter = mock.Mock()
    adapter.name = name
    return adapter


class RegistryTest(unittest.TestCase):
    def test_longest_prefix_case_and_tie_registration_order(self):
        reg = Registry()
        broad, narrow, tied = item("broad"), item("narrow"), item("tied")
        reg.register(Route("broad", ("DB",), lambda: broad))
        reg.register(Route("narrow", ("DB31/T",), lambda: narrow))
        reg.register(Route("tied", ("DB31/T",), lambda: tied))
        self.assertIs(reg.match(" db31/t 792-2015 "), narrow)
        self.assertIs(reg.match("DB44/T1"), broad)
        self.assertEqual(reg.sources(), ["broad", "narrow", "tied"])

    def test_disabled_and_unimplemented_routes_are_skipped(self):
        reg = Registry()
        reg.register(Route("disabled", ("GB",), lambda: item("x"), disabled=True))
        reg.register(Route("future", ("GB",), None))
        self.assertIsNone(reg.match("GB1"))
        self.assertEqual(reg.sources(), ["disabled", "future"])

    def test_ascii_word_boundary_not_substring(self):
        for code, expected in (("GB50011", True), ("GB/T50011", True), ("GB", True),
                               ("GB 1", True), ("GBX123", False), ("GBFOO", False), ("JGJAB1", False)):
            prefix = "JGJ" if code.startswith("JGJ") else "GB"
            with self.subTest(code=code):
                self.assertEqual(has_code_prefix(code, prefix), expected)
        self.assertFalse(has_code_prefix("anything", ""))

    def test_more_specific_composite_prefix_can_be_registered_later(self):
        reg = Registry()
        reg.register(Route("dg", ("DG",), lambda: item("dg")))
        reg.register(Route("dg-tj", ("DG/TJ",), lambda: item("dg-tj")))
        self.assertEqual(reg.match("DG/TJ08-2007").name, "dg-tj")
        self.assertEqual(reg.match("DG8").name, "dg")

    def test_default_general_chains_remain_in_exact_order(self):
        reg = default_registry(client=mock.Mock())
        for code, sources in (("GB50011", ("csres", "ccsn", "std_samr")),
                              ("JGJ147", ("csres", "ccsn", "std_samr")),
                              ("CJJ1", ("csres", "ccsn", "std_samr")),
                              ("JTG D60", ("csres", "jtst")), ("DB44/T792", ("csres", "dbba"))):
            with self.subTest(code=code):
                adapter = reg.match(code)
                self.assertIsInstance(adapter, FallbackCurrency)
                self.assertEqual(adapter.source_names, sources)
                self.assertEqual(adapter.name, "csres")
        for code in ("CECS1", "GBX1", "UNKNOWN"):
            self.assertIsNone(reg.match(code))

    def test_cache_namespace_changes_with_route_or_backup_policy(self):
        reg = Registry()
        source = item("p")
        reg.register(Route("p", ("GB",), lambda: source, cache_namespace="policy-v1"))
        first = reg.namespace_for("GB1", source)
        other = Registry()
        other.register(Route("p", ("GB",), lambda: source, cache_namespace="policy-v2"))
        self.assertNotEqual(first, other.namespace_for("GB1", source))
        self.assertNotEqual(FallbackCurrency("p", source).cache_namespace,
                            FallbackCurrency("p", source, item("b")).cache_namespace)


if __name__ == "__main__":
    unittest.main()
