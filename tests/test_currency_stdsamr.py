"""std_samr 原始响应的独立 golden 与纯离线查询契约。"""

import json
import socket
import unittest
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from guiju.currency.httpx import HttpClient
from guiju.currency.stdsamr import SEARCH_URL, StdSamrCurrency, parse_stdsamr, status_stdsamr, strip_tags
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable

FIXTURE = Path(__file__).parent / "fixtures" / "currency" / "stdsamr_gbt19001.json"
# 人工照录旧 stdsamr_test.go 与原始响应；包括不属于查询号族的行，不能拿第一条当答案。
GOLDEN = [
    {
        "code": "GB/T 19002-2018",
        "name": "质量管理体系 GB/T 19001—2016应用指南",
        "status": "current",
        "publish_date": "2018-12-28",
        "implement_date": "2019-07-01",
        "source": "std_samr",
        "replaces": "",
        "raw": "现行",
        "fallback": "",
    },
    {
        "code": "GB/T 19000.2-1998",
        "name": "质量管理和质量保证标准  第2部分:GB/T 19001、GB/T 19002和GB/T 19003实施通用指南",
        "status": "withdrawn",
        "publish_date": "1998-11-13",
        "implement_date": "1999-05-01",
        "source": "std_samr",
        "replaces": "",
        "raw": "废止",
        "fallback": "",
    },
    {
        "code": "GB/T 27865-2011",
        "name": "危险货物包装  包装、中型散装容器、大包装　GB/T 19001的应用指南",
        "status": "current",
        "publish_date": "2011-12-30",
        "implement_date": "2012-08-01",
        "source": "std_samr",
        "replaces": "",
        "raw": "现行",
        "fallback": "",
    },
    {
        "code": "GB/Z 19036-2009",
        "name": "质量管理体系  GB/T 19001在中小型组织中的应用指南",
        "status": "withdrawn",
        "publish_date": "2009-09-30",
        "implement_date": "",
        "source": "std_samr",
        "replaces": "",
        "raw": "废止",
        "fallback": "",
    },
]


def response(*rows):
    return json.dumps({"total": len(rows), "rows": list(rows)}, ensure_ascii=False).encode("utf-8")


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        guard = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("offline test attempted a socket connection"))
        guard.start()
        self.addCleanup(guard.stop)


class StdSamrGoldenTest(OfflineTestCase):
    def test_golden_each_field_in_original_order(self):
        got = parse_stdsamr(FIXTURE.read_bytes())
        self.assertEqual(len(got), len(GOLDEN))
        for index, (record, wanted) in enumerate(zip(got, GOLDEN, strict=True)):
            self.assertEqual(set(asdict(record)), set(wanted))
            for field, value in wanted.items():
                with self.subTest(row=index, field=field):
                    self.assertEqual(getattr(record, field), value)

    def test_golden_lookup_uses_search_text_and_std_samr_source(self):
        client = mock.Mock(spec=HttpClient)
        client.get.return_value = FIXTURE.read_bytes()
        source = StdSamrCurrency(client)
        self.assertEqual(source.name, "std_samr")
        self.assertEqual([asdict(record) for record in source.lookup("GB/T 19002-2018")], GOLDEN[:1])
        client.get.assert_called_once()
        url = urllib.parse.urlsplit(client.get.call_args.args[0])
        self.assertEqual(urllib.parse.urlunsplit((url.scheme, url.netloc, url.path, "", "")), SEARCH_URL)
        self.assertEqual(urllib.parse.parse_qs(url.query, keep_blank_values=True), {
            "searchText": ["GB/T 19002-2018"], "ics": [""], "state": [""], "ISSUE_DATE": [""],
        })
        self.assertEqual(client.get.call_args.kwargs, {"accept": "application/json"})
        client.post_form.assert_not_called()


class StdSamrParseTest(OfflineTestCase):
    def test_highlight_tag_removal_and_entity_unescape(self):
        for text, expected in (
            ("质量管理体系 <sacinfo>GB</sacinfo>/<sacinfo>T</sacinfo> <sacinfo>19001</sacinfo>—2016", "质量管理体系 GB/T 19001—2016"),
            ("  <b>粗</b>体  ", "粗体"), ("无标签", "无标签"),
            ("&nbsp;<sacinfo>GB</sacinfo>&#47;T 1&amp;2&nbsp;", "GB/T 1&2"),
            ("&lt;b&gt;不是实际标签&lt;/b&gt;", "<b>不是实际标签</b>"),
            (" 甲  乙　丙 ", "甲  乙　丙"),
        ):
            with self.subTest(text=text):
                self.assertEqual(strip_tags(text), expected)
        record = parse_stdsamr(response({
            "C_STD_CODE": " <sacinfo>GB</sacinfo>/<sacinfo>T</sacinfo> <b>1</b>—2000 ",
            "C_C_NAME": "&nbsp;<sacinfo>甲</sacinfo>&amp;乙&nbsp;", "STATE": "现行",
        }))[0]
        self.assertEqual((record.code, record.name), ("GB/T 1—2000", "甲&乙"))

    def test_status_unknown_never_guesses_current_or_upcoming(self):
        for state, expected in (
            ("现行", "current"), ("被代替", "superseded"), ("被替代", "superseded"),
            ("废止", "withdrawn"), ("", "unknown"), ("未知词", "unknown"),
            ("即将实施", "unknown"), ("局修完成", "unknown"), ("现 行", "unknown"),
        ):
            with self.subTest(state=state):
                self.assertEqual(status_stdsamr(" " + state + " "), expected)
                record = parse_stdsamr(response({"C_STD_CODE": "GB1", "STATE": " " + state + " ", "ACT_DATE": "2099-01-01"}))[0]
                self.assertEqual(record.status, expected)
                self.assertEqual(record.raw, state)

    def test_dates_only_trim_and_replaces_is_not_inferred(self):
        record = parse_stdsamr(response({
            "C_STD_CODE": " GB/T 1-2000 ", "STATE": " \t被代替 \n",
            "ISSUE_DATE": " 2000-01-01T00:00:00 ", "ACT_DATE": " 未公布 ",
            "C_C_NAME": "替代 GB1-1980 的标准", "ReplaceStandardCode": "GB1-1980",
        }))[0]
        self.assertEqual(asdict(record), {
            "code": "GB/T 1-2000", "name": "替代 GB1-1980 的标准", "status": "superseded",
            "publish_date": "2000-01-01T00:00:00", "implement_date": "未公布",
            "source": "std_samr", "replaces": "", "raw": "被代替", "fallback": "",
        })

    def test_missing_and_null_fields_use_go_zero_values(self):
        empty = {
            "code": "", "name": "", "status": "unknown", "publish_date": "", "implement_date": "",
            "source": "std_samr", "replaces": "", "raw": "", "fallback": "",
        }
        for row in ({}, None, {"C_STD_CODE": None, "C_C_NAME": None, "STATE": None, "ISSUE_DATE": None, "ACT_DATE": None}):
            with self.subTest(row=row):
                self.assertEqual([asdict(record) for record in parse_stdsamr(response(row))], [empty])
        for raw in (b"{}", b'{"rows":null}', response()):
            with self.subTest(raw=raw):
                self.assertEqual(parse_stdsamr(raw), [])

    def test_rows_and_duplicates_keep_upstream_order(self):
        codes = ("GB1-2010", "GB1-2000", "GB1-2010")
        self.assertEqual([r.code for r in parse_stdsamr(response(*({"C_STD_CODE": code} for code in codes)))], list(codes))

    def test_total_accepts_missing_null_and_int64_values(self):
        rows = [{"C_STD_CODE": "GB1"}]
        payloads = [{"rows": rows}]
        payloads.extend({"total": value, "rows": rows} for value in (None, 0, 1, -1, -(2**63), 2**63 - 1))
        for payload in payloads:
            with self.subTest(payload=payload):
                raw = json.dumps(payload).encode("utf-8")
                self.assertEqual([record.code for record in parse_stdsamr(raw)], ["GB1"])

    def test_total_wrong_types_fractions_and_int64_overflow_fail_closed(self):
        for value in ("1", True, False, 1.0, 1.5, {}, [], 2**63, -(2**63) - 1):
            with self.subTest(total=value, type=type(value).__name__):
                raw = json.dumps({"total": value, "rows": []}).encode("utf-8")
                with self.assertRaises(NeedsHuman) as ctx:
                    parse_stdsamr(raw)
                self.assertEqual(ctx.exception.exit, 50)
                self.assertIn("total", str(ctx.exception))
                self.assertIn("std_samr", str(ctx.exception))

    def test_malformed_json_or_wrong_field_type_fail_closed(self):
        cases = [b"", b"\xff", b"{", b"[]", b'{"rows":{}}', b'{"rows":[42]}']
        for key in ("C_STD_CODE", "C_C_NAME", "STATE", "ISSUE_DATE", "ACT_DATE"):
            cases.append(response({"C_STD_CODE": "GB1", key: 7}))
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(NeedsHuman) as ctx:
                parse_stdsamr(raw)
            self.assertEqual(ctx.exception.exit, 50)
            self.assertIn("std_samr", str(ctx.exception))


class StdSamrLookupTest(OfflineTestCase):
    def test_blank_query_never_requests(self):
        client = mock.Mock(spec=HttpClient)
        for code in ("", " \t\n"):
            self.assertEqual(StdSamrCurrency(client).lookup(code), [])
        client.get.assert_not_called()
        client.post_form.assert_not_called()

    def test_old_year_retries_base_once_and_keeps_record_own_status(self):
        client = mock.Mock(spec=HttpClient)
        client.get.side_effect = [response(), response({"C_STD_CODE": "GB/T 19001-2016", "STATE": "现行"})]
        records = StdSamrCurrency(client).lookup("GB/T 19001-2008")
        self.assertEqual([(r.code, r.status) for r in records], [("GB/T 19001-2016", "current")])
        self.assertEqual(self.terms(client), ["GB/T 19001-2008", "GB/T 19001"])
        self.assertEqual(records[0].replaces, "")

    def test_fuzzy_first_response_must_be_filtered_before_fallback(self):
        client = mock.Mock(spec=HttpClient)
        client.get.side_effect = [FIXTURE.read_bytes(), response({"C_STD_CODE": "GB/T 19001-2016", "STATE": "未知词"})]
        records = StdSamrCurrency(client).lookup("GB/T 19001-2008")
        self.assertEqual([(r.code, r.status) for r in records], [("GB/T 19001-2016", "unknown")])
        self.assertEqual(self.terms(client), ["GB/T 19001-2008", "GB/T 19001"])

    def test_same_family_first_response_does_not_repeat(self):
        client = mock.Mock(spec=HttpClient)
        client.get.return_value = response({"C_STD_CODE": "GB 19001-2016", "STATE": "现行"}, {"C_STD_CODE": "GB/T 19002-2018"})
        self.assertEqual([r.code for r in StdSamrCurrency(client).lookup("GB/T 19001-2008")], ["GB 19001-2016"])
        client.get.assert_called_once()

    def test_no_year_requests_once_old_year_requests_at_most_twice(self):
        for code, terms in (("GB/T 19001", ["GB/T 19001"]), ("GB/T 19001-2008", ["GB/T 19001-2008", "GB/T 19001"])):
            client = mock.Mock(spec=HttpClient)
            client.get.return_value = response()
            with self.subTest(code=code):
                self.assertEqual(StdSamrCurrency(client).lookup(code), [])
                self.assertEqual(self.terms(client), terms)

    def test_user_text_cannot_inject_query_parameters(self):
        client = mock.Mock(spec=HttpClient)
        client.get.return_value = response()
        code = "GB/T 19001（年版）&state=现行#尾"
        self.assertEqual(StdSamrCurrency(client).lookup(code), [])
        client.get.assert_called_once()
        url = client.get.call_args.args[0]
        self.assertTrue(url.isascii())
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True), {
            "searchText": [code], "ics": [""], "state": [""], "ISSUE_DATE": [""],
        })

    def test_failures_propagate_without_base_retry(self):
        for error in (SourceUnreachable("offline"), ComplianceDenied("stop"), NeedsHuman("std_samr bad JSON")):
            client = mock.Mock(spec=HttpClient)
            client.get.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as ctx:
                StdSamrCurrency(client).lookup("GB/T 19001-2008")
            self.assertIs(ctx.exception, error)
            client.get.assert_called_once()

    def test_existing_five_field_payload_is_unchanged(self):
        record = parse_stdsamr(FIXTURE.read_bytes())[0]
        self.assertEqual(record.as_payload(), {
            "code": "GB/T 19002-2018", "name": "质量管理体系 GB/T 19001—2016应用指南", "status": "current",
            "implement_date": "2019-07-01", "source": "std_samr",
        })

    def test_decode_failure_does_not_fall_back_or_refetch(self):
        client = mock.Mock(spec=HttpClient)
        client.get.return_value = b"{"
        with self.assertRaises(NeedsHuman):
            StdSamrCurrency(client).lookup("GB/T 19001-2008")
        client.get.assert_called_once()

    @staticmethod
    def terms(client):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query)["searchText"][0]
                for call in client.get.call_args_list]


if __name__ == "__main__":
    unittest.main()
