"""各时效上游适配器、form_post 路由白名单、权威空结果与同族年份降级边界测试。"""

from __future__ import annotations

import json
import socket
import unittest
import warnings
from unittest import mock

from guiju.currency import CachedCurrency
from guiju.currency.ccsn import SEARCH_URL, CcsnCurrency, status_ccsn
from guiju.currency.csres import CsresCurrency, parse_csres
from guiju.currency.dbba import DbbaCurrency
from guiju.currency.fallback import FallbackCurrency
from guiju.currency.jtst import JtstCurrency, parse_jtst
from guiju.currency.native import NativeCurrency
from guiju.currency.registry import Registry, Route, default_registry
from guiju.currency.shzjw import _valid_query, is_shanghai_code, normalize_code
from guiju.currency.stdsamr import StdSamrCurrency
from guiju.domain.codes import Confidence, Mention, Role, _demote_older
from guiju.domain.currency_check import CurrencyCheck, CurrencyState, empty_authoritative_of
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.net.endpoints import DEFAULT_METADATA_FORM_HOSTS
from guiju.net.metadata import MetadataClient
from guiju.net.throttle import Throttle

CODE = "GB 50011-2010"
NOW = 1_700_000_000.0


class _SingleBodyClient:
    def __init__(self, body=b""):
        self.body = body
        self.closed = False

    def get(self, url, *, accept=None):
        return self.body

    def post_form(self, url, data, *, accept=None):
        return self.body

    def close(self):
        self.closed = True


class _RecordingFormClient:
    def __init__(self, payload):
        self.payload = payload
        self.posts = []
        self.closed = 0

    def post_form(self, url, form, accept=None):
        self.posts.append((url, dict(form)))
        return self.payload

    def close(self):
        self.closed += 1


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


class _RecordingCache:
    def __init__(self):
        self.puts = 0

    def get(self, code, *, namespace=None):
        return None

    def get_any(self, code, *, namespace=None):
        return None

    def put(self, code, records, *, namespace=None):
        self.puts += 1


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(
            socket.socket, "connect",
            side_effect=AssertionError("offline test attempted a real socket connection"),
        )
        guard.start()
        self.addCleanup(guard.stop)


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


class L1BareDb31Test(unittest.TestCase):
    def test_bare_db31_accepted_as_shanghai(self):
        for code in ("DB3108-2020", "DB31-08-2020", "DB3108", "DB31/T 08-2020", "DB31/T08-2020"):
            self.assertTrue(is_shanghai_code(code), code)
            self.assertTrue(_valid_query(normalize_code(code)), code)  # _valid_query 契约：吃规范化后的 key

    def test_db31_slash_forms_still_rejected(self):
        for code in ("DB31/373-2010", "DB31/373", "DB31/Z1", "DB31/TX1", "DB31X"):
            self.assertFalse(is_shanghai_code(code), code)

    def test_default_registry_routes_bare_db31_to_shzjw(self):
        registry = default_registry()
        adapter = registry.match("DB3108-2020")
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.name, "shzjw")

    def test_default_registry_db31_slash_matches_nothing(self):
        # DB31/… 被上海路由拒绝（code_filter）且裸 DB 路由也排除（owns_shanghai_namespace）：
        # 结论是无源可答，不能偷偷送去外源 DB。
        registry = default_registry()
        self.assertIsNone(registry.match("DB31/373"))


class R4FormPostRegistryDrivenTest(unittest.TestCase):
    """_FORM_HOSTS 常量已删：放行与否只看 Route(hosts=…, form_post=True)。"""

    def test_default_registry_flags_ccsn_and_dbba_only(self):
        reg = default_registry()
        self.assertEqual(DEFAULT_METADATA_FORM_HOSTS, frozenset({"www.ccsn.org.cn", "dbba.sacinfo.org.cn"}))
        for host in DEFAULT_METADATA_FORM_HOSTS:
            self.assertTrue(reg.form_post_for_host(host))
        for host in ("www.csres.com", "std.samr.gov.cn", "jtst.mot.gov.cn",
                     "bzjg.zjw.sh.gov.cn", "unlisted.example.org"):
            with self.subTest(host=host):
                self.assertFalse(reg.form_post_for_host(host))

    def test_injected_route_with_form_post_is_honored(self):
        # 判定由 Route 数据驱动而非写死：手造一条 form_post=True 的 Route 即被采信。
        reg = Registry()
        reg.register(Route("x", (), None, hosts=("www.csres.com",), form_post=True))
        self.assertTrue(reg.form_post_for_host("www.csres.com"))
        self.assertFalse(reg.form_post_for_host("other.example.org"))
        reg2 = Registry()
        reg2.register(Route("y", (), None, hosts=("www.csres.com",)))  # form_post 缺省 False
        self.assertFalse(reg2.form_post_for_host("www.csres.com"))

    def test_registered_form_host_passes_gate_and_posts(self):
        client = MetadataClient(throttle=Throttle(0))
        with mock.patch.object(client, "_request", return_value=b"{}") as req:
            client.post_form("https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard",
                             {"key": "GB 50011"})
        req.assert_called_once()

    def test_non_form_host_keeps_original_deny_and_get_unaffected(self):
        client = MetadataClient(throttle=Throttle(0))
        # 非 form_post 端点：仍走拒发分支（GET 行为不变，不打上游即可验证）
        with self.assertRaises(ComplianceDenied):
            client.post_form("https://www.csres.com/s.jsp", {"key": "GB 50011"})
        with self.assertRaises(ComplianceDenied):
            client.post_form("https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWebApi/external/localStandard/list",
                             {"key": "DG/TJ08-2007"})
        with self.assertRaises(ComplianceDenied):
            client.post_form("https://bzjg.zjw.sh.gov.cn/JGBStandardWebApi/external/localStandard/list",
                             {"key": "DG/TJ08-2007"})

    def test_endpoint_routes_do_not_steal_code_matches(self):
        # 端点 Route 的 prefixes=()，不得让任何编号匹配到它（factory=None 否则 match 会崩）。
        reg = default_registry()
        for code in ("GB50011", "DB44/T792", "CJJ1-2008"):
            with self.subTest(code=code):
                adapter = reg.match(code)
                self.assertIsNotNone(adapter)
                self.assertNotIn(adapter.name if hasattr(adapter, "name") else "", ("ccsn", "dbba"))
        self.assertIsNone(reg.match("XX 999"))


class L5JtstTest(unittest.TestCase):
    def test_empty_result_page_no_warning(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            records = parse_jtst(b"<html><body><table></table></body></html>")
        self.assertEqual(records, [])
        self.assertFalse(any("jtst" in str(w.message) for w in seen))

    def test_rows_present_but_unparseable_is_needs_human(self):
        # W3/P1-4：有数据痕迹（en-code 标记在）却 0 条可解析 = 改版，不是"没有"，必须 50。
        html = '<table><tr><td><span class="en-code">&nbsp;</span></td></tr></table>'
        with self.assertRaises(NeedsHuman) as ctx:
            parse_jtst(html.encode())
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("0 条可解析", str(ctx.exception))

    def test_pagination_honest_and_close(self):
        client = _SingleBodyClient(b"<table></table>")
        jtst = JtstCurrency(client)
        self.assertEqual(jtst.pagination["fetched_pages"], 0)
        self.assertIsNone(jtst.pagination["total"])
        self.assertIsNone(jtst.pagination["has_more"])
        jtst.lookup("JTG 1")
        self.assertEqual(jtst.pagination["fetched_pages"], 1)
        self.assertIsNone(jtst.pagination["total"])      # HTML 页无总数：None 而非猜 0
        self.assertIsNone(jtst.pagination["has_more"])   # 无法判定不猜 False
        self.assertFalse(jtst.complete)                  # 无完整性证据 → 不得写负缓存
        jtst.close()
        self.assertTrue(client.closed)

    def test_context_manager_closes(self):
        client = _SingleBodyClient(b"")
        with JtstCurrency(client):
            pass
        self.assertTrue(client.closed)


class L6CsresTest(unittest.TestCase):
    def test_data_rows_but_zero_records_is_needs_human(self):
        # W3/P1-4：有 title 数据痕迹但解析不出记录 = 改版，必须 50，不能静默成无命中。
        # parse_csres 按 GBK 解码：title 标记必须用 GBK 字节，utf-8 的"编号："会被解成乱码。
        html = ('<table><tr title="编号：GB 1"><td>a</td><td>b</td><td>c</td></tr>'
                '<tr title="编号：GB 2"><td>a</td><td>b</td><td>c</td></tr></table>')
        with self.assertRaises(NeedsHuman) as ctx:
            parse_csres(html.encode("gbk", "replace"))
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("csres", str(ctx.exception))

    def test_structured_rows_without_marker_is_needs_human(self):
        html = "<table><tr><td>a</td><td>b</td><td>c</td><td>d</td></tr></table>"
        with self.assertRaises(NeedsHuman) as ctx:
            parse_csres(html.encode())
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("csres", str(ctx.exception))

    def test_truly_empty_page_no_warning(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            records = parse_csres(b"<html><body></body></html>")
        self.assertEqual(records, [])
        self.assertFalse(any("csres" in str(w.message) for w in seen))

    def test_pagination_and_close(self):
        client = _SingleBodyClient(b"<html></html>")
        csres = CsresCurrency(client)
        csres.lookup("GB 1")
        self.assertEqual(csres.pagination["fetched_pages"], 1)
        self.assertIsNone(csres.pagination["has_more"])
        csres.close()
        self.assertTrue(client.closed)
        client2 = _SingleBodyClient(b"")
        with CsresCurrency(client2):
            pass
        self.assertTrue(client2.closed)


class L6StdSamrTest(unittest.TestCase):
    def test_total_exceeds_page_warns(self):
        payload = json.dumps({"total": 25, "rows": [
            {"C_STD_CODE": "GB 1-2000", "C_C_NAME": "标准", "STATE": "现行",
             "ISSUE_DATE": "2000-01-01", "ACT_DATE": "2000-06-01"}]})
        client = _SingleBodyClient(payload.encode())
        samr = StdSamrCurrency(client)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            rows = samr.lookup("GB 1-2000")
        self.assertEqual(samr.pagination["total"], 25)
        self.assertTrue(samr.pagination["has_more"])
        self.assertTrue(any("std_samr" in str(w.message) for w in seen))
        self.assertEqual(len(rows), 1)

    def test_total_zero_no_warning(self):
        client = _SingleBodyClient(json.dumps({"total": 0, "rows": []}).encode())
        samr = StdSamrCurrency(client)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            rows = samr.lookup("GB 9999")
        self.assertEqual(rows, [])
        self.assertFalse(any("std_samr" in str(w.message) for w in seen))

    def test_total_positive_but_empty_records_is_needs_human(self):
        # W3/P1-4：源声称 3 条却一行没给 = 结构不符（不是"没有"）。
        client = _SingleBodyClient(json.dumps({"total": 3, "rows": []}).encode())
        with self.assertRaises(NeedsHuman) as ctx:
            StdSamrCurrency(client).lookup("GB 1")
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("total=3", str(ctx.exception))

    def test_close_and_context(self):
        client = _SingleBodyClient(b"")
        samr = StdSamrCurrency(client)
        samr.close()
        self.assertTrue(client.closed)


class L6DbbaTest(unittest.TestCase):
    def test_total_exceeds_page_warns(self):
        payload = json.dumps({"total": 30, "records": [
            {"code": "DB 1-2020", "chName": "标准", "status": "现行",
             "reviseStdCodes": "", "actDate": 1, "issueDate": 1, "fzDate": 0}]})
        client = _SingleBodyClient(payload.encode())
        dbba = DbbaCurrency(client)
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            rows = dbba.lookup("DB 1")
        self.assertEqual(dbba.pagination["total"], 30)
        self.assertTrue(dbba.pagination["has_more"])
        self.assertTrue(any("dbba" in str(w.message) for w in seen))
        self.assertEqual(len(rows), 1)

    def test_total_positive_empty_records_is_needs_human(self):
        # W3/P1-4：源声称 2 条却一行没给 = 结构不符（不是"没有"）。
        client = _SingleBodyClient(json.dumps({"total": 2, "records": []}).encode())
        with self.assertRaises(NeedsHuman) as ctx:
            DbbaCurrency(client).lookup("DB 9")
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("total=2", str(ctx.exception))

    def test_no_json_accept_header_and_close(self):
        seen_accept = []

        class C(_SingleBodyClient):
            def post_form(self, url, data, *, accept=None):
                seen_accept.append(accept)
                return json.dumps({"total": 0, "records": []}).encode()

        dbba = DbbaCurrency(C())
        dbba.lookup("DB 1")
        self.assertEqual(seen_accept, [None])  # dbba 禁带 Accept: application/json
        dbba.close()
        self.assertTrue(dbba.client.closed)


class M18CcsnPaginationTest(unittest.TestCase):
    def test_total_missing_is_needs_human(self):
        # W3/P1-4：Total 是必需字段；缺 key 不能再被当成"total=0 的权威空结果"缓存。
        client = _RecordingFormClient(json.dumps({"List": []}).encode())
        with self.assertRaises(NeedsHuman):
            CcsnCurrency(client).lookup("CECS 1-2020")

    def test_more_than_page_marks_has_more_without_truncation(self):
        # 老响应不带 HasNextPage：Total>已取 → has_more（服务端声称未完），
        # 但不翻页也不告警（可能服务端本就只回这么多）；truncated 只在被
        # MAX_PAGES 硬截断时为 True。
        client = _RecordingFormClient(_ccsn_payload(total=40, rows=20))
        cur = CcsnCurrency(client)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            cur.lookup("CECS 1-2020")
        self.assertTrue(cur.pagination["has_more"])
        self.assertEqual(cur.pagination["fetched_pages"], 1)
        self.assertFalse(cur.pagination["truncated"])
        self.assertFalse(any("截断" in str(w.message) for w in caught))

    def test_assx_url_kept_as_is(self):
        self.assertIn(".assx", SEARCH_URL)

    def test_close_and_context_manager(self):
        client = _RecordingFormClient(json.dumps({"Total": 0, "List": []}).encode())
        with CcsnCurrency(client) as cur:
            cur.lookup("CECS 1-2020")
        self.assertEqual(client.closed, 1)


class M20bCcsnStatusTest(unittest.TestCase):
    def test_burevision_done_is_unknown_not_superseded(self):
        # 「局修完成」只说明一次局部修订流程结束：既不能证明被代替也不能证明仍现行。
        self.assertEqual(status_ccsn("局修完成", "2016-08-01"), "unknown")
        self.assertEqual(status_ccsn("局修完成", ""), "unknown")

    def test_other_unknown_words_keep_abolish_fallback(self):
        # W3/P2-18：没有 ReplaceStandardCode 的废止证据只能判废止，不能判"被替代"。
        self.assertEqual(status_ccsn("奇怪状态", "2016-08-01"), "withdrawn")
        self.assertEqual(status_ccsn("奇怪状态", "2016-08-01", "GB 1-2020"), "superseded")
        self.assertEqual(status_ccsn("奇怪状态", ""), "unknown")


class CsresEmptyAuthoritativeTest(OfflineTestCase):
    """W3-2：csres 的 pageNum=0 是权威空结果，不允许被当成"没取全"再升 50。"""

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
        cur = CsresCurrency(_SequenceClient(self.empty_page(total=0, pages=0)))
        self.assertEqual(cur.lookup("GB 99999-2099"), [])
        self.assertTrue(cur.empty_authoritative)
        self.assertTrue(cur.complete)
        self.assertIs(cur.pagination["has_more"], False)

    def test_https_zero_hit_without_total_or_pages_is_authoritative_empty(self):
        cur = CsresCurrency(_SequenceClient(self.empty_page(total=None, pages=None, page=0)))
        self.assertEqual(cur.lookup("GB 99999-2099"), [])
        self.assertTrue(cur.empty_authoritative)
        self.assertTrue(cur.complete, "pageNum=0 且无记录是源明确回答无结果，不能因缺总数判不完整")
        self.assertIs(cur.pagination["has_more"], False)

    def test_plaintext_zero_hit_is_empty_authoritative_but_not_complete(self):
        client = _SequenceClient(SourceUnreachable("tls blocked"), self.empty_page(total=0, pages=0))
        cur = CsresCurrency(client, plaintext_fallback=True)
        with self.assertWarns(RuntimeWarning):
            self.assertEqual(cur.lookup("GB 99999-2099"), [])
        self.assertTrue(cur.empty_authoritative)
        self.assertFalse(cur.complete, "明文回退仍不得作为权威缓存答案")
        self.assertTrue(cur.plaintext)

    def test_plaintext_nonempty_is_not_empty_authoritative(self):
        client = _SequenceClient(SourceUnreachable("tls blocked"), self.nonempty_page())
        cur = CsresCurrency(client, plaintext_fallback=True)
        with self.assertWarns(RuntimeWarning):
            rows = cur.lookup("GB 50011-2010")
        self.assertTrue(rows)
        self.assertFalse(cur.empty_authoritative)
        self.assertFalse(cur.complete)

    def test_page_zero_with_positive_total_is_contradiction_50(self):
        client = _SequenceClient(self.empty_page(total=5, pages=1, page=0))
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

        adapter = StubAdapter()
        registry = Registry()
        registry.register(Route("p", ("GB",), lambda: adapter))
        native = NativeCurrency(registry, _RecordingCache(), clock=lambda: NOW)
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
        client = _SequenceClient(SourceUnreachable("tls blocked"), self.empty_page(total=0, pages=0))
        adapter = CsresCurrency(client, plaintext_fallback=True)
        registry = Registry()
        registry.register(Route("csres", ("GB",), lambda: FallbackCurrency("csres", adapter, None)))
        native = NativeCurrency(registry, _RecordingCache(), clock=lambda: NOW)
        with self.assertWarns(RuntimeWarning):
            outcome = native.query("GB 99999-2099")
        self.assertEqual(outcome.results, ())
        self.assertFalse(outcome.complete)
        self.assertTrue(outcome.empty_authoritative)
        self.assertEqual(native.cache.puts, 0, "明文空结果不得写缓存")

    def test_fallback_currency_inherits_empty_authoritative(self):
        client = _SequenceClient(SourceUnreachable("tls blocked"), self.empty_page(total=0, pages=0))
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
        # 同页两个"现批准"：旧 primary 也必须让位给同族最新 primary。
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


if __name__ == "__main__":
    unittest.main()
