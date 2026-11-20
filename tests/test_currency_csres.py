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
from guiju.currency.csres import CsresCurrency, extract_title_field, parse_csres, parse_csres_page
from guiju.currency.model import Record
from guiju.currency.registry import default_registry
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
        # 3 列 + 「编号：」title = 残缺数据行（有痕迹、0 条可解析）→ 50，不再静默成空。
        with self.assertRaises(NeedsHuman):
            parse(row(cells[:3]))
        self.assertEqual(parse(row(cells[:4]))[0].status, "unknown")
        for n in (5, 6):
            record = parse(row(cells[:n]))[0]
            self.assertEqual(record.status, "current")
            self.assertEqual(record.implement_date, "未公布")
        # 空编号行是坏行（W3/P1-4）：不能生成空 code 记录后当完整结果。
        with self.assertRaises(NeedsHuman):
            parse(row(["", "", "", ""]))

    def test_only_tr_own_exact_title_prefix_is_a_data_row(self):
        # 这些 title 都不算数据行：5 列结构在但 0 条可解析 = 改版痕迹 → 50（不猜编号）。
        cells = ["GB 1-2000", "标准", "部门", "2000-01-01", "现行"]
        for title in ("", " 编号：GB 1-2000", "编号:GB 1-2000", "标准编号：GB 1-2000"):
            with self.subTest(title=title), self.assertRaises(NeedsHuman):
                parse(row(cells, title))
        with self.assertRaises(NeedsHuman):
            parse('<tr><td title="编号：GB1">1</td><td>2</td><td>3</td><td>4</td></tr>')
        # th 不是数据单元格：既不成记录也不算数据痕迹（title 为空时是成功的空结果）；
        # 但「编号：」title 本身仍是数据痕迹 → 50。
        th_only = row(cells, title="").replace("<td>", "<th>").replace("</td>", "</th>")
        self.assertEqual(parse(th_only), [])
        with self.assertRaises(NeedsHuman):
            parse(row(cells).replace("<td>", "<th>").replace("</td>", "</th>"))

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

    def test_query_escapes_user_input_and_without_total_stays_on_page_one(self):
        # 页面没有"共 N 页"证据时只取第 1 页，且 has_more=None（W3/P1-4）：
        # 输入里的 pageNum=99 不能改写请求参数。
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


class CsresEmptyPageTest(unittest.TestCase):
    def test_zero_hit_response_page_zero_is_authoritative_empty(self):
        # 真实站点对未知编号返回“共找到 0 条”、pageNum=0、pageSize=25；
        # 0 是“无结果页”的站点标记，不是页号错位，更不能报 50。
        raw = (
            '<html><body><form><input name="pageNum" value="0">'
            '<input name="pageSize" value="25"></form>'
            '<div>共找到 0 条</div></body></html>'
        ).encode("gbk")
        page = parse_csres_page(raw)
        self.assertEqual(page.records, [])
        self.assertEqual(page.total, 0)
        self.assertIsNone(page.page)
        client = mock.Mock()
        client.get.return_value = raw
        self.assertEqual(CsresCurrency(client).lookup("GB99999-2099"), [])


class CsresDefaultRegistryTest(unittest.TestCase):
    def test_default_registry_explicitly_allows_plaintext_metadata_fallback(self):
        # 站点没有 443；组合根必须显式 opt-in，不能在默认路径上静默失去主源。
        client = mock.Mock()
        registry = default_registry(client)
        for code in ("GB 50011-2010", "JGJ 147-2004", "JTG B01-2014", "DB 11/T 1-2020"):
            with self.subTest(code=code):
                adapter = registry.match(code)
                self.assertIsNotNone(adapter)
                self.assertIsInstance(adapter.primary, CsresCurrency)
                self.assertTrue(adapter.primary.plaintext_fallback)

    def test_default_registry_survives_https_failure_and_marks_plaintext(self):
        # 不只检查构造参数：默认注册表实际 lookup 时，HTTPS 失败必须能回退 HTTP，
        # 且把风险传播到 FallbackCurrency（complete=False），不能伪装成权威结果。
        raw = (FIXTURES / "csres_gb50011.gbk").read_bytes()
        client = mock.Mock()
        client.get.side_effect = [SourceUnreachable("https blocked"), raw]
        adapter = default_registry(client).match("GB 50011-2001")
        self.assertIsNotNone(adapter)
        rows = adapter.lookup("GB 50011-2001")
        self.assertTrue(rows)
        self.assertTrue(adapter.primary.plaintext)
        self.assertFalse(adapter.complete)
        self.assertIn("明文", adapter.incomplete_reason)
        self.assertEqual(client.get.call_count, 2)
        self.assertIn("https://", client.get.call_args_list[0].args[0])
        self.assertIn("http://", client.get.call_args_list[1].args[0])

    def test_plaintext_fallback_is_recorded_and_not_authoritative(self):
        client = mock.Mock()
        client.get.side_effect = [SourceUnreachable("https blocked"), b"<html></html>"]
        adapter = CsresCurrency(client, plaintext_fallback=True)
        with self.assertWarns(RuntimeWarning):
            self.assertEqual(adapter.lookup("GB1"), [])
        self.assertTrue(adapter.plaintext)
        self.assertFalse(adapter.complete)
        self.assertIn("正文", adapter.incomplete_reason)


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
