"""上海住建委唯一真相：复用核心、严格编号和四栏，不查询任何外源。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.currency.model import QueryCancelled, Record
from guiju.currency.native import NativeCurrency, NoCurrencySource
from guiju.currency.registry import default_registry
from guiju.currency.shzjw import (
    SHANGHAI_PREFIXES,
    ShzjwCurrency,
    is_shanghai_code,
    matches,
    normalize_code,
    owns_shanghai_namespace,
)
from guiju.domain import shzjw as official
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.store.currency import DiskCurrencyCache

BASE = "DG/TJ08-2007"
CURRENT = BASE + "-2026"
URL = "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWebApi/external/localStandard/list"


def raw_row(code=CURRENT, name="地质灾害危险性评估技术标准", **extra):
    return {"standardNumber": code, "standardProjectName": name, "releaseTime": "2026-08-10 00:00:00",
            "implementationTime": "2027-02-01 00:00:00", "standardType": "0", **extra}


def page(rows=(), total=None):
    return {"data": {"total": len(rows) if total is None else total, "list": list(rows)}}


class OfficialClient:
    def __init__(self, responses=None):
        self.responses = {} if responses is None else responses
        self.calls = []

    def post_json(self, url, payload):
        if url != URL:
            raise AssertionError("上海单源只能查询固定官方接口")
        self.calls.append(dict(payload))
        key = (payload["isAbolish"], payload["standardType"], payload["pageNum"])
        result = self.responses.get(key, page())
        if isinstance(result, Exception):
            raise result
        return json.dumps(result, ensure_ascii=False).encode("utf-8")


def client_for_current(*rows):
    return OfficialClient({(False, 0, 1): page(rows)})


class ShanghaiIdentityTest(unittest.TestCase):
    def test_only_scoped_shanghai_forms_are_supported(self):
        for prefix in SHANGHAI_PREFIXES:
            suffix = " 1010-2026" if prefix == "DB31/T" else "-144-2026"
            self.assertTrue(is_shanghai_code(prefix + suffix))
        for code in ("DG/TJ09-1", "DGJ13-1", "DBJ13-1", "DBJT13-1", "DB31/Z1", "DB31/373",
                     "DG/TJ080-1", "DBJT080-1", "DB31/TX1", "GB55037"):
            self.assertFalse(is_shanghai_code(code), code)

    def test_shared_normalizer_preserves_identity_and_source_output(self):
        self.assertIs(normalize_code, official.normalize_code)
        self.assertEqual(normalize_code(" d b j t\u00a008 − 144—2026 "), "DBJT08-144-2026")
        self.assertEqual(normalize_code("DB31 / T 1010"), "DB31/T1010")
        self.assertNotEqual(normalize_code("DB31/T1"), normalize_code("DB31/1"))

    def test_matcher_never_collapses_all_shanghai_codes_to_prefix(self):
        pairs = ((BASE, CURRENT, True), (BASE, "DG/TJ08-20070-2026", False),
                 (BASE, "DG/TJ08-9999-2026", False), (CURRENT, BASE, False),
                 (BASE + "-2020", CURRENT, False), (CURRENT, CURRENT + "-2027", False),
                 ("DBJT08-144", "DBJT08-144-2026", True),
                 ("DBJT08-144", "DBJT08-1440-2026", False),
                 ("DBJT08-144", "DBTJ08-144-2026", False),
                 ("DB31/T1010", "DB31/T 1010-2026", True),
                 ("DB31/T1010", "DB31/T10100-2026", False),
                 ("DB31/T1010-2020", "DB31/T1010-2026", False),
                 ("DGJ08-1", "DG/TJ08-1-2026", False))
        for ref, candidate, expected in pairs:
            with self.subTest(ref=ref, candidate=candidate):
                self.assertEqual(matches(ref, candidate), expected)

    def test_malformed_owned_namespace_is_reserved_not_external(self):
        registry = default_registry(client=mock.Mock())
        for code in ("DB31/373-2010", "DB31/Z1", "DB31/TX1", "DB J T 080-1", "DB J08／1"):
            with self.subTest(code=code):
                self.assertTrue(owns_shanghai_namespace(code))
                self.assertIsNone(registry.match(code))

    def test_invisible_or_control_characters_cannot_escape_to_an_external_route(self):
        registry = default_registry(client=mock.Mock())
        for code in ("DB\u200b31/T1010", "DB\u200dJT08-144", "DB31/T1010\x00", "DG/TJ08-2007\u2060"):
            with self.subTest(code=code):
                self.assertIsNone(registry.match(code))

    def test_route_normalizes_before_db_and_is_not_a_fallback_chain(self):
        client = mock.Mock()
        registry = default_registry(client)
        for code in (BASE, "d g / t j 08−2007", "db31 / t\u00a01010", "D B J T08—144", "DBTJ08-1"):
            with self.subTest(code=code):
                adapter = registry.match(code)
                self.assertIsInstance(adapter, ShzjwCurrency)
                self.assertIs(adapter.client, client)
                self.assertEqual(adapter.source_names, ("shzjw",))
                self.assertFalse(hasattr(adapter, "backups"))
        self.assertEqual(registry.match("DB44/T1010").source_names, ("csres", "dbba"))


class ShanghaiNativeTest(unittest.TestCase):
    def test_observed_official_projection_string_type_and_future_date(self):
        path = Path(__file__).parent / "fixtures/currency/shzjw_current_projection.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        client = OfficialClient({(False, 0, 1): payload})
        records = ShzjwCurrency(client).lookup(CURRENT)
        self.assertEqual(len(records), 1)
        row = records[0]
        self.assertEqual((row.code, row.name, row.status), (CURRENT, "地质灾害危险性评估技术标准", "current"))
        self.assertEqual((row.publish_date, row.implement_date), ("2026-08-10", "2027-02-01"))
        self.assertEqual((row.source, row.fallback), ("shzjw", ""))
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["condition"], CURRENT)

    def test_full_query_preserves_official_code_and_uses_one_current_bar(self):
        original = "DG/TJ 08-2007-2026"
        client = client_for_current(raw_row(original))
        record = ShzjwCurrency(client).lookup("d g/tj08−2007-2026")[0]
        self.assertEqual(record.code, original)
        self.assertEqual(len(client.calls), 1)
        self.assertFalse(client.calls[0]["isAbolish"])

    def test_only_matching_code_not_single_fuzzy_name(self):
        client = client_for_current(raw_row("DG/TJ08-9999-2026", name=CURRENT))
        self.assertEqual(ShzjwCurrency(client).lookup(CURRENT), [])
        self.assertEqual(len(client.calls), 4)

    def test_explicit_old_version_is_not_replaced_with_current_year(self):
        old = BASE + "-2020"
        client = OfficialClient({(False, 0, 1): page([raw_row(CURRENT)]),
                                 (True, 0, 1): page([raw_row(old)])})
        records = ShzjwCurrency(client).lookup(old)
        self.assertEqual([(r.code, r.status) for r in records], [(old, "withdrawn")])
        self.assertTrue(any(call["isAbolish"] for call in client.calls))

    def test_withdrawn_bar_is_authority_even_without_row_flags(self):
        client = OfficialClient({(True, 0, 1): page([raw_row()])})
        records = ShzjwCurrency(client).lookup(CURRENT)
        self.assertEqual(records[0].status, "withdrawn")
        self.assertEqual(len(client.calls), 4)

    def test_base_returns_current_and_old_and_deduplicates_same_code_current(self):
        old = BASE + "-2020"
        client = OfficialClient({(False, 0, 1): page([raw_row()]),
                                 (True, 0, 1): page([raw_row(CURRENT), raw_row(old)])})
        rows = ShzjwCurrency(client).lookup(BASE)
        self.assertEqual([(r.code, r.status) for r in rows], [(CURRENT, "current"), (old, "withdrawn")])
        self.assertEqual(len(client.calls), 4)

    def test_base_record_does_not_hide_already_found_current_year_version(self):
        for base in (BASE, "DB31/T1010"):
            latest = base + "-2026"
            client = OfficialClient({(False, 0, 1): page([raw_row(latest)]),
                                     (True, 0, 1): page([raw_row(base)])})
            with self.subTest(base=base):
                records = ShzjwCurrency(client).lookup(base)
                self.assertEqual([(r.code, r.status) for r in records],
                                 [(latest, "current"), (base, "withdrawn")])

    def test_bare_current_record_does_not_short_circuit_base_version_discovery(self):
        old = BASE + "-2020"
        client = OfficialClient({(False, 0, 1): page([raw_row(BASE), raw_row(CURRENT)]),
                                 (True, 0, 1): page([raw_row(old)])})
        records = ShzjwCurrency(client).lookup(BASE)
        self.assertEqual([(r.code, r.status) for r in records],
                         [(BASE, "current"), (CURRENT, "current"), (old, "withdrawn")])
        self.assertEqual(len(client.calls), 4)

    def test_design_priority_keeps_dbjt_and_dbtj_literal_identities(self):
        client = OfficialClient({(False, 1, 1): page([raw_row("DBJT08-144-2026", standardType="1")])})
        rows = ShzjwCurrency(client).lookup("DBJT08-144-2026")
        self.assertEqual(rows[0].code, "DBJT08-144-2026")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["standardType"], 1)
        self.assertFalse(matches("DBTJ08-144", rows[0].code))

    def test_no_ten_result_truncation_and_all_pages_are_read(self):
        rows = [raw_row(BASE + f"-{year}") for year in range(1900, 2050)]
        client = OfficialClient({(False, 0, 1): page(rows[:100], total=150),
                                 (False, 0, 2): page(rows[100:], total=150)})
        records = ShzjwCurrency(client).lookup(BASE)
        self.assertEqual(len(records), 150)
        self.assertEqual(records[0].code, rows[0]["standardNumber"])
        self.assertEqual(records[-1].code, rows[-1]["standardNumber"])
        self.assertEqual(client.calls[1]["pageNum"], 2)

    def test_partial_page_failure_is_not_empty_or_partial_success(self):
        rows = [raw_row(BASE + f"-{year}") for year in range(1900, 2000)]
        for failure in (SourceUnreachable("offline page 2"), page([], total=101)):
            client = OfficialClient({(False, 0, 1): page(rows, total=101), (False, 0, 2): failure})
            with self.subTest(failure=type(failure).__name__), self.assertRaises((SourceUnreachable, NeedsHuman)):
                ShzjwCurrency(client).lookup(BASE)
            self.assertEqual(len(client.calls), 2)

    def test_unknown_version_suffix_is_not_guessed_or_negative_cached(self):
        client = client_for_current(raw_row(BASE + "-修订版"))
        with self.assertRaises(NeedsHuman):
            ShzjwCurrency(client).lookup(BASE)

    def test_all_failures_stop_without_trying_another_bar_or_source(self):
        for failure in (SourceUnreachable("offline"), NeedsHuman("schema"), ComplianceDenied("captcha")):
            client = OfficialClient({(False, 0, 1): failure})
            with self.subTest(failure=type(failure).__name__), self.assertRaises(type(failure)):
                ShzjwCurrency(client).lookup(BASE)
            self.assertEqual(len(client.calls), 1)

    def test_cancel_between_queries_stops_the_next_request(self):
        state = {"cancelled": False}
        client = mock.Mock()

        def answer(*_args):
            state["cancelled"] = True
            return json.dumps(page()).encode()

        client.post_json.side_effect = answer
        with self.assertRaises(QueryCancelled):
            ShzjwCurrency(client, cancelled=lambda: state["cancelled"]).lookup(BASE)
        client.post_json.assert_called_once()

    def test_direct_unsupported_inputs_are_no_io(self):
        client = mock.Mock()
        for code in ("", "DG/TJ08", "DB31/T", "GB1", "DBJ13-1", "DB31/373"):
            self.assertEqual(ShzjwCurrency(client).lookup(code), [])
        client.post_json.assert_not_called()

    def test_native_success_empty_or_failure_never_calls_external_sources(self):
        targets = ["csres.CsresCurrency", "ccsn.CcsnCurrency", "stdsamr.StdSamrCurrency",
                   "jtst.JtstCurrency", "dbba.DbbaCurrency"]
        for response in (page(), SourceUnreachable("offline"), NeedsHuman("schema"), ComplianceDenied("captcha")):
            client = OfficialClient({(False, 0, 1): response})
            from contextlib import ExitStack

            with self.subTest(response=type(response).__name__), ExitStack() as stack:
                spies = [stack.enter_context(mock.patch(f"guiju.currency.{target}.lookup",
                                                        side_effect=AssertionError("上海不得请求外源"))) for target in targets]
                native = NativeCurrency(default_registry(client))
                if isinstance(response, Exception):
                    with self.assertRaises(type(response)):
                        native.query(BASE)
                else:
                    outcome = native.query(BASE)
                    self.assertEqual(outcome.results, ())
                    self.assertEqual(outcome.source, "shzjw")
                for spy in spies:
                    spy.assert_not_called()

    def test_unconfirmed_shanghai_db31_does_not_even_read_cache(self):
        cache = mock.Mock()
        client = mock.Mock()
        with self.assertRaises(NoCurrencySource):
            NativeCurrency(default_registry(client), cache).query("DB31/373-2010")
        cache.get.assert_not_called()
        cache.get_any.assert_not_called()
        client.post_json.assert_not_called()

    def test_cached_records_require_exact_shanghai_identity_source_and_status(self):
        valid = Record(CURRENT, "current", source="shzjw")
        self.assertTrue(ShzjwCurrency.accepts_record(BASE, valid))
        for record in (Record("DG/TJ08-9999-2026", "current", source="shzjw"),
                       Record(CURRENT, "current", source="csres"),
                       Record(CURRENT, "current", source="shzjw", fallback="shzjw"),
                       Record(CURRENT, "upcoming", source="shzjw"),
                       Record(CURRENT, "superseded", source="shzjw")):
            self.assertFalse(ShzjwCurrency.accepts_record(BASE, record), record)


class ShanghaiCacheIntegrationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="guiju-m3-cache-")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "cache"
        self.now = 1_800_000_000.0
        self.client = client_for_current(raw_row())
        self.registry = default_registry(self.client)
        self.cache = DiskCurrencyCache(self.directory, clock=lambda: self.now)
        self.native = NativeCurrency(self.registry, self.cache, clock=lambda: self.now)
        self.namespace = self.registry.namespace_for(BASE, self.registry.match(BASE))

    def test_fresh_disk_restart_preserves_only_official_provenance(self):
        initial = self.native.query(BASE)
        calls = len(self.client.calls)
        self.now += 5
        restarted = NativeCurrency(self.registry, DiskCurrencyCache(self.directory, clock=lambda: self.now), clock=lambda: self.now)
        outcome = restarted.query(BASE)
        self.assertTrue(outcome.from_cache)
        self.assertEqual(outcome.source, "shzjw")
        self.assertEqual(outcome.results, initial.results)
        self.assertEqual(outcome.fallback_source, "")
        self.assertEqual(outcome.critical_notes(), [])
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_stale_is_same_source_and_explicit_then_rejected_after_thirty_days(self):
        self.native.query(BASE)
        self.now += 3 * 86400
        self.client.responses = {(False, 0, 1): SourceUnreachable("官方临时不可达")}
        outcome = self.native.query(BASE)
        self.assertTrue(outcome.stale)
        self.assertEqual(outcome.source, "shzjw")
        self.assertEqual(outcome.fallback_source, "")
        self.assertEqual(len(outcome.critical_notes()), 1)
        self.assertIn("官方临时不可达", str(outcome.upstream_error))
        self.now += 28 * 86400
        with self.assertRaises(SourceUnreachable):
            self.native.query(BASE)

    def test_40_or_cancellation_never_restores_same_source_stale(self):
        self.native.query(BASE)
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.now += 3 * 86400
        self.client.responses = {(False, 0, 1): ComplianceDenied("验证码")}
        with self.assertRaises(ComplianceDenied):
            self.native.query(BASE)
        self.assertIsNone(self.native.last_outcome)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)
        native = NativeCurrency(self.registry, self.cache, clock=lambda: self.now, cancelled=lambda: True)
        with self.assertRaises(QueryCancelled):
            native.query(BASE)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)

    def test_poisoned_record_cannot_match_on_only_shanghai_prefix(self):
        self.client.responses = {(False, 0, 1): SourceUnreachable("官方不可达")}
        for bad in (Record("DG/TJ08-9999-2026", "current", source="shzjw"),
                    Record(CURRENT, "current", source="csres"),
                    Record(CURRENT, "current", source="dbba", fallback="dbba"),
                    Record(CURRENT, "current", source="shzjw", fallback="shzjw"),
                    Record(CURRENT, "superseded", source="shzjw"),
                    Record(CURRENT, "upcoming", source="shzjw")):
            self.cache.put(BASE, [bad], namespace=self.namespace)
            self.client.calls.clear()
            with self.subTest(record=bad), self.assertRaises(SourceUnreachable):
                self.native.query(BASE)
            self.assertEqual(len(self.client.calls), 1)
            self.assertIsNone(self.native.last_outcome)

    def test_old_external_namespace_is_ignored_not_renamed_to_shanghai(self):
        from guiju.currency.cache import CACHE_NAMESPACE

        query = "DB31/T1010"
        old_namespace = json.dumps([CACHE_NAMESPACE, "csres", ["DB"], "fallback:csres>dbba"],
                                   ensure_ascii=True, separators=(",", ":"))
        self.cache.put(query, [Record(query, "current", source="dbba", fallback="dbba")], namespace=old_namespace)
        outcome = self.native.query(query)
        self.assertFalse(outcome.from_cache)
        self.assertEqual(outcome.source, "shzjw")
        self.assertEqual(outcome.results, ())
        self.assertEqual(outcome.fallback_source, "")
        self.assertTrue(self.client.calls)

    def test_successful_empty_replaces_expired_official_positive_result(self):
        self.native.query(BASE)
        self.now += 86400
        self.client.responses = {}
        outcome = self.native.query(BASE)
        self.assertEqual(outcome.results, ())
        self.assertFalse(outcome.stale)
        calls = len(self.client.calls)
        again = self.native.query(BASE)
        self.assertTrue(again.from_cache)
        self.assertEqual(again.results, ())
        self.assertEqual(len(self.client.calls), calls)


if __name__ == "__main__":
    unittest.main()
