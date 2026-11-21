"""W3 契约测试：P1-4 空结果/解析失败、P2-17 同族与 ;jsessionid、P2-18 状态与 fallback 严重级。

全部离线：适配器只注入内存 fake 客户端；socket.connect 被禁用，任何真实联网都会让测试失败。
这里的断言是"这一类问题不许再发生"的不变量，而不是当前输出的快照。
"""

from __future__ import annotations

import json
import socket
import unittest
import urllib.parse
import warnings
from pathlib import Path
from unittest import mock

from guiju.currency import CachedCurrency, _cache
from guiju.currency.cache import CACHE_NAMESPACE, CacheEntry
from guiju.currency.ccsn import CcsnCurrency, parse_ccsn, status_ccsn
from guiju.currency.codes import family_key, filter_by_code, same_family
from guiju.currency.contract import Completeness, parse_or_fail, require_keys
from guiju.currency.csres import CsresCurrency, parse_csres, parse_csres_page
from guiju.currency.dbba import DbbaCurrency, parse_dbba
from guiju.currency.fallback import FallbackCurrency
from guiju.currency.httpx import HttpClient, _check_url
from guiju.currency.jtst import parse_jtst
from guiju.currency.model import Record
from guiju.currency.native import NativeCurrency, Outcome
from guiju.currency.registry import Registry, Route
from guiju.currency.stdsamr import StdSamrCurrency, parse_stdsamr
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable

FIXTURES = Path(__file__).parent / "fixtures" / "currency"
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
    """按调用顺序返回内存响应；只有一个响应时重复使用最后一个。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

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

    def close(self):
        self.closed = True


# ---------------------------------------------------------------- JSON 适配器


def ccsn_body(rows=(), total=0, **extra):
    payload = {"Total": total, "List": list(rows)}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def samr_body(rows=(), total=0, **extra):
    payload = {"total": total, "rows": list(rows)}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def dbba_body(records=(), total=0, **extra):
    payload = {"total": total, "records": list(records)}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


WAF_BODY = b'{"code":500,"message":"system busy"}'
ADAPTERS = (
    ("ccsn", parse_ccsn, CcsnCurrency, ccsn_body),
    ("std_samr", parse_stdsamr, StdSamrCurrency, samr_body),
    ("dbba", parse_dbba, DbbaCurrency, dbba_body),
)


class JsonAdapterFailClosedTest(OfflineTestCase):
    """P1-4：缺必需字段 / WAF 错误体不得伪装成"权威无记录"。"""

    def test_error_body_without_required_keys_is_needs_human(self):
        for source, parser, adapter, _body in ADAPTERS:
            with self.subTest(source=source), self.assertRaises(NeedsHuman) as ctx:
                parser(WAF_BODY)
            self.assertEqual(ctx.exception.exit, 50)
            self.assertIn(source, str(ctx.exception))
            self.assertIn("缺少必需字段", str(ctx.exception))
            client = FakeClient(WAF_BODY)
            with self.subTest(source=source, where="lookup"), self.assertRaises(NeedsHuman):
                adapter(client).lookup("GB 50011-2010")
            self.assertEqual(len(client.calls), 1)  # 结构错误不触发基础号重查

    def test_explicit_empty_answer_is_successful_empty_and_complete(self):
        for source, _parser, adapter, body in ADAPTERS:
            client = FakeClient(body())
            with self.subTest(source=source):
                cur = adapter(client)
                self.assertEqual(cur.lookup("GB 50011-2010"), [])
                self.assertTrue(cur.complete, "源明确回答 0 条 = 权威空结果")
                self.assertEqual(cur.pagination["total"], 0)
                self.assertIs(cur.pagination["has_more"], False)
                self.assertEqual(cur.incomplete_reason, "")

    def test_rows_without_any_parsable_record_is_needs_human(self):
        # ccsn 的行必须有 StandardCode；有行但 0 条可解析 = 结构变了，不是"没有"。
        with self.assertRaises(NeedsHuman) as ctx:
            parse_ccsn(ccsn_body([{}, {"StandardCode": "   "}], total=2))
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("0 条可解析", str(ctx.exception))

    def test_ccsn_total_positive_but_zero_rows_is_needs_human(self):
        with self.assertRaises(NeedsHuman) as ctx:
            parse_ccsn(ccsn_body([], total=3))
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("Total=3", str(ctx.exception))

    def test_total_positive_but_zero_rows_is_needs_human(self):
        for source, _parser, adapter, body in ADAPTERS[1:]:
            client = FakeClient(body(total=3))
            with self.subTest(source=source), self.assertRaises(NeedsHuman) as ctx:
                adapter(client).lookup("GB 50011-2010")
            self.assertEqual(ctx.exception.exit, 50)
            self.assertIn("total=3", str(ctx.exception))

    def test_require_keys_contract(self):
        require_keys({"Total": 0, "List": []}, ("Total", "List"), "ccsn")
        # Go 零值口径：键在、值为 null 不是"缺字段"（缺字段才是结构不符）。
        require_keys({"Total": None, "List": None}, ("Total", "List"), "ccsn")
        for payload in ({}, {"Total": 0}, {"List": []}):
            with self.subTest(payload=payload), self.assertRaises(NeedsHuman) as ctx:
                require_keys(payload, ("Total", "List"), "ccsn")
            self.assertEqual(ctx.exception.exit, 50)
            self.assertIn("ccsn", str(ctx.exception))

    def test_parse_or_fail_contract(self):
        record = Record("GB 50011-2010", "current", source="ccsn")
        self.assertEqual(parse_or_fail([], [], "ccsn", where="List"), [])
        self.assertEqual(parse_or_fail([{"x": 1}], [record], "ccsn"), [record])
        with self.assertRaises(NeedsHuman) as ctx:
            parse_or_fail([{"x": 1}], [], "ccsn", where="List")
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("List", str(ctx.exception))


# ---------------------------------------------------------------- HTML 适配器


SUMMARY = "共有{total}条符合状态条件的标准，共{pages}页"


def csres_row(code, status="现行", date="2020-01-01"):
    title = f"编号：{code} &#xA;发布日期：{date}"
    return (
        f'<tr title="{title}"><td>{code}</td><td>名称</td><td>部门</td>'
        f"<td>{date}</td><td>{status}</td></tr>"
    )


def csres_page(rows, *, total=None, pages=None, page=1, page_size=25, summary=True):
    html = ["<html><body><table>", *rows, "</table>"]
    if summary and total is not None:
        html.append('<span class="hei14">' + SUMMARY.format(total=total, pages=pages) + "</span>")
    html.append(f'<input type="hidden" name="pageSize" value="{page_size}">')
    html.append(f'<input type="hidden" name="pageNum" value="{page}">')
    html.append("</body></html>")
    return "".join(html).encode("gbk")


class HtmlAdapterFailClosedTest(OfflineTestCase):
    """改版 HTML：有数据痕迹但 0 条可解析 → 50；真空页仍是成功的空结果。"""

    def test_csres_data_traces_without_parsable_rows_is_needs_human(self):
        cases = (
            "<table><tr><td>GB 1-2000</td><td>名称</td><td>部门</td><td>2020-01-01</td></tr></table>",
            '<table><tr title="编号：GB 1-2000"><td>GB 1-2000</td><td>名称</td><td>部门</td></tr></table>',
        )
        for html in cases:
            with self.subTest(html=html), self.assertRaises(NeedsHuman) as ctx:
                parse_csres(html.encode("gbk"))
            self.assertEqual(ctx.exception.exit, 50)
            self.assertIn("csres", str(ctx.exception))
        client = FakeClient(cases[0].encode("gbk"))
        with self.assertRaises(NeedsHuman):
            CsresCurrency(client).lookup("GB1")

    def test_csres_empty_page_without_traces_is_successful_empty(self):
        for html in (b"", b"<html></html>", "<table><tr><td>无结果</td></tr></table>".encode("gbk")):
            with self.subTest(html=html):
                self.assertEqual(parse_csres(html), [])

    def test_jtst_rows_without_parsable_records_is_needs_human(self):
        # en-code 标记在（数据痕迹）但编号为空 → 0 条可解析 → 50；纯布局行不算痕迹。
        html = '<table><tr><td><span class="en-code">&nbsp;</span></td></tr></table>'
        self.assertEqual(parse_jtst(b'<table><tr><td><span class="other">no code</span></td></tr></table>'), [])
        with self.assertRaises(NeedsHuman) as ctx:
            parse_jtst(html.encode())
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("jtst", str(ctx.exception))
        self.assertEqual(parse_jtst(b"<html><body><table></table></body></html>"), [])


class CsresPaginationTest(OfflineTestCase):
    """P1-4/P2-17：csres 真的按 pageNum 翻页；无 total 时 has_more=None。"""

    def test_summary_gives_total_pages_and_page_size(self):
        page = parse_csres_page((FIXTURES / "csres_gb50011.gbk").read_bytes())
        self.assertEqual(len(page.records), 2)
        self.assertEqual((page.total, page.pages, page.page_size), (2, 1, 25))

    def test_second_page_match_is_visible(self):
        first = csres_page([csres_row(f"JGJ {i}-2010") for i in range(1, 26)], total=26, pages=2)
        second = csres_page([csres_row("GB 50011-2010")], total=26, pages=2, page=2)
        client = FakeClient(first, second)
        cur = CsresCurrency(client)
        self.assertEqual([r.code for r in cur.lookup("GB50011")], ["GB 50011-2010"])
        self.assertEqual(len(client.calls), 2)
        queries = [urllib.parse.parse_qs(urllib.parse.urlsplit(url).query) for url in client.calls]
        self.assertEqual([q["pageNum"][0] for q in queries], ["1", "2"])
        self.assertEqual([q["keyword"][0] for q in queries], ["GB50011", "GB50011"])
        self.assertTrue(cur.complete)
        self.assertEqual(cur.pagination["fetched_pages"], 2)
        self.assertEqual(cur.pagination["total"], 26)
        self.assertIs(cur.pagination["has_more"], False)

    def test_without_total_is_unknown_not_complete(self):
        page = csres_page([csres_row("GB 50011-2010")], summary=False)
        client = FakeClient(page)
        cur = CsresCurrency(client)
        self.assertEqual([r.code for r in cur.lookup("GB50011")], ["GB 50011-2010"])
        self.assertEqual(len(client.calls), 1)
        self.assertIsNone(cur.pagination["total"])
        self.assertIsNone(cur.pagination["has_more"])
        self.assertFalse(cur.complete)
        self.assertIn("无法证明", cur.incomplete_reason)

    def test_truncated_at_max_pages_is_flagged(self):
        pages = 12
        client = FakeClient(*[
            csres_page([csres_row(f"GB 50011-{2010 + i}") for i in range(25)],
                       total=300, pages=pages, page=page)
            for page in range(1, CsresCurrency.MAX_PAGES + 1)
        ])
        cur = CsresCurrency(client)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rows = cur.lookup("GB50011")
        self.assertEqual(len(client.calls), cur.MAX_PAGES)
        self.assertEqual(len(rows), len(client.calls) * 25)
        self.assertTrue(cur.pagination["truncated"])
        self.assertIs(cur.pagination["has_more"], True)
        self.assertFalse(cur.complete)
        self.assertTrue(any("截断" in str(w.message) for w in caught))

    def test_total_change_between_pages_is_needs_human(self):
        # 结果集在翻页期间变动：混合两页会得到"看似完整"的假结论，必须交人。
        first = csres_page([csres_row(f"GB 50011-{2000 + i}") for i in range(25)], total=26, pages=2)
        second = csres_page([csres_row("GB 50011-2020")], total=27, pages=2, page=2)
        with self.assertRaises(NeedsHuman) as ctx:
            CsresCurrency(FakeClient(first, second)).lookup("GB50011")
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("总条数", str(ctx.exception))

    def test_declared_more_pages_but_empty_page_is_needs_human(self):
        first = csres_page([csres_row(f"GB 50011-{2010 + i}") for i in range(25)], total=60, pages=3)
        empty = csres_page([], total=60, pages=3, page=2)
        client = FakeClient(first, empty)
        with self.assertRaises(NeedsHuman) as ctx:
            CsresCurrency(client).lookup("GB50011")
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("第 2 页", str(ctx.exception))

    def test_last_page_short_of_total_is_incomplete_not_cached(self):
        first = csres_page([csres_row(f"GB 50011-{2010 + i}") for i in range(25)], total=30, pages=2, page=1)
        last = csres_page([csres_row(f"GB 50011-{2040 + i}") for i in range(4)], total=30, pages=2, page=2)
        cur = CsresCurrency(FakeClient(first, last))
        cur.lookup("GB50011")
        self.assertFalse(cur.complete)
        self.assertIs(cur.pagination["has_more"], True)
        self.assertIn("30", cur.incomplete_reason)

    def test_last_page_empty_before_total_is_incomplete(self):
        first = csres_page([csres_row(f"GB 50011-{2010 + i}") for i in range(25)], total=30, pages=2, page=1)
        last = csres_page([], total=30, pages=2, page=2)
        cur = CsresCurrency(FakeClient(first, last))
        rows = cur.lookup("GB50011")
        self.assertEqual(len(rows), 25)
        self.assertFalse(cur.complete)
        self.assertIn("30", cur.incomplete_reason)

    def test_repeated_page_is_needs_human(self):
        first = csres_page([csres_row(f"GB 50011-{2010 + i}") for i in range(25)], total=30, pages=2, page=1)
        repeated = csres_page([csres_row(f"GB 50011-{2010 + i}") for i in range(25)], total=30, pages=2, page=1)
        with self.assertRaises(NeedsHuman) as ctx:
            CsresCurrency(FakeClient(first, repeated)).lookup("GB50011")
        self.assertIn("第 2 页", str(ctx.exception))

    def test_total_unknown_last_page_empty_is_incomplete(self):
        first = csres_page([csres_row(f"GB 50011-{2010 + i}") for i in range(1)], total=None, pages=2, page=1)
        last = csres_page([], total=None, pages=2, page=2)
        tail = "<span>共 2 页</span></body>".encode("gbk")
        first = first.replace(b"</body>", tail)
        last = last.replace(b"</body>", tail)
        self.assertIn("共 2 页".encode("gbk"), first)
        cur = CsresCurrency(FakeClient(first, last))
        cur.lookup("GB50011")
        self.assertFalse(cur.complete)
        self.assertIn("总条数未知", cur.incomplete_reason)

    def test_more_rows_than_total_is_incomplete(self):
        first = csres_page([csres_row(f"GB 50011-{2010 + i}") for i in range(3)], total=2, pages=1)
        cur = CsresCurrency(FakeClient(first))
        cur.lookup("GB50011")
        self.assertFalse(cur.complete)
        self.assertIn("多余", cur.incomplete_reason)

    def test_empty_code_row_is_needs_human(self):
        html = csres_page([
            '<tr title="编号： GB 1-2020"><td></td><td>名称</td><td>部门</td><td>2020-01-01</td><td>现行</td></tr>'
        ], total=1, pages=1)
        with self.assertRaises(NeedsHuman):
            parse_csres(html)

    def test_lookup_returns_only_requested_family_after_paging(self):
        page = csres_page([csres_row("GB 50011-2010"), csres_row("GB 50012-2010")], total=2, pages=1)
        self.assertEqual([r.code for r in CsresCurrency(FakeClient(page)).lookup("GB50011")],
                         ["GB 50011-2010"])


class CsresTransportTest(OfflineTestCase):
    """csres 明文 http：优先 https；降级必须显式、留痕、且不可作为权威答案。"""

    def test_https_is_the_default(self):
        self.assertTrue(CsresCurrency.url.startswith("https://"))
        client = FakeClient(csres_page([csres_row("GB 50011-2010")], total=1, pages=1))
        cur = CsresCurrency(client)
        cur.lookup("GB50011")
        self.assertEqual(CsresCurrency.url, "https://www.csres.com/s.jsp")
        self.assertTrue(client.calls[0].startswith("https://www.csres.com/s.jsp?"))
        self.assertEqual(cur.transport, "https")
        self.assertFalse(cur.plaintext)
        self.assertEqual(cur.plaintext_risk, "")
        self.assertTrue(cur.complete)

    def test_both_transports_are_allowlisted_metadata_endpoints(self):
        # 传输策略不等于放宽白名单：https 与明文回退都必须是已批准端点。
        self.assertEqual(_check_url(CsresCurrency.url), "www.csres.com")
        self.assertEqual(_check_url("http://www.csres.com/s.jsp?keyword=GB1&pageNum=1"), "www.csres.com")

    def test_plaintext_downgrade_requires_explicit_opt_in(self):
        client = FakeClient(SourceUnreachable("tls 不可用"))
        with self.assertRaises(SourceUnreachable):
            CsresCurrency(client).lookup("GB50011")
        self.assertEqual(len(client.calls), 1, "默认不得自动降级到明文 HTTP")

    def test_explicit_plaintext_fallback_is_recorded_and_not_authoritative(self):
        page = csres_page([csres_row("GB 50011-2010")], total=1, pages=1)
        client = FakeClient(SourceUnreachable("tls 不可用"), page)
        cur = CsresCurrency(client, plaintext_fallback=True)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rows = cur.lookup("GB50011")
        self.assertEqual([r.code for r in rows], ["GB 50011-2010"])
        self.assertTrue(client.calls[1].startswith("http://www.csres.com/s.jsp?"))
        self.assertEqual(cur.transport, "http")
        self.assertTrue(cur.plaintext)
        self.assertIn("正文", cur.plaintext_risk)
        self.assertFalse(cur.complete, "明文回退的结果不得作为权威答案写缓存")
        self.assertTrue(any("明文" in str(w.message) for w in caught))


class StatusMappingTest(OfflineTestCase):
    """P2-18：状态照录源站证据，不把"在编"说成"即将实施"，不把"废止"说成"被替代"。"""

    def test_ccsn_status_matrix(self):
        for state, abolish, replaces, expected in (
            ("现行", "", "", "current"),
            ("废止", "", "", "withdrawn"),
            ("即将实施", "", "", "upcoming"),
            ("在编", "", "", "unknown"),          # 编制中 ≠ 即将实施
            ("在编", "2016-08-01", "", "unknown"),
            ("局修完成", "2016-08-01", "", "unknown"),
            ("未知词", "", "", "unknown"),
            ("未知词", "2016-08-01", "", "withdrawn"),          # 废止日期，无替代编号
            ("未知词", "2016-08-01", "GB 1-2020", "superseded"),  # 有替代编号
            ("", " 2016-08-01 ", "", "withdrawn"),
            # 源站明说「废止」就照录：替代编号不把"废止"改写成"被替代"（P2-18）。
            ("废止", "2016-08-01", "GB 1-2020", "withdrawn"),
        ):
            with self.subTest(state=state, abolish=abolish, replaces=replaces):
                self.assertEqual(status_ccsn(state, abolish, replaces), expected)

    def test_parse_ccsn_uses_replace_code_as_evidence(self):
        row = {"StandardCode": "GB1-2000", "StandardState": "未知词", "AbolishDate": "2020-01-01"}
        self.assertEqual(parse_ccsn(ccsn_body([row], total=1))[0].status, "withdrawn")
        row["ReplaceStandardCode"] = "GB 1-2020"
        self.assertEqual(parse_ccsn(ccsn_body([row], total=1))[0].status, "superseded")


class CcsnPaginationEvidenceTest(OfflineTestCase):
    """ccsn 的 Total 是全局计数：翻页期间变化说明结果集在动，不能当完整结果。"""

    @staticmethod
    def page(total, rows, has_next):
        return json.dumps({
            "Total": total,
            "List": [{"StandardCode": f"GB 50011-{2000 + i}", "StandardState": "现行"} for i in range(rows)],
            "HasNextPage": has_next,
        }, ensure_ascii=False).encode("utf-8")

    def test_total_change_between_pages_is_needs_human(self):
        client = FakeClient(self.page(40, 20, True), self.page(41, 20, True))
        with self.assertRaises(NeedsHuman) as ctx:
            CcsnCurrency(client).lookup("GB 50011-2010")
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("Total", str(ctx.exception))
        self.assertEqual(len(client.calls), 2)

    def test_stable_total_pages_to_the_end_and_is_complete(self):
        client = FakeClient(self.page(25, 20, True), self.page(25, 5, False))
        cur = CcsnCurrency(client)
        self.assertEqual(len(cur.lookup("GB 50011-2010")), 25)
        self.assertTrue(cur.complete)
        self.assertEqual(cur.pagination["fetched_pages"], 2)


class FamilyBoundaryTest(OfflineTestCase):
    """P2-17：同族判定用完整基号；JTG/T 3360-01 与 -02 不是同族。"""

    def test_complete_base_code_matrix(self):
        for left, right, expected in (
            ("JTG/T 3360-01", "JTG/T 3360-01-2020", True),
            ("JTG/T 3360-01-2020", "JTG/T 3360-01", True),
            ("JTG/T 3360-01", "JTG/T 3360-02", False),
            ("JTG/T 3360-01-2020", "JTG/T 3360-02-2019", False),
            ("JTG/T 3360", "JTG/T 3360-01-2020", True),    # 只给基号时接受子册
            ("JTG/T 3360-01-2020", "JTG/T 3360", False),   # 反向不接受（查询是具体子册）
            ("GB 50011", "GB/T 50011-2010（2024年版）", True),
            ("GB 50011-1-2010", "GB 50011-2-2010", False),
            ("JGJ 147-2004", "JGJ147-2016", True),
            ("JGJ3", "JGJ 203-2010", False),
            ("GB1", "JGJ1", False),
            ("", "GB1", False),
        ):
            with self.subTest(left=left, right=right):
                self.assertEqual(same_family(left, right), expected)

    def test_family_key_strips_edition_year_and_decoration(self):
        self.assertEqual(family_key("GB/T 50011-2010（2024年版）"), "GB50011")
        self.assertEqual(family_key("JTG/T 3360-01-2020"), "JTG3360-01")
        self.assertEqual(family_key("DG/TJ08-2007-2026"), "DG/TJ08")
        self.assertEqual(family_key(""), "")

    def test_sibling_part_is_filtered_out(self):
        rows = [Record("JTG/T 3360-02-2019", "current"), Record("JTG/T 3360-01-2020", "current")]
        self.assertEqual([r.code for r in filter_by_code(rows, "JTG/T 3360-01")],
                         ["JTG/T 3360-01-2020"])


class JsessionIdTest(OfflineTestCase):
    """P2-17：/s.jsp;jsessionid=... 是同一个已批准端点，不能判成 40。"""

    def test_check_url_normalizes_session_id_and_duplicate_slashes(self):
        for url in (
            "https://www.csres.com/s.jsp;jsessionid=ABC",
            "https://www.csres.com//s.jsp",
            "https://www.csres.com/s.jsp;jsessionid=abc;foo=bar",
            "http://www.csres.com/s.jsp;jsessionid=ABC",
        ):
            with self.subTest(url=url):
                self.assertEqual(_check_url(url), "www.csres.com")
        for url in (
            "https://www.csres.com/other.jsp;jsessionid=ABC",
            "https://www.csres.com/s.jsp/extra",
            "https://evil.example/s.jsp;jsessionid=ABC",
        ):
            with self.subTest(url=url), self.assertRaises(ComplianceDenied):
                _check_url(url)

    def test_session_id_url_passes_client_and_final_url_check(self):
        client = HttpClient()
        client.transport = mock.Mock()
        final = "https://www.csres.com/s.jsp;jsessionid=ABC"
        client.transport.open.return_value = FakeResponseFor(b"<html></html>", final)
        with mock.patch("guiju.currency.httpx._throttle"):
            self.assertEqual(client.get(final), b"<html></html>")


def FakeResponseFor(body, url):
    class _Response:
        def __init__(self):
            import io

            self._stream = io.BytesIO(body)
            self.status = 200
            self.url = url
            self.headers = {"Content-Type": "text/html; charset=gbk"}
            self.length = None
            self.closed = False

        def read(self, size=-1):
            return self._stream.read(size)

        def geturl(self):
            return self.url

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    return _Response()


class FallbackSeverityTest(OfflineTestCase):
    """P2-18：最终错误类型取所有分支里最需要人处理的级别（50 > 20）。"""

    class Failing:
        def __init__(self, name, error):
            self.name, self.error = name, error

        def lookup(self, code):
            raise self.error

    class Empty:
        name = "empty"

        def lookup(self, code):
            return []

    def test_error_type_uses_most_severe_branch(self):
        cases = (
            (SourceUnreachable("p 20"), [NeedsHuman("b 50")], NeedsHuman),
            (NeedsHuman("p 50"), [SourceUnreachable("b 20")], NeedsHuman),
            (SourceUnreachable("p 20"), [SourceUnreachable("b1 20")], SourceUnreachable),
            (SourceUnreachable("p 20"), [self.Empty()], SourceUnreachable),
            (SourceUnreachable("p 20"), [self.Empty(), NeedsHuman("b2 50")], NeedsHuman),
        )
        for primary, backups, expected in cases:
            chain = FallbackCurrency(
                "p",
                self.Failing("p", primary),
                *(self.Failing(f"b{index}", item) if isinstance(item, Exception) else item
                  for index, item in enumerate(backups, start=1)),
            )
            with self.subTest(primary=type(primary).__name__, backups=len(backups)), \
                    self.assertRaises(expected) as ctx:
                chain.lookup(CODE)
            self.assertEqual(ctx.exception.exit, expected.exit)
            self.assertIn("主源失败", str(ctx.exception))

    def test_all_errors_are_listed_in_message(self):
        chain = FallbackCurrency(
            "p",
            self.Failing("p", SourceUnreachable("primary down")),
            self.Failing("b1", NeedsHuman("backup changed")),
        )
        with self.assertRaises(NeedsHuman) as ctx:
            chain.lookup(CODE)
        self.assertIn("p: primary down", str(ctx.exception))
        self.assertIn("b1: backup changed", str(ctx.exception))

    def test_primary_success_empty_still_stops_the_chain(self):
        # ADR 0002 裁决不变：主源成功空是终局，不因"可能不完整"转备源。
        backup = self.Empty()
        backup.lookup = mock.Mock(return_value=[Record(CODE, "current", source="empty")])
        primary = mock.Mock()
        primary.name = "p"
        primary.lookup.return_value = []
        self.assertEqual(FallbackCurrency("p", primary, backup).lookup(CODE), [])
        backup.lookup.assert_not_called()


# ---------------------------------------------------------------- 缓存权威性


class StubAdapter:
    name = "p"

    def __init__(self, rows=(), *, error=None, complete=True, reason=""):
        self.rows = list(rows)
        self.error = error
        self.complete = complete
        self.incomplete_reason = reason
        self.calls = 0

    def lookup(self, code):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


class BareAdapter:
    """没有 complete 字段的老适配器：沿用"成功即可缓存"的旧契约。"""

    name = "p"

    def __init__(self):
        self.calls = 0

    def lookup(self, code):
        self.calls += 1
        return []


class RecordingCache:
    def __init__(self):
        self.entries = {}
        self.calls = []
        self.puts = 0

    def get(self, code, *, namespace=CACHE_NAMESPACE):
        self.calls.append("get")
        return self.entries.get((namespace, code))

    def get_any(self, code, *, namespace=CACHE_NAMESPACE):
        self.calls.append("get_any")
        return self.entries.get((namespace, code))

    def put(self, code, records, *, namespace=CACHE_NAMESPACE):
        self.calls.append("put")
        self.puts += 1
        self.entries[(namespace, code)] = CacheEntry(NOW, tuple(records))


def native_with(adapter, cache):
    registry = Registry()
    registry.register(Route("p", ("GB",), lambda: adapter))
    return NativeCurrency(registry, cache, clock=lambda: NOW)


class NativeCacheAuthorityTest(OfflineTestCase):
    """P1-4：只有权威结果（完整且可信）才写缓存；解析失败绝不 put。"""

    def test_parse_failure_never_puts(self):
        cache = RecordingCache()
        native = native_with(StubAdapter(error=NeedsHuman("ccsn 元数据 JSON 结构异常")), cache)
        with self.assertRaises(NeedsHuman):
            native.query(CODE)
        self.assertEqual(cache.puts, 0)
        self.assertNotIn("put", cache.calls)
        self.assertIsNone(native.last_outcome)

    def test_incomplete_empty_is_not_negative_cached(self):
        adapter = StubAdapter(rows=[], complete=False, reason="页面未提供总页数，无法证明结果已取全")
        cache = RecordingCache()
        native = native_with(adapter, cache)
        first = native.query(CODE)
        self.assertEqual(first.results, ())
        self.assertFalse(first.complete)
        self.assertIn("不完整", first.critical_notes()[0])
        self.assertEqual(cache.puts, 0)
        native.query(CODE)
        self.assertEqual(adapter.calls, 2, "不完整的空结果不得成为 24h 假阴性")
        self.assertEqual(cache.puts, 0)

    def test_complete_empty_is_negative_cached(self):
        adapter = StubAdapter(rows=[], complete=True)
        cache = RecordingCache()
        native = native_with(adapter, cache)
        first = native.query(CODE)
        self.assertTrue(first.complete)
        self.assertEqual(cache.puts, 1)
        second = native.query(CODE)
        self.assertTrue(second.from_cache)
        self.assertTrue(second.complete)
        self.assertEqual(adapter.calls, 1)

    def test_incomplete_nonempty_result_is_not_cached(self):
        record = Record(CODE, "current", "标准", source="p")
        adapter = StubAdapter(rows=[record], complete=False, reason="只取到第 1 页")
        cache = RecordingCache()
        native = native_with(adapter, cache)
        first = native.query(CODE)
        self.assertEqual(first.results, (record,))
        self.assertFalse(first.complete)
        self.assertEqual(cache.puts, 0)
        native.query(CODE)
        self.assertEqual(adapter.calls, 2)

    def test_adapter_without_completeness_attribute_keeps_old_contract(self):
        cache = RecordingCache()
        native = native_with(BareAdapter(), cache)
        self.assertEqual(native.query(CODE).results, ())
        self.assertEqual(cache.puts, 1)

    def test_exposed_flags_reset_between_queries(self):
        adapter = StubAdapter(rows=[], complete=False, reason="不完整")
        cache = RecordingCache()
        native = native_with(adapter, cache)
        native.query(CODE)
        self.assertFalse(native.complete)
        adapter.complete, adapter.incomplete_reason = True, ""
        native.query(CODE)
        self.assertTrue(native.complete)

    def test_error_body_cannot_become_a_cached_negative(self):
        # 端到端：ccsn 的 WAF 错误体两次都必须是 50，第二次不能变成"没收录"的缓存结论。
        client = FakeClient(WAF_BODY)
        registry = Registry()
        registry.register(Route("ccsn", ("GB",), lambda: CcsnCurrency(client)))
        cache = RecordingCache()
        native = NativeCurrency(registry, cache, clock=lambda: NOW)
        for attempt in (1, 2):
            with self.subTest(attempt=attempt), self.assertRaises(NeedsHuman) as ctx:
                native.query(CODE)
            self.assertEqual(ctx.exception.exit, 50)
        self.assertEqual(cache.puts, 0)
        self.assertEqual(len(client.calls), 2)

class CachedCurrencyCompletenessTest(OfflineTestCase):
    """完整性字段必须能被上层读到：CLI 的 JSON 投影只差一层 plumbing（Lead 接）。"""

    def setUp(self):
        super().setUp()
        _cache.clear()
        self.addCleanup(_cache.clear)

    def test_cached_currency_forwards_native_completeness(self):
        adapter = StubAdapter(rows=[], complete=False, reason="页面未提供总页数")
        registry = Registry()
        registry.register(Route("p", ("GB",), lambda: adapter))
        wrapped = CachedCurrency(NativeCurrency(registry, None, clock=lambda: NOW))
        wrapped.lookup(CODE)
        self.assertIs(wrapped.complete, False)

    def test_adapter_without_contract_reports_unknown_not_complete(self):
        wrapped = CachedCurrency(BareAdapter())
        wrapped.lookup(CODE)
        self.assertIsNone(wrapped.complete, "老适配器没有完整性契约时不得谎报已完整")

    def test_completeness_resets_between_queries(self):
        adapter = StubAdapter(rows=[], complete=False, reason="不完整")
        registry = Registry()
        registry.register(Route("p", ("GB",), lambda: adapter))
        # 带缓存后端：不完整的空结果不落盘，第二次会重新问上游，标记随之翻新。
        wrapped = CachedCurrency(NativeCurrency(registry, RecordingCache(), clock=lambda: NOW))
        wrapped.lookup(CODE)
        self.assertIs(wrapped.complete, False)
        adapter.complete, adapter.incomplete_reason = True, ""
        wrapped.lookup(CODE)
        self.assertIs(wrapped.complete, True)


class CompletenessProjectionTest(OfflineTestCase):
    """完整性值对象本身：complete 只有"取全 + 未截断 + 未走明文"才为真。"""

    def test_annotation_marks_incomplete_results(self):
        outcome = Outcome((), "p", complete=False, incomplete_reason="只取到第 1 页")
        self.assertIn("不完整", outcome.annotation())
        self.assertEqual(Outcome((), "p").annotation(), "")

    def test_completeness_projection(self):
        self.assertTrue(Completeness(fetched_pages=1, total=1, has_more=False).complete)
        self.assertFalse(Completeness(fetched_pages=1, has_more=None).complete)
        self.assertFalse(Completeness(fetched_pages=1, total=9, has_more=True).complete)
        self.assertFalse(Completeness(fetched_pages=1, has_more=False, truncated=True).complete)
        self.assertFalse(Completeness(fetched_pages=1, has_more=False, insecure_transport=True).complete)
        self.assertEqual(
            Completeness(fetched_pages=1, total=1, has_more=False).as_pagination(),
            {"fetched_pages": 1, "total": 1, "has_more": False, "truncated": False, "reason": ""},
        )


if __name__ == "__main__":
    unittest.main()
