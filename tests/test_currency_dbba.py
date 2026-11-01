"""dbba：Go 原始 JSON fixture 的逐字段离线回归与严格 JSON 契约。"""

import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from guiju.currency.dbba import SEARCH_URL, DbbaCurrency, parse_dbba
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable

FIXTURES = Path(__file__).parent / "fixtures" / "currency"


def body(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def entry(**changes):
    item = {"code": "DB31/T 792-2026", "chName": "标准名称", "status": "现行"}
    item.update(changes)
    return item


class DbbaGoldenTest(unittest.TestCase):
    def test_original_fixture_bytes_are_unchanged(self):
        for filename, length, digest in (
            ("dbba_db31t792.json", 565, "812c0ceba9bf785b5a4d1ef78f820492699b44cb77b9e5bdca0e6ae331d5a53a"),
            ("dbba_db31_373.json", 1354, "561278faf69d10627d563446bd2a43b2e96db9f92f7d2e7de91903bb86976c32"),
        ):
            with self.subTest(filename=filename):
                raw = (FIXTURES / filename).read_bytes()
                self.assertEqual(len(raw), length)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)

    def assert_fields(self, filename, expected):
        rows = parse_dbba((FIXTURES / filename).read_bytes())
        self.assertEqual(len(rows), len(expected))
        for index, fields in enumerate(expected):
            for field, value in fields.items():
                with self.subTest(filename=filename, row=index, field=field):
                    self.assertEqual(getattr(rows[index], field), value)

    def test_golden_792_each_field(self):
        self.assert_fields("dbba_db31t792.json", [{
            "code": "DB31/T 792-2026", "name": "硅单晶棒及单晶硅片单位产品能源消耗限额",
            "status": "current", "publish_date": "2026-02-14", "implement_date": "2026-06-01",
            "source": "dbba", "replaces": "DB31/ 792-2020", "raw": "现行",
        }])

    def test_golden_373_each_version_each_field(self):
        self.assert_fields("dbba_db31_373.json", [
            {"code": "DB31/ 373-2026", "name": "制药工业水污染物排放标准",
             "status": "current", "publish_date": "2026-07-30", "implement_date": "2026-12-01",
             "source": "dbba", "replaces": "DB31/ 373-2010", "raw": "现行"},
            {"code": "DB31/ 373-2010", "name": "生物制药行业污染物排放标准",
             "status": "superseded", "publish_date": "2010-05-17", "implement_date": "2010-07-01",
             "source": "dbba", "replaces": "DB31/ 373-2006", "raw": "有更新版"},
            {"code": "DB31/ 373-2006", "name": "生物制药行业污染物排放标准",
             "status": "superseded", "publish_date": "2006-11-01", "implement_date": "2007-02-01",
             "source": "dbba", "replaces": "", "raw": "有更新版"},
        ])


class DbbaParseTest(unittest.TestCase):
    def test_status_vocabulary_and_positive_withdrawal_evidence(self):
        for label, fz_date, expected in (
            ("现行", 0, "current"), ("废止", 0, "withdrawn"), ("有更新版", 0, "superseded"),
            ("现行", 1700000000000, "current"), ("有更新版", 1700000000000, "superseded"),
            ("陌生词", 1700000000000, "withdrawn"), ("陌生词", 0, "unknown"),
            ("即将实施", 0, "unknown"), ("", -1, "unknown"), ("现 行", 0, "unknown"),
        ):
            with self.subTest(label=label, fz_date=fz_date):
                raw_status = " " + label + " "
                record = parse_dbba(body({"records": [entry(status=raw_status, fzDate=fz_date)]}))[0]
                self.assertEqual(record.status, expected)
                self.assertEqual(record.raw, raw_status)

    def test_millisecond_dates_use_fixed_plus_eight_not_host_timezone(self):
        for milliseconds, expected in (
            (1780243200000, "2026-06-01"), (1780243199999, "2026-05-31"),
            (1780243200001, "2026-06-01"), (1770998400000, "2026-02-14"),
            (1, "1970-01-01"), (0, ""), (-1, ""),
        ):
            with self.subTest(milliseconds=milliseconds):
                record = parse_dbba(body({"records": [entry(actDate=milliseconds, issueDate=milliseconds)]}))[0]
                self.assertEqual(record.implement_date, expected)
                self.assertEqual(record.publish_date, expected)

    def test_empty_missing_null_fields_use_go_zero_values(self):
        for payload in ({}, {"records": None}, {"records": []}):
            with self.subTest(payload=payload):
                self.assertEqual(parse_dbba(body(payload)), [])
        null_item = {key: None for key in ("code", "chName", "status", "reviseStdCodes", "actDate", "issueDate", "fzDate")}
        for item in ({}, null_item, None):
            with self.subTest(item=item):
                record = parse_dbba(body({"records": [item], "total": None}))[0]
                for field in ("code", "name", "publish_date", "implement_date", "replaces", "raw"):
                    self.assertEqual(getattr(record, field), "")
                self.assertEqual(record.status, "unknown")
                self.assertEqual(record.source, "dbba")

    def test_trims_fields_but_keeps_original_code_spelling_and_replaces(self):
        record = parse_dbba(body({"records": [entry(
            code="  DB3311/T319—2025  ", chName=" 标准 名称 ", reviseStdCodes=" DB3311/T 319-2020；DB33/ 1-2000 ",
        )]}))[0]
        self.assertEqual(record.code, "DB3311/T319—2025")
        self.assertEqual(record.name, "标准 名称")
        self.assertEqual(record.replaces, "DB3311/T 319-2020；DB33/ 1-2000")

    def test_valid_unknown_fields_are_ignored_like_go_model(self):
        payload = {"records": [entry(unused={"any": [True, 1]})], "pages": "not-modelled", "size": []}
        self.assertEqual(parse_dbba(body(payload))[0].status, "current")

    def test_json_shape_and_records_type_errors_are_needs_human(self):
        for payload in (None, [], "records", 1, True, {"records": {}}, {"records": "bad"},
                        {"records": ["bad"]}, {"records": [123]}, {"records": [True]}, {"records": [[]]}):
            with self.subTest(payload=payload), self.assertRaisesRegex(NeedsHuman, "dbba"):
                parse_dbba(body(payload))

    def test_string_fields_reject_numbers_bools_containers_without_coercion(self):
        for key in ("code", "chName", "status", "reviseStdCodes"):
            for value in (123, 1.5, True, False, [], {}):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(NeedsHuman, "dbba"),
                ):
                    parse_dbba(body({"records": [entry(**{key: value})]}))

    def test_integer_fields_reject_strings_bools_floats_containers(self):
        for key in ("actDate", "issueDate", "fzDate"):
            for value in ("1780243200000", True, False, 1.0, [], {}):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(NeedsHuman, "dbba"),
                ):
                    parse_dbba(body({"records": [entry(**{key: value})]}))
        for value in ("1", True, 1.0, [], {}):
            with self.subTest(total=value), self.assertRaisesRegex(NeedsHuman, "dbba"):
                parse_dbba(body({"total": value, "records": []}))

    def test_integer_fields_enforce_go_int64_bounds(self):
        for key in ("actDate", "issueDate", "fzDate"):
            for value in (2 ** 63, -(2 ** 63) - 1):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaisesRegex(NeedsHuman, "dbba"),
                ):
                    parse_dbba(body({"records": [entry(**{key: value})]}))
        with self.assertRaisesRegex(NeedsHuman, "dbba"):
            parse_dbba(body({"total": 2 ** 63, "records": []}))

    def test_unrepresentable_date_is_needs_human(self):
        # 仍在 int64 范围，但已经超出 YYYY-MM-DD 可表达的日期。
        with self.assertRaisesRegex(NeedsHuman, "dbba.*日期范围"):
            parse_dbba(body({"records": [entry(actDate=2 ** 63 - 1)]}))

    def test_invalid_json_and_html_are_diagnosable_needs_human(self):
        for raw in (b"", b"{", b"{} {}", b"\xff",
                    b'<html><head><meta http-equiv="refresh" content="0;url=/"></head></html>'):
            with self.subTest(raw=raw), self.assertRaisesRegex(NeedsHuman, "dbba"):
                parse_dbba(raw)
        with self.assertRaises(NeedsHuman) as caught:
            parse_dbba(b'<html><meta http-equiv="refresh" content="0;url=/"></html>')
        self.assertIn("refresh", str(caught.exception))


class DbbaLookupTest(unittest.TestCase):
    def terms(self, client):
        return [call.args[1]["key"] for call in client.post_form.call_args_list]

    def test_post_form_without_json_accept_and_exact_query_text(self):
        client = mock.Mock()
        client.post_form.return_value = (FIXTURES / "dbba_db31t792.json").read_bytes()
        adapter = DbbaCurrency(client)
        self.assertEqual(adapter.name, "dbba")
        rows = adapter.lookup("DB31/T 792")
        self.assertEqual([r.code for r in rows], ["DB31/T 792-2026"])
        client.post_form.assert_called_once_with(
            SEARCH_URL, {"current": "1", "size": "20", "key": "DB31/T 792"}, accept=None)
        client.get.assert_not_called()

    def test_all_same_family_versions_are_returned_without_historical_inference(self):
        client = mock.Mock()
        client.post_form.return_value = (FIXTURES / "dbba_db31_373.json").read_bytes()
        rows = DbbaCurrency(client).lookup("DB31/ 373-1999")
        self.assertEqual([r.code for r in rows], ["DB31/ 373-2026", "DB31/ 373-2010", "DB31/ 373-2006"])
        self.assertEqual([r.status for r in rows], ["current", "superseded", "superseded"])
        client.post_form.assert_called_once()

    def test_family_filter_keeps_output_spelling(self):
        client = mock.Mock()
        client.post_form.return_value = body({"records": [entry(code="DB31/T 792—2026"), entry(code="DB31/T 7920-2026")]})
        rows = DbbaCurrency(client).lookup("db31/t792-2020")
        self.assertEqual([r.code for r in rows], ["DB31/T 792—2026"])
        client.post_form.assert_called_once()

    def test_successful_empty_or_other_family_queries_base_once(self):
        for first in (body({"records": []}), body({"records": [entry(code="DB31/T 7920-2026")]})):
            with self.subTest(first=first):
                client = mock.Mock()
                client.post_form.side_effect = [first, body({"records": [entry()]})]
                rows = DbbaCurrency(client).lookup("DB31/T 792-2020")
                self.assertEqual([r.code for r in rows], ["DB31/T 792-2026"])
                self.assertEqual(self.terms(client), ["DB31/T 792-2020", "DB31/T 792"])
                for call in client.post_form.call_args_list:
                    self.assertIsNone(call.kwargs["accept"])

    def test_nonmatching_fallback_is_also_filtered(self):
        client = mock.Mock()
        client.post_form.return_value = body({"records": [entry(code="DB31/T 7920-2026")]})
        self.assertEqual(DbbaCurrency(client).lookup("DB31/T 792-2020"), [])
        self.assertEqual(self.terms(client), ["DB31/T 792-2020", "DB31/T 792"])

    def test_no_year_is_not_queried_twice_and_blank_never_fetches(self):
        client = mock.Mock()
        client.post_form.return_value = body({"records": []})
        self.assertEqual(DbbaCurrency(client).lookup("DB31/T 792"), [])
        client.post_form.assert_called_once()
        client.reset_mock()
        for code in ("", " \t\n"):
            self.assertEqual(DbbaCurrency(client).lookup(code), [])
        client.post_form.assert_not_called()

    def test_errors_do_not_trigger_base_fallback(self):
        for error in (SourceUnreachable("dbba offline"), NeedsHuman("dbba changed"), ComplianceDenied("dbba blocked")):
            with self.subTest(error=type(error).__name__):
                client = mock.Mock()
                client.post_form.side_effect = error
                with self.assertRaises(type(error)):
                    DbbaCurrency(client).lookup("DB31/T 792-2020")
                client.post_form.assert_called_once()
        for raw in (body({"records": [entry(code=792)]}), b'<html><meta http-equiv="refresh"></html>'):
            with self.subTest(raw=raw):
                client = mock.Mock()
                client.post_form.return_value = raw
                with self.assertRaises(NeedsHuman):
                    DbbaCurrency(client).lookup("DB31/T 792-2020")
                client.post_form.assert_called_once()


if __name__ == "__main__":
    unittest.main()
