"""1.2.0a4 W3-1/W3-2/W3-3/W3-7 回归：完整性合并、csres 权威空、年份降级、显式配置不读旧缓存。

这些用例按修复前缺陷编写：在 f98e0d2（1.2.0a3）上应当失败。
"""

from __future__ import annotations

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.currency import CachedCurrency, MisconfiguredCurrency, _cache
from guiju.currency.ccsn import CcsnCurrency
from guiju.currency.codes import query_with_base_fallback
from guiju.currency.contract import Completeness
from guiju.currency.csres import CsresCurrency
from guiju.currency.dbba import DbbaCurrency
from guiju.currency.fallback import FallbackCurrency
from guiju.currency.jtst import JtstCurrency
from guiju.currency.model import Record
from guiju.currency.native import NativeCurrency
from guiju.currency.registry import Registry, Route
from guiju.currency.stdsamr import StdSamrCurrency
from guiju.domain.codes import Confidence, Mention, Role, _demote_older
from guiju.domain.currency_check import CurrencyCheck, CurrencyState, empty_authoritative_of
from guiju.errors import NeedsHuman, SourceUnreachable
from guiju.store.sqlite import Store

CODE = "GB 50011-2010"
NOW = 1_700_000_000.0


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(
            socket.socket, "connect",
            side_effect=AssertionError("offline test attempted a real socket connection"),
        )
        guard.start()
        self.addCleanup(guard.stop)


class FakeClient:
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


def samr_body(rows=(), total=0):
    return json.dumps({"total": total, "rows": list(rows)}, ensure_ascii=False).encode("utf-8")


def dbba_body(records=(), total=0):
    return json.dumps({"total": total, "records": list(records)}, ensure_ascii=False).encode("utf-8")


def ccsn_body(rows=(), total=0, has_next=False):
    return json.dumps(
        {"Total": total, "List": list(rows), "HasNextPage": has_next},
        ensure_ascii=False,
    ).encode("utf-8")


def samr_row(code="GB/T 9999-2020"):
    return {"STATE": "现行", "C_STD_CODE": code, "C_C_NAME": "某标准", "ISSUE_DATE": "", "ACT_DATE": ""}


def dbba_row(code="GB/T 9999-2020"):
    return {"code": code, "chName": "某标准", "status": "现行", "reviseStdCodes": "",
            "actDate": 0, "issueDate": 0, "fzDate": 0}


def ccsn_row(code="GB/T 9999-2020"):
    return {"StandardCode": code, "StandardCNName": "某标准", "StandardState": "现行",
            "AbolishDate": "", "ReplaceStandardCode": "", "PublishDate": "", "PerformDate": ""}


class BaseFallbackCompletenessTest(OfflineTestCase):
    """W3-1：第一次检索不完整时，base fallback 的 complete=True 不能把它洗白。"""

    def test_first_page_incomplete_wins_over_complete_base(self):
        cases = (
            (StdSamrCurrency, lambda rows, total: samr_body(rows, total)),
            (DbbaCurrency, lambda rows, total: dbba_body(rows, total)),
            (CcsnCurrency, lambda rows, total: ccsn_body(rows, total)),
        )
        for adapter_cls, body in cases:
            with self.subTest(adapter=adapter_cls.__name__):
                client = FakeClient(body([samr_row() if adapter_cls is StdSamrCurrency else
                                          dbba_row() if adapter_cls is DbbaCurrency else
                                          ccsn_row()], total=2),
                                    body([], total=0))
                cur = adapter_cls(client)
                self.assertEqual(cur.lookup(CODE), [])
                self.assertEqual(len(client.calls), 2, "第一次空结果后必须再查基础号")
                self.assertFalse(cur.complete, "第一次 total>rows 的不完整证据不能被 base 的 complete=True 覆盖")
                self.assertIn("2", cur.incomplete_reason, "不完整原因应来自第一次检索（源声称共 2 条）")

    def test_first_page_complete_stays_complete_when_base_empty(self):
        cases = (
            (StdSamrCurrency, lambda rows, total: samr_body(rows, total)),
            (DbbaCurrency, lambda rows, total: dbba_body(rows, total)),
            (CcsnCurrency, lambda rows, total: ccsn_body(rows, total)),
        )
        for adapter_cls, body in cases:
            with self.subTest(adapter=adapter_cls.__name__):
                client = FakeClient(body([], total=0), body([], total=0))
                cur = adapter_cls(client)
                self.assertEqual(cur.lookup(CODE), [])
                self.assertTrue(cur.complete)

    def test_jtst_base_fallback_keeps_incomplete_state(self):
        first = '<table><tr><td><a><span class="en-code">JTG D99-2020</span>某标准</a></td></tr></table>'.encode()
        second = b"<table></table>"
        client = FakeClient(first, second)
        cur = JtstCurrency(client)
        self.assertEqual(cur.lookup("JTG D60-2015"), [])
        self.assertEqual(len(client.calls), 2)
        self.assertFalse(cur.complete, "jtst 没有总数证据，base 的空页也不能证明完整")

    def test_snapshot_restore_contract_and_legacy_call_without_callbacks(self):
        state = {"value": Completeness(has_more=False)}

        def fetch(term):
            if term == CODE:
                state["value"] = Completeness(total=9, has_more=True, reason="第一次未取全")
                return []
            state["value"] = Completeness(total=0, has_more=False)
            return []

        rows = query_with_base_fallback(
            CODE, fetch,
            snapshot=lambda: state["value"],
            restore=lambda value: state.__setitem__("value", value),
        )
        self.assertEqual(rows, [])
        self.assertFalse(state["value"].complete)
        self.assertEqual(state["value"].reason, "第一次未取全")

        # 旧调用方不传任何回调也必须继续工作（不得 TypeError）。
        legacy = query_with_base_fallback(
            CODE,
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
                self.entries[(namespace, code)] = (NOW, tuple(records))

        client = FakeClient(samr_body([samr_row()], total=2), samr_body([], total=0))
        adapter = StdSamrCurrency(client)
        registry = Registry()
        registry.register(Route("std_samr", ("GB",), lambda: adapter))
        native = NativeCurrency(registry, RecordingCache(), clock=lambda: NOW)
        outcome = native.query(CODE)
        self.assertEqual(outcome.results, ())
        self.assertFalse(outcome.complete)
        self.assertEqual(native.cache.puts, 0)


class CsresEmptyAuthoritativeTest(OfflineTestCase):
    """W3-2：csres 的 pageNum=0 是权威空结果，不允许被当成“没取全”再升 50。"""

    @staticmethod
    def empty_page(*, total=None, pages=None, page=0, page_size=25):
        html = ["<html><body><form>"]
        if total is not None:
            html.append(f"<span>共有 {total} 条符合状态条件的标准，共 {pages} 页</span>")
        html.append(f'<input type="hidden" name="pageSize" value="{page_size}">')
        if page is not None:
            html.append(f'<input type="hidden" name="pageNum" value="{page}">')
        html.append("</form></body></html>")
        return "".join(html).encode("gbk")

    @staticmethod
    def nonempty_page(code="GB 50011-2010"):
        return (
            '<html><body><form>'
            f'<tr title="编号：{code} &#xA;发布日期：2020-01-01"><td>{code}</td><td>名称</td>'
            "<td>部门</td><td>2020-01-01</td><td>现行</td></tr>"
            '<span>共有 1 条符合状态条件的标准，共 1 页</span>'
            '<input type="hidden" name="pageSize" value="25">'
            '<input type="hidden" name="pageNum" value="1">'
            "</form></body></html>"
        ).encode("gbk")

    def test_https_zero_hit_with_total_is_authoritative_empty_and_complete(self):
        cur = CsresCurrency(FakeClient(self.empty_page(total=0, pages=0)))
        self.assertEqual(cur.lookup("GB 99999-2099"), [])
        self.assertTrue(cur.empty_authoritative)
        self.assertTrue(cur.complete)
        self.assertIs(cur.pagination["has_more"], False)

    def test_https_zero_hit_without_total_or_pages_is_authoritative_empty(self):
        cur = CsresCurrency(FakeClient(self.empty_page(total=None, pages=None, page=0)))
        self.assertEqual(cur.lookup("GB 99999-2099"), [])
        self.assertTrue(cur.empty_authoritative)
        self.assertTrue(cur.complete, "pageNum=0 且无记录是源明确回答无结果，不能因缺总数判不完整")
        self.assertIs(cur.pagination["has_more"], False)

    def test_plaintext_zero_hit_is_empty_authoritative_but_not_complete(self):
        client = FakeClient(SourceUnreachable("tls blocked"), self.empty_page(total=0, pages=0))
        cur = CsresCurrency(client, plaintext_fallback=True)
        with self.assertWarns(RuntimeWarning):
            self.assertEqual(cur.lookup("GB 99999-2099"), [])
        self.assertTrue(cur.empty_authoritative)
        self.assertFalse(cur.complete, "明文回退仍不得作为权威缓存答案")
        self.assertTrue(cur.plaintext)

    def test_plaintext_nonempty_is_not_empty_authoritative(self):
        client = FakeClient(SourceUnreachable("tls blocked"), self.nonempty_page())
        cur = CsresCurrency(client, plaintext_fallback=True)
        with self.assertWarns(RuntimeWarning):
            rows = cur.lookup("GB 50011-2010")
        self.assertTrue(rows)
        self.assertFalse(cur.empty_authoritative)
        self.assertFalse(cur.complete)

    def test_page_zero_with_positive_total_is_contradiction_50(self):
        client = FakeClient(self.empty_page(total=5, pages=1, page=0))
        with self.assertRaises(NeedsHuman) as ctx:
            CsresCurrency(client).lookup("GB 99999-2099")
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("自相矛盾", str(ctx.exception))

    def test_empty_authoritative_projects_through_native_and_cached_currency(self):
        class StubAdapter:
            name = "p"
            complete = False
            empty_authoritative = True
            incomplete_reason = "明文回退"

            def __init__(self):
                self.calls = 0

            def lookup(self, code):
                self.calls += 1
                return []

        class RecordingCache:
            def __init__(self):
                self.puts = 0

            def get(self, code, *, namespace=None):
                return None

            def get_any(self, code, *, namespace=None):
                return None

            def put(self, code, records, *, namespace=None):
                self.puts += 1

        adapter = StubAdapter()
        registry = Registry()
        registry.register(Route("p", ("GB",), lambda: adapter))
        native = NativeCurrency(registry, RecordingCache(), clock=lambda: NOW)
        outcome = native.query(CODE)
        self.assertEqual(outcome.results, ())
        self.assertFalse(outcome.complete)
        self.assertTrue(outcome.empty_authoritative)
        self.assertTrue(native.empty_authoritative)

        wrapped = CachedCurrency(native)
        wrapped.lookup(CODE)
        self.assertTrue(wrapped.empty_authoritative)
        self.assertTrue(empty_authoritative_of(wrapped))

    def test_plaintext_empty_is_not_written_to_cache(self):
        class RecordingCache:
            def __init__(self):
                self.puts = 0

            def get(self, code, *, namespace=None):
                return None

            def get_any(self, code, *, namespace=None):
                return None

            def put(self, code, records, *, namespace=None):
                self.puts += 1

        client = FakeClient(SourceUnreachable("tls blocked"), self.empty_page(total=0, pages=0))
        adapter = CsresCurrency(client, plaintext_fallback=True)
        registry = Registry()
        registry.register(Route("csres", ("GB",), lambda: FallbackCurrency("csres", adapter, None)))
        native = NativeCurrency(registry, RecordingCache(), clock=lambda: NOW)
        with self.assertWarns(RuntimeWarning):
            outcome = native.query("GB 99999-2099")
        self.assertEqual(outcome.results, ())
        self.assertFalse(outcome.complete)
        self.assertTrue(outcome.empty_authoritative)
        self.assertEqual(native.cache.puts, 0, "明文空结果不得写缓存")

    def test_fallback_currency_inherits_empty_authoritative(self):
        client = FakeClient(SourceUnreachable("tls blocked"), self.empty_page(total=0, pages=0))
        chain = FallbackCurrency(
            "csres", CsresCurrency(client, plaintext_fallback=True), None,
        )
        with self.assertWarns(RuntimeWarning):
            self.assertEqual(chain.lookup("GB 99999-2099"), [])
        self.assertFalse(chain.complete)
        self.assertTrue(chain.empty_authoritative, "FallbackCurrency 必须把权威空标记传给上层")

    def test_currency_check_keeps_verified_semantics(self):
        check = CurrencyCheck(state=CurrencyState.OK, complete=False, empty_authoritative=True)
        self.assertFalse(check.verified)
        self.assertTrue(check.needs_warning)
        check2 = CurrencyCheck(state=CurrencyState.OK, complete=True, empty_authoritative=True)
        self.assertTrue(check2.verified)
        self.assertFalse(check2.needs_warning)


class DemoteOlderYearTest(unittest.TestCase):
    """W3-3：MENTIONED/TARGET_OF_REVISION/PRIMARY 且年份严格更早才降级。"""

    @staticmethod
    def mention(code, role):
        return Mention(code, code.replace("-", " "), role, "证据")

    def roles(self, primary_key, mentions):
        primary = Mention(primary_key, primary_key, Role.PRIMARY, "发布")
        return {m.code_key: m.role for m in _demote_older(primary, mentions)}

    def test_newer_mentioned_is_not_demoted(self):
        roles = self.roles("GB/T50362-2010", [self.mention("GB/T50362-2022", Role.MENTIONED)])
        self.assertIs(roles["GB/T50362-2022"], Role.MENTIONED)

    def test_older_mentioned_is_demoted(self):
        roles = self.roles("GB/T50362-2022", [self.mention("GB/T50362-2005", Role.MENTIONED)])
        self.assertIs(roles["GB/T50362-2005"], Role.SUPERSEDED_REF)

    def test_older_target_of_revision_is_demoted(self):
        roles = self.roles("GB/T50362-2022", [self.mention("GB/T50362-2005", Role.TARGET_OF_REVISION)])
        self.assertIs(roles["GB/T50362-2005"], Role.SUPERSEDED_REF)

    def test_older_primary_is_demoted(self):
        # 同页两个“现批准”：旧 primary 也必须让位给同族最新 primary。
        roles = self.roles("GB/T50362-2022", [self.mention("GB/T50362-2010", Role.PRIMARY)])
        self.assertIs(roles["GB/T50362-2010"], Role.SUPERSEDED_REF)

    def test_same_year_missing_year_and_non_numeric_are_not_demoted(self):
        for candidate in ("GB50016-2014", "GB50016", "GB50016-20X4"):
            with self.subTest(candidate=candidate):
                roles = self.roles("GB50016-2014", [self.mention(candidate, Role.MENTIONED)])
                self.assertIs(roles[candidate], Role.MENTIONED)

    def test_weak_primary_older_is_not_demoted(self):
        roles = self.roles("GB/T50362-2022", [self.mention("GB/T50362-2005", Role.WEAK_PRIMARY)])
        self.assertIs(roles["GB/T50362-2005"], Role.WEAK_PRIMARY)

    def test_ambiguous_classification_does_not_demote(self):
        from guiju.domain.codes import classify

        codes = classify("《建筑拆除工程安全技术规范》JGJ147-2016与JGJ147-2004均已列入目录。")
        self.assertEqual(codes.confidence, Confidence.AMBIGUOUS)
        roles = {m.code_key: m.role for m in codes.mentions}
        self.assertIs(roles["JGJ147-2004"], Role.MENTIONED)


class MisconfiguredCurrencyCacheTest(unittest.TestCase):
    """W3-7：显式配置错误时必须报 20，不能拿旧缓存回答。"""

    def setUp(self):
        _cache.clear()
        self.addCleanup(_cache.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)

    def test_misconfigured_currency_never_reads_cached_records(self):
        payload = json.dumps([
            {"code": "GB 50016-2014", "status": "current", "name": "旧缓存里的规范",
             "implement_date": "", "source": "csres", "replaces": "", "fallback": ""}
        ], ensure_ascii=False)
        self.store.cache_currency("GB50016-2014", payload)
        self.store.commit()
        cur = CachedCurrency(
            inner=MisconfiguredCurrency("xyz(未知)", "GUIJU_CURRENCY=xyz 无法识别"),
            store=self.store,
        )
        with self.assertRaises(SourceUnreachable) as ctx:
            cur.lookup("GB 50016-2014")
        self.assertIn("无法识别", str(ctx.exception))
        self.assertFalse(cur.from_cache)
