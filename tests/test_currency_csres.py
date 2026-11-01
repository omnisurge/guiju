"""ADR 0002 M1：原始 Go GBK golden 的逐字段离线回归。"""

import hashlib
import json
import os
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from guiju import currency
from guiju.currency.codes import code_key, filter_by_code, same_family
from guiju.currency.csres import CsresCurrency, extract_title_field, parse_csres
from guiju.currency.model import Record
from guiju.errors import NeedsHuman, SourceUnreachable

FIXTURES = Path(__file__).parent / "fixtures" / "currency"


def row(cells, title="编号：来自 title 的错误编号&#xA;发布日期：2001-07-20&#xA;备注：保留原文"):
    return '<tr title="' + title + '">' + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def parse(text):
    return parse_csres(("<table>" + text + "</table>").encode("gbk"))


class CsresGoldenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = (FIXTURES / "csres_gb50011.gbk").read_bytes()
        cls.want = json.loads((FIXTURES / "csres_gb50011.expected.json").read_text(encoding="utf-8"))

    def test_original_fixture_bytes_are_unchanged(self):
        self.assertEqual(len(self.raw), 10039)
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(),
                         "142c161609a8181247e11a2b347ac291d6729572ce9251deb77c1d0c13a6b039")
        with self.assertRaises(UnicodeDecodeError):
            self.raw.decode("utf-8")

    def test_golden_gb50011_each_field(self):
        got = parse_csres(self.raw)
        self.assertEqual(len(got), len(self.want))
        for i, expected in enumerate(self.want):
            for field, value in expected.items():
                with self.subTest(row=i, field=field):
                    self.assertEqual(getattr(got[i], field), value)

    def test_golden_no_field_pollution(self):
        for i, record in enumerate(parse_csres(self.raw)):
            for field in ("code", "name", "publish_date", "implement_date"):
                with self.subTest(row=i, field=field):
                    value = getattr(record, field)
                    self.assertFalse(any(c in value for c in "\n\r\t"))
                    if field != "code":
                        self.assertNotIn("：", value)

    def test_duplicate_publish_label_uses_first_and_keeps_raw(self):
        for record, expected in zip(parse_csres(self.raw), self.want, strict=True):
            self.assertEqual(record.publish_date, expected["publish_date"])
            self.assertEqual(record.implement_date, expected["implement_date"])
            self.assertEqual(record.raw.count("发布日期："), 2)
            self.assertIn("\n发布日期：" + record.implement_date, record.raw)
            self.assertEqual(record.replaces, "")

    def test_lookup_runs_full_gbk_parse_and_family_filter(self):
        client = mock.Mock()
        client.get.return_value = self.raw
        got = CsresCurrency(client).lookup("GB50011")
        self.assertEqual([r.code for r in got], [r["code"] for r in self.want])
        url = urllib.parse.urlsplit(client.get.call_args.args[0])
        self.assertEqual(url.path, "/s.jsp")
        self.assertEqual(urllib.parse.parse_qs(url.query), {"keyword": ["GB50011"], "pageNum": ["1"]})
        client.get.assert_called_once()


class CsresParseTest(unittest.TestCase):
    def test_status_vocabulary_unknown_is_not_current(self):
        for label, expected in (("现行", "current"), ("作废", "superseded"), ("废止", "withdrawn"),
                                ("即将实施", "upcoming"), ("", "unknown"), ("陌生词", "unknown"),
                                ("现 行", "unknown")):
            with self.subTest(label=label):
                record = parse(row(["GB 1-2000", "标准", "部门", "2099-01-01", " " + label + " "]))[0]
                self.assertEqual(record.status, expected)

    def test_column_count_and_no_guessing(self):
        cells = ["GB 1-2000", "标准", "部门", "未公布", "现行", "废止"]
        self.assertEqual(parse(row(cells[:3])), [])
        self.assertEqual(parse(row(cells[:4]))[0].status, "unknown")
        for n in (5, 6):
            record = parse(row(cells[:n]))[0]
            self.assertEqual(record.status, "current")
            self.assertEqual(record.implement_date, "未公布")
        empty = parse(row(["", "", "", ""]))[0]
        self.assertEqual((empty.code, empty.name, empty.implement_date), ("", "", ""))

    def test_only_tr_own_exact_title_prefix_is_a_data_row(self):
        cells = ["GB 1-2000", "标准", "部门", "2000-01-01", "现行"]
        for title in ("", " 编号：GB 1-2000", "编号:GB 1-2000", "标准编号：GB 1-2000"):
            with self.subTest(title=title):
                self.assertEqual(parse(row(cells, title)), [])
        self.assertEqual(parse('<tr><td title="编号：GB1">1</td><td>2</td><td>3</td><td>4</td></tr>'), [])
        self.assertEqual(parse(row(cells).replace("<td>", "<th>").replace("</td>", "</th>")), [])

    def test_nested_text_entities_and_void_tags(self):
        record = parse(row(["&nbsp;<a><font>GB 1-2000</font></a>&nbsp;",
                            "&nbsp;<span>甲&amp;乙</span><br><font>丙</font>&nbsp;丁&nbsp;",
                            "忽略\n部门", "2000-01-01", '<font color="red">现行</font>']))[0]
        self.assertEqual(record.code, "GB 1-2000")
        self.assertEqual(record.name, "甲&乙丙\xa0丁")
        self.assertEqual(record.status, "current")

    def test_nested_table_cells_are_not_extra_direct_columns(self):
        nested = "<table><tr><td>甲</td><td>乙</td></tr></table>"
        record = parse(row(["GB 1-2000", nested, "部门", "2000-01-01", "作废"]))[0]
        self.assertEqual(record.name, "甲乙")
        self.assertEqual(record.implement_date, "2000-01-01")
        self.assertEqual(record.status, "superseded")

    def test_implicit_cell_and_row_closes(self):
        text = '<tr title="编号：A"><td>GB 1-2000<td>甲<td>部门<td>2000-01-01<td>现行'
        text += '<tr title="编号：B"><td>GB 1-2010<td>乙<td>部门<td>2010-01-01<td>作废'
        self.assertEqual([r.code for r in parse(text)], ["GB 1-2000", "GB 1-2010"])

    def test_row_order_and_duplicates_are_preserved(self):
        a = row(["GB 1-2010", "甲", "部门", "", "废止"])
        b = row(["GB 1-2000", "乙", "部门", "", "现行"])
        self.assertEqual([r.code for r in parse(a + b + a)], ["GB 1-2010", "GB 1-2000", "GB 1-2010"])

    def test_title_entities_stop_date_and_notes_stay_only_in_raw(self):
        for sep in ("&#xA;", "&#10;", "\n", "\r", "\r\n", "&amp;#xA;"):
            title = f"编号：GB1{sep}发布日期：2001-07-20{sep}备注：甲{sep}乙"
            record = parse(row(["GB 1-2000", "名称", "部门", "2002-01-01", "现行"], title))[0]
            with self.subTest(sep=sep):
                self.assertEqual(record.publish_date, "2001-07-20")
                self.assertEqual(record.implement_date, "2002-01-01")
                self.assertIn("备注：甲", record.raw)
                self.assertEqual(record.replaces, "")

    def test_title_field_missing_or_non_iso_is_not_invented(self):
        self.assertEqual(extract_title_field("没有日期", "发布日期："), "")
        self.assertEqual(extract_title_field("发布日期： 未公布\n备注：甲", "发布日期："), "未公布")
        self.assertEqual(extract_title_field("发布日期：A\tB", "发布日期："), "A\tB")

    def test_empty_or_unrelated_html_is_successful_empty_result(self):
        for text in ("", "<html></html>", "<table><tr><td>无结果</td></tr></table>"):
            self.assertEqual(parse_csres(text.encode("gbk")), [])

    def test_bad_gbk_fails_closed_and_does_not_refetch(self):
        client = mock.Mock()
        client.get.return_value = b"\x81"
        with self.assertRaises(NeedsHuman) as ctx:
            CsresCurrency(client).lookup("GB1")
        self.assertEqual(ctx.exception.exit, 50)
        client.get.assert_called_once()

    def test_network_failure_is_not_an_empty_match(self):
        client = mock.Mock()
        client.get.side_effect = SourceUnreachable("offline")
        with self.assertRaises(SourceUnreachable):
            CsresCurrency(client).lookup("GB1")

    def test_query_escapes_user_input_and_stays_on_page_one(self):
        client = mock.Mock()
        client.get.return_value = b"<html></html>"
        code = "GB/T 50011-2010（2024年版）&pageNum=99"
        self.assertEqual(CsresCurrency(client).lookup(code), [])
        url = client.get.call_args.args[0]
        self.assertTrue(url.isascii())
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query),
                         {"keyword": [code], "pageNum": ["1"]})


class CodeFamilyTest(unittest.TestCase):
    def test_family_matrix_preserves_nature_and_dash_contract(self):
        for left, right, expected in (
            ("GB50011", "GB/T 50011-2010（2024年版）", True),
            ("gb\xa050011—2001", "GB/Z 50011–2010", True),
            ("JGJ147-2004", "JGJ 147－2016", True),
            ("JGJ3", "JGJ 203-2010", False),
            ("JGJ3", "JGJ/T 213-2010", False),
            ("GB1", "JGJ1", False),
            ("DG/TJ08-2007", "DGJ08-2007", False),
            ("DG/TJ08-2007", "DG/TJ08-2007-2026", True),
            ("", "GB1", False), (" ", "", False),
        ):
            with self.subTest(left=left, right=right):
                self.assertEqual(same_family(left, right), expected)
        self.assertEqual(code_key(" DB31/T\xa0792−2015 "), "DB31792-2015")
        self.assertEqual(code_key("DG/TJ08-2007"), "DG/TJ08-2007")

    def test_filter_does_not_rewrite_codes_or_sort(self):
        rows = [Record(code, "current") for code in ("JGJ 203-2010", "JGJ 3-2002", "JGJ/T 3-2010")]
        self.assertEqual(filter_by_code(rows, "JGJ3"), rows[1:])


class CurrencyPackageCompatibilityTest(unittest.TestCase):
    def test_public_model_and_five_column_projection_are_unchanged(self):
        self.assertIs(currency.Record, Record)
        record = Record("GB1", "current", "标准", "2000-01-01", "csres", "旧号", "1999-01-01", "证据")
        self.assertEqual(record.as_payload(), {"code": "GB1", "status": "current", "name": "标准",
                                              "implement_date": "2000-01-01", "source": "csres"})

    def test_default_is_native_and_explicit_native_is_honoured(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY_CACHE": "off"}, clear=True):
            self.assertEqual(currency.default_currency().name, "native")
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "native", "GUIJU_CURRENCY_CACHE": "off"}, clear=True):
            self.assertEqual(currency.default_currency().name, "native")


if __name__ == "__main__":
    unittest.main()
