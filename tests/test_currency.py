"""时效端口 —— 契约解析必须稳（上游 TSV 是机器契约）。"""

import unittest

from guiju.currency import (
    CachedCurrency,
    CommandCurrency,
    McpCurrency,
    NullCurrency,
    Record,
    _parse_sse,
    _tool_text,
    parse_tsv,
)
from guiju.errors import SourceUnreachable

RAW = """GB 50011-2010\tcurrent\t建筑抗震设计规范\t2010-12-01\tcsres
GB 50011-2001\tsuperseded\t建筑抗震设计规范\t2001-12-01\tcsres
"""


class Fake:
    name = "fake"

    def __init__(self, records):
        self.records = records
        self.calls = 0

    def lookup(self, code):
        self.calls += 1
        return self.records


class ParseTest(unittest.TestCase):
    def test_parses_contract_columns(self):
        rows = parse_tsv(RAW)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].code, "GB 50011-2010")
        self.assertEqual(rows[0].status, "current")
        self.assertEqual(rows[0].name, "建筑抗震设计规范")
        self.assertEqual(rows[0].implement_date, "2010-12-01")
        self.assertEqual(rows[0].source, "csres")
        self.assertTrue(rows[0].is_current)
        self.assertFalse(rows[1].is_current)

    def test_skips_comment_lines(self):
        rows = parse_tsv("# [! 降级: 3 天前的缓存]\nGB 50011-2010\tcurrent\tX\n")
        self.assertEqual(len(rows), 1)

    def test_tolerates_missing_columns(self):
        rows = parse_tsv("GB 50011-2010\tcurrent\n")
        self.assertEqual(rows[0].name, "")

    def test_empty(self):
        self.assertEqual(parse_tsv(""), [])

    def test_status_is_lowercased(self):
        self.assertEqual(parse_tsv("X\tCURRENT\tY\n")[0].status, "current")


class SseParseTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_parse_sse('{"a":1}'), {"a": 1})

    def test_sse_frame(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n'
        self.assertEqual(_parse_sse(body), {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_garbage(self):
        self.assertIsNone(_parse_sse("nope"))

    def test_tool_text(self):
        result = {"result": {"content": [{"type": "text", "text": RAW}]}}
        self.assertIn("GB 50011-2010", _tool_text(result))


class CacheTest(unittest.TestCase):
    def test_second_lookup_does_not_hit_upstream(self):
        fake = Fake([Record(code="GB 1-2020", status="current", name="X")])
        cached = CachedCurrency(fake)
        cached.lookup("GB 1-2020")
        cached.lookup("GB 1-2020")
        self.assertEqual(fake.calls, 1)

    def test_null_currency_is_silent(self):
        self.assertEqual(NullCurrency().lookup("GB 1-2020"), [])

    def test_command_currency_missing_binary_is_unreachable(self):
        # 契约：连不上要抛（20，可重试），不能静默返回空数组冒充"没有这个编号"
        with self.assertRaises(SourceUnreachable):
            CommandCurrency("/nonexistent/spec-currency").lookup("GB 1-2020")

    def test_mcp_currency_unreachable_is_unreachable(self):
        with self.assertRaises(SourceUnreachable):
            McpCurrency("http://127.0.0.1:9/mcp").lookup("GB 1-2020")


if __name__ == "__main__":
    unittest.main()
