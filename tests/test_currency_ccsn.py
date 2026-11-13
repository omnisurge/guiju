"""ccsn 移植的离线 golden 与查询契约；明确期望不由新解析器生成。"""

import json
import socket
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from guiju.currency.ccsn import SEARCH_URL, CcsnCurrency, parse_ccsn, status_ccsn
from guiju.currency.httpx import HttpClient
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable

FIXTURES = Path(__file__).parent / "fixtures" / "currency"
# 人工照录旧 ccsn_test.go 与原始响应：编号、名称、两日期、状态、来源、替代关系、原状态词。
GOLDEN = {
    "ccsn_gb50011.json": [{
        "code": "GB/T50011-2010（2024年版）",
        "name": "建筑抗震设计标准",
        "status": "current",
        "publish_date": "2024-04-24",
        "implement_date": "2024-08-01",
        "source": "ccsn",
        "replaces": "GB50011-2010（2016年版）",
        "raw": "现行",
        "fallback": "",
    }],
    "ccsn_jgj147.json": [{
        "code": "JGJ147-2016",
        "name": "建筑拆除工程安全技术规范",
        "status": "current",
        "publish_date": "2016-11-15",
        "implement_date": "2017-05-01",
        "source": "ccsn",
        "replaces": "JGJ147-2004",
        "raw": "现行",
        "fallback": "",
    }],
}


def response(*rows):
    return json.dumps({"Total": len(rows), "List": list(rows)}, ensure_ascii=False).encode("utf-8")


def page_response(rows, total=None, has_next=False):
    """照 PageInfo 实测形状造一页；HasNextPage 驱动翻页。"""
    return json.dumps({
        "Total": len(rows) if total is None else total,
        "List": list(rows),
        "HasNextPage": has_next,
    }, ensure_ascii=False).encode("utf-8")


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        # 测试失败也不得尝试连接上游；lookup 只注入内存客户端。
        guard = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("offline test attempted a socket connection"))
        guard.start()
        self.addCleanup(guard.stop)


class CcsnGoldenTest(OfflineTestCase):
    def test_golden_each_field_in_original_order(self):
        for filename, expected in GOLDEN.items():
            got = parse_ccsn((FIXTURES / filename).read_bytes())
            with self.subTest(fixture=filename):
                self.assertEqual(len(got), len(expected))
            for index, (record, wanted) in enumerate(zip(got, expected, strict=True)):
                self.assertEqual(set(asdict(record)), set(wanted))
                for field, value in wanted.items():
                    with self.subTest(fixture=filename, row=index, field=field):
                        self.assertEqual(getattr(record, field), value)

    def test_golden_lookup_posts_numeric_query_only(self):
        client = mock.Mock(spec=HttpClient)
        client.post_form.return_value = (FIXTURES / "ccsn_gb50011.json").read_bytes()
        source = CcsnCurrency(client)
        self.assertEqual(source.name, "ccsn")
        self.assertEqual([asdict(record) for record in source.lookup("GB 50011-2010")], GOLDEN["ccsn_gb50011.json"])
        client.post_form.assert_called_once_with(
            SEARCH_URL, {"pageIndex": "1", "pageSize": "20", "gjz": "50011-2010"}, accept="application/json",
        )
        client.get.assert_not_called()


class CcsnParseTest(OfflineTestCase):
    def test_status_vocabulary_and_unknown_are_not_guessed(self):
        for state, abolish, expected in (
            ("现行", "", "current"), ("废止", "", "withdrawn"),
            ("即将实施", "", "upcoming"), ("在编", "", "upcoming"),
            ("局修完成", "", "unknown"), ("未知词", "", "unknown"),
            ("", "", "unknown"), ("未知词", "2016-08-01", "superseded"),
            # M20 修正：「局修完成」是明确词，即使有 abolish 日期也判 unknown，不再被兜底成 superseded
            ("局修完成", "2016-08-01", "unknown"),
            ("未知词", " \t ", "unknown"), (" 现行 ", "2000-01-01", "current"),
        ):
            with self.subTest(state=state, abolish=abolish):
                self.assertEqual(status_ccsn(state, abolish), expected)
                record = parse_ccsn(response({"StandardCode": "GB1-2000", "StandardState": state, "AbolishDate": abolish}))[0]
                self.assertEqual(record.status, expected)
                self.assertEqual(record.raw, state)

    def test_trim_dates_and_replaces_but_preserve_raw_state(self):
        record = parse_ccsn(response({
            "StandardCode": " GB/T 1-2000（2024年版） ", "StandardCNName": " 标准 名称 ",
            "StandardState": " \t局修完成 \n", "AbolishDate": None,
            "ReplaceStandardCode": " GB 1-1980、GB 1-1990 ",
            "PublishDate": " 2024-04-24T00:00:00 ", "PerformDate": " 2024-08-01 00:00:00 ",
        }))[0]
        self.assertEqual(asdict(record), {
            "code": "GB/T 1-2000（2024年版）", "name": "标准 名称", "status": "unknown",
            "publish_date": "2024-04-24", "implement_date": "2024-08-01",
            "source": "ccsn", "replaces": "GB 1-1980、GB 1-1990", "raw": " \t局修完成 \n", "fallback": "",
        })

    def test_unrecognised_dates_are_preserved_without_invention(self):
        record = parse_ccsn(response({"StandardCode": "GB1", "PublishDate": " 未公布 ", "PerformDate": " 2000 "}))[0]
        self.assertEqual((record.publish_date, record.implement_date, record.status), ("未公布", "2000", "unknown"))

    def test_missing_and_null_strings_use_go_zero_values(self):
        record = parse_ccsn(response({"StandardCode": "GB1", "StandardCNName": None, "StandardState": None}))[0]
        self.assertEqual(asdict(record), {
            "code": "GB1", "name": "", "status": "unknown", "publish_date": "", "implement_date": "",
            "source": "ccsn", "replaces": "", "raw": "", "fallback": "",
        })
        for raw in (b"{}", b'{"List":null}', response(None), response({}, {"StandardCode": " \t"})):
            with self.subTest(raw=raw):
                self.assertEqual(parse_ccsn(raw), [])

    def test_rows_and_duplicates_keep_upstream_order(self):
        rows = [{"StandardCode": code} for code in ("JGJ2-2000", "JGJ1-2010", "JGJ2-2000")]
        self.assertEqual([r.code for r in parse_ccsn(response(*rows))], [row["StandardCode"] for row in rows])

    def test_total_accepts_missing_null_and_int64_values(self):
        rows = [{"StandardCode": "GB1"}]
        payloads = [{"List": rows}]
        payloads.extend({"Total": value, "List": rows} for value in (None, 0, 1, -1, -(2**63), 2**63 - 1))
        for payload in payloads:
            with self.subTest(payload=payload):
                raw = json.dumps(payload).encode("utf-8")
                self.assertEqual([record.code for record in parse_ccsn(raw)], ["GB1"])

    def test_total_wrong_types_fractions_and_int64_overflow_fail_closed(self):
        for value in ("1", True, False, 1.0, 1.5, {}, [], 2**63, -(2**63) - 1):
            with self.subTest(total=value, type=type(value).__name__):
                raw = json.dumps({"Total": value, "List": []}).encode("utf-8")
                with self.assertRaises(NeedsHuman) as ctx:
                    parse_ccsn(raw)
                self.assertEqual(ctx.exception.exit, 50)
                self.assertIn("Total", str(ctx.exception))
                self.assertIn("ccsn", str(ctx.exception))

    def test_malformed_json_or_typed_fields_fail_closed(self):
        cases = [b"", b"\xff", b"{", b"[]", b'{"List":{}}', b'{"List":[42]}']
        for key in ("StandardCode", "StandardCNName", "StandardState", "AbolishDate", "ReplaceStandardCode", "PublishDate", "PerformDate"):
            cases.append(response({"StandardCode": "GB1", key: 7}))
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(NeedsHuman) as ctx:
                parse_ccsn(raw)
            self.assertEqual(ctx.exception.exit, 50)
            self.assertIn("ccsn", str(ctx.exception))


class CcsnLookupTest(OfflineTestCase):
    def test_no_digits_do_not_query_full_catalog(self):
        client = mock.Mock(spec=HttpClient)
        for code in ("", " \t", "GB", "JGJ/T", "标准"):
            with self.subTest(code=code):
                self.assertEqual(CcsnCurrency(client).lookup(code), [])
        client.post_form.assert_not_called()
        client.get.assert_not_called()

    def test_numeric_query_omits_nature_and_edition_suffix(self):
        client = mock.Mock(spec=HttpClient)
        client.post_form.return_value = (FIXTURES / "ccsn_gb50011.json").read_bytes()
        self.assertEqual([r.code for r in CcsnCurrency(client).lookup("GB/T 50011-2010（2024年版）")],
                         ["GB/T50011-2010（2024年版）"])
        self.assertEqual(client.post_form.call_args.args[1]["gjz"], "50011-2010")
        client.post_form.assert_called_once()

    def test_fuzzy_hits_are_filtered_before_return(self):
        client = mock.Mock(spec=HttpClient)
        client.post_form.return_value = response(
            {"StandardCode": "GB50141-2008", "StandardState": "现行"},
            {"StandardCode": "GB/T50011-2010（2024年版）", "StandardState": "现行"},
        )
        self.assertEqual([r.code for r in CcsnCurrency(client).lookup("GB 50011-2010")], ["GB/T50011-2010（2024年版）"])
        client.post_form.assert_called_once()

    def test_old_year_retries_base_once_without_inventing_old_status(self):
        client = mock.Mock(spec=HttpClient)
        client.post_form.side_effect = [response(), (FIXTURES / "ccsn_jgj147.json").read_bytes()]
        records = CcsnCurrency(client).lookup("JGJ147-2004")
        self.assertEqual([asdict(record) for record in records], GOLDEN["ccsn_jgj147.json"])
        self.assertEqual([call.args[1]["gjz"] for call in client.post_form.call_args_list], ["147-2004", "147"])
        self.assertNotIn("JGJ147-2004", [record.code for record in records])

    def test_unrelated_first_response_also_triggers_base_fallback(self):
        client = mock.Mock(spec=HttpClient)
        client.post_form.side_effect = [response({"StandardCode": "JGJ1147-2004"}), response({"StandardCode": "JGJ147-2016", "StandardState": "现行"})]
        self.assertEqual([r.code for r in CcsnCurrency(client).lookup("JGJ147-2004")], ["JGJ147-2016"])
        self.assertEqual([call.args[1]["gjz"] for call in client.post_form.call_args_list], ["147-2004", "147"])

    def test_same_family_first_hit_does_not_retry_even_if_year_differs(self):
        client = mock.Mock(spec=HttpClient)
        client.post_form.return_value = (FIXTURES / "ccsn_jgj147.json").read_bytes()
        self.assertEqual([r.code for r in CcsnCurrency(client).lookup("JGJ147-2004")], ["JGJ147-2016"])
        client.post_form.assert_called_once()

    def test_no_year_makes_one_request_and_old_year_makes_at_most_two(self):
        for code, expected in (("JGJ147", ["147"]), ("JGJ147-2004", ["147-2004", "147"])):
            client = mock.Mock(spec=HttpClient)
            client.post_form.return_value = response()
            with self.subTest(code=code):
                self.assertEqual(CcsnCurrency(client).lookup(code), [])
                self.assertEqual([call.args[1]["gjz"] for call in client.post_form.call_args_list], expected)

    def test_failures_propagate_without_base_retry(self):
        for error in (SourceUnreachable("offline"), ComplianceDenied("stop"), NeedsHuman("ccsn bad JSON")):
            client = mock.Mock(spec=HttpClient)
            client.post_form.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as ctx:
                CcsnCurrency(client).lookup("JGJ147-2004")
            self.assertIs(ctx.exception, error)
            client.post_form.assert_called_once()

    def test_decode_failure_does_not_fall_back_or_refetch(self):
        client = mock.Mock(spec=HttpClient)
        client.post_form.return_value = b"{"
        with self.assertRaises(NeedsHuman):
            CcsnCurrency(client).lookup("JGJ147-2004")
        client.post_form.assert_called_once()

    def test_existing_five_field_payload_is_unchanged(self):
        record = parse_ccsn((FIXTURES / "ccsn_jgj147.json").read_bytes())[0]
        self.assertEqual(record.as_payload(), {
            "code": "JGJ147-2016", "name": "建筑拆除工程安全技术规范", "status": "current",
            "implement_date": "2017-05-01", "source": "ccsn",
        })


class CcsnPaginationTest(OfflineTestCase):
    """分页契约（2026-11-13 实测）：HasNextPage 驱动 pageIndex+1 续页，超 MAX_PAGES 截断告警。"""

    def _code(self, year):
        # 同族不同年版（filter_by_code 只按编号族筛，年版归下游裁决）
        return {"StandardCode": f"GB50000-{year}", "StandardState": "现行", "StandardCNName": "n"}

    def test_follows_pages_until_no_next(self):
        client = mock.Mock(spec=HttpClient)
        # 45 条同族记录跨 3 页；页内数据混若干异族行（真实站会回），过滤后仍应取全 45 条。
        flat = [self._code(1900 + i) for i in range(45)]
        client.post_form.side_effect = [
            page_response([*flat[0:20], {"StandardCode": "ZZ999-2000"}], total=45, has_next=True),
            page_response(flat[20:40], total=45, has_next=True),
            page_response(flat[40:45], total=45, has_next=False),
        ]
        cur = CcsnCurrency(client)
        records = cur.lookup("GB 50000")
        self.assertEqual(len(records), 45)
        self.assertEqual([c.args[1]["pageIndex"] for c in client.post_form.call_args_list], ["1", "2", "3"])
        self.assertEqual(cur.pagination["fetched_pages"], 3)
        self.assertFalse(cur.pagination["has_more"])
        self.assertFalse(cur.pagination["truncated"])

    def test_truncated_at_max_pages_warns(self):
        client = mock.Mock(spec=HttpClient)
        client.post_form.return_value = page_response(
            [self._code(1900 + i) for i in range(20)], total=500, has_next=True,
        )
        cur = CcsnCurrency(client)
        import warnings as _w
        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            cur.lookup("GB 50000")
        self.assertEqual(client.post_form.call_count, cur.MAX_PAGES)
        self.assertTrue(cur.pagination["truncated"])
        self.assertTrue(any("截断" in str(w.message) for w in caught))


if __name__ == "__main__":
    unittest.main()
