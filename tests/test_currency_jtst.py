"""jtst：Go 原始 HTML fixture 的逐字段离线回归与请求契约。"""

import hashlib
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from guiju.currency.jtst import SEARCH_URL, JtstCurrency, parse_jtst
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable

FIXTURES = Path(__file__).parent / "fixtures" / "currency"


def row(code="JTG D60-2015", name="公路桥涵设计通用规范", status="现行"):
    return (f'<tr><td><a><span class="en-code">{code}</span>&nbsp;&nbsp;{name}</a></td>'
            f'<td><span class="s-status">{status}</span></td></tr>')


def document(content):
    return ("<table>" + content + "</table>").encode("utf-8")


class JtstGoldenTest(unittest.TestCase):
    def test_original_fixture_bytes_are_unchanged(self):
        for filename, length, digest in (
            ("jtst_jtgd60.html", 7948, "ec49e688c0d43538d322365fb532d14582a732b97dec33038dff6b0dcd593d93"),
            ("jtst_empty.html", 4992, "da27626a3c5c6d0d411d64086cc4609e1177aefd5f809342ef3c1a11efafd9ef"),
        ):
            with self.subTest(filename=filename):
                raw = (FIXTURES / filename).read_bytes()
                self.assertEqual(len(raw), length)
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)

    def test_golden_each_field(self):
        rows = parse_jtst((FIXTURES / "jtst_jtgd60.html").read_bytes())
        self.assertEqual(len(rows), 1)
        expected = {
            "code": "JTG D60-2015", "name": "公路桥涵设计通用规范", "status": "current",
            "publish_date": "", "implement_date": "", "source": "jtst", "replaces": "", "raw": "",
        }
        for field, value in expected.items():
            with self.subTest(field=field):
                self.assertEqual(getattr(rows[0], field), value)

    def test_golden_empty_page(self):
        self.assertEqual(parse_jtst((FIXTURES / "jtst_empty.html").read_bytes()), [])


class JtstParseTest(unittest.TestCase):
    def test_sacinfo_text_is_concatenated_and_original_spacing_kept(self):
        record = parse_jtst(document(row("<sacinfo>JTG</sacinfo> <sacinfo>D60-2015</sacinfo>")))[0]
        self.assertEqual(record.code, "JTG D60-2015")
        self.assertEqual(record.name, "公路桥涵设计通用规范")
        record = parse_jtst(document(row(" JTG  D60—2015 ", "桥涵  设计")))[0]
        self.assertEqual(record.code, "JTG  D60—2015")
        self.assertEqual(record.name, "桥涵  设计")

    def test_first_nonempty_status_in_this_row_only(self):
        content = ('<span class="s-status">废止</span><table><tr><td>'
                   '<span class="s-status">&nbsp; </span><span class="s-status">即将实施</span>'
                   '<a><span class="en-code">JTG D60-2015</span> 名称</a>'
                   '<span class="s-status">现行</span></td></tr></table>'
                   '<span class="s-status">废止</span>')
        record = parse_jtst(content.encode())[0]
        self.assertEqual(record.status, "upcoming")

    def test_first_nonempty_unknown_status_stays_unknown(self):
        content = row(status="陌生词</span><span class='s-status'>现行")
        self.assertEqual(parse_jtst(document(content))[0].status, "unknown")

    def test_status_vocabulary_does_not_guess_current(self):
        for status, expected in (("现行", "current"), ("即将实施", "upcoming"), ("废止", "withdrawn"),
                                 ("", "unknown"), ("别的词", "unknown"), ("作废", "unknown"), ("现 行", "unknown")):
            with self.subTest(status=status):
                self.assertEqual(parse_jtst(document(row(status=" " + status + " ")))[0].status, expected)

    def test_class_matching_is_exact_and_whitespace_separated(self):
        content = ('<tr><td><span class="not-en-code">错误号</span>'
                   '<a><span class="first\ten-code other">JTG D60-2015</span> 名称</a>'
                   '<span class="not-s-status">废止</span><span class="label s-status x">现行</span></td></tr>')
        record = parse_jtst(document(content))[0]
        self.assertEqual((record.code, record.name, record.status), ("JTG D60-2015", "名称", "current"))

    def test_missing_or_first_empty_code_is_not_a_record(self):
        for content in ('<tr><td><span class="s-status">现行</span></td></tr>',
                        '<tr><td><span class="en-code">&nbsp;</span>'
                        '<a><span class="en-code">JTG D60-2015</span> 名称</a></td></tr>'):
            with self.subTest(content=content):
                self.assertEqual(parse_jtst(document(content)), [])

    def test_first_code_only_and_no_anchor_means_empty_name(self):
        content = ('<tr><td><span class="en-code">JTG D60-2015</span> 外部名称'
                   '<a><span class="en-code">JTG B01-2014</span> 不取这个名称</a></td></tr>')
        record = parse_jtst(document(content))[0]
        self.assertEqual((record.code, record.name), ("JTG D60-2015", ""))

    def test_anchor_prefix_is_removed_once_not_replaced_globally(self):
        code = "JTG D60-2015"
        record = parse_jtst(document(row(code, f"{code} 适用范围")))[0]
        self.assertEqual(record.name, f"{code} 适用范围")
        content = f'<tr><td><a>前言 <span class="en-code">{code}</span> 名称</a></td></tr>'
        self.assertEqual(parse_jtst(document(content))[0].name, f"前言 {code} 名称")

    def test_nested_layout_tr_duplicates_are_preserved_in_go_order(self):
        content = "<tr><td><table>" + row() + row("JTG B01-2014", "公路工程技术标准", "废止") + "</table></td></tr>"
        rows = parse_jtst(document(content))
        self.assertEqual([r.code for r in rows], ["JTG D60-2015", "JTG D60-2015", "JTG B01-2014"])
        self.assertEqual([r.status for r in rows], ["current", "current", "withdrawn"])
        self.assertEqual(rows[0], rows[1])

    def test_omitted_table_end_tags_and_void_elements(self):
        content = ('<tr><td><a><span class="en-code">JTG D60-2015</span><br/>名称</a>'
                   '<td><span class="s-status">现行</span>'
                   '<tr><td><a><span class="en-code">JTG B01-2014</span>另一名称</a>'
                   '<td><span class="s-status">废止</span>')
        rows = parse_jtst(document(content))
        self.assertEqual([(r.code, r.name, r.status) for r in rows],
                         [("JTG D60-2015", "名称", "current"), ("JTG B01-2014", "另一名称", "withdrawn")])

    def test_comments_and_scripts_do_not_add_records(self):
        content = f"<!--{row()}--><script>var example = '{row()}';</script><style>.en-code {{color:red}}</style>"
        self.assertEqual(parse_jtst(content.encode()), [])
        self.assertEqual(parse_jtst(b""), [])

    def test_invalid_utf8_is_needs_human_not_empty(self):
        with self.assertRaisesRegex(NeedsHuman, "jtst.*解码"):
            parse_jtst(b"\xff")


class JtstLookupTest(unittest.TestCase):
    def terms(self, client):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query)["q"][0]
                for call in client.get.call_args_list]

    def test_request_endpoint_parameters_and_html_accept(self):
        client = mock.Mock()
        client.get.return_value = (FIXTURES / "jtst_jtgd60.html").read_bytes()
        adapter = JtstCurrency(client)
        self.assertEqual(adapter.name, "jtst")
        rows = adapter.lookup("JTG D60-2015")
        self.assertEqual([r.code for r in rows], ["JTG D60-2015"])
        query = urllib.parse.urlencode({"tid": "", "op": "", "q": "JTG D60-2015"})
        client.get.assert_called_once_with(f"{SEARCH_URL}?{query}", accept="text/html")
        client.post_form.assert_not_called()

    def test_only_same_family_is_returned_without_normalizing_output(self):
        client = mock.Mock()
        client.get.return_value = document(row("JTG D60—2015") + row("JTG B01-2014"))
        rows = JtstCurrency(client).lookup("jtgd60-2004")
        self.assertEqual([r.code for r in rows], ["JTG D60—2015"])
        self.assertEqual(rows[0].status, "current")
        client.get.assert_called_once()  # 同族新版已返回，不重查也不生成旧版记录。

    def test_successful_empty_or_other_family_retries_base_once(self):
        for first in (document(""), document(row("JTG B01-2014"))):
            with self.subTest(first=first):
                client = mock.Mock()
                client.get.side_effect = [first, document(row())]
                rows = JtstCurrency(client).lookup("JTG D60-2004")
                self.assertEqual([r.code for r in rows], ["JTG D60-2015"])
                self.assertEqual(self.terms(client), ["JTG D60-2004", "JTG D60"])

    def test_nonmatching_fallback_is_also_filtered(self):
        client = mock.Mock()
        client.get.return_value = document(row())
        self.assertEqual(JtstCurrency(client).lookup("JTG B01-2014"), [])
        self.assertEqual(self.terms(client), ["JTG B01-2014", "JTG B01"])

    def test_no_year_is_not_queried_twice_and_blank_never_fetches(self):
        client = mock.Mock()
        client.get.return_value = document("")
        self.assertEqual(JtstCurrency(client).lookup("JTG D60"), [])
        client.get.assert_called_once()
        client.reset_mock()
        for code in ("", " \t\n"):
            self.assertEqual(JtstCurrency(client).lookup(code), [])
        client.get.assert_not_called()

    def test_errors_do_not_trigger_base_fallback(self):
        for error in (SourceUnreachable("jtst offline"), NeedsHuman("jtst changed"), ComplianceDenied("jtst blocked")):
            with self.subTest(error=type(error).__name__):
                client = mock.Mock()
                client.get.side_effect = error
                with self.assertRaises(type(error)):
                    JtstCurrency(client).lookup("JTG D60-2004")
                client.get.assert_called_once()
        client = mock.Mock()
        client.get.return_value = b"\xff"
        with self.assertRaises(NeedsHuman):
            JtstCurrency(client).lookup("JTG D60-2004")
        client.get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
