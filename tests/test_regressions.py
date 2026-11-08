"""高危 bug 的定向回归 —— 覆盖退出码、分类器之外的崩溃点与 MCP 处理。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from guiju.domain.codes import classify
from guiju.domain.search import _year_of
from guiju.errors import BadUsage, NotIndexed, SourceUnreachable
from guiju.store.sqlite import Store, persist_codes


class YearOfTest(unittest.TestCase):
    """published_on='待定' 曾让排序键抛 ValueError（internal 1）。"""

    def test_non_numeric_year_is_zero(self):
        self.assertEqual(_year_of("待定"), 0)
        self.assertEqual(_year_of(None), 0)
        self.assertEqual(_year_of(""), 0)
        self.assertEqual(_year_of("2026-10-31"), 2026)


class RefIsCodeTest(unittest.TestCase):
    """which/resolve 入口分流：名称形态必须先走本地检索，不能去问时效服务。"""

    def test_cjk_name_is_not_code(self):
        from guiju.cli.lookup import ref_is_code

        self.assertFalse(ref_is_code("绿色建筑评价标准"))
        self.assertFalse(ref_is_code("《绿色建筑评价标准》"))
        self.assertTrue(ref_is_code("GB/T 50378-2019"))
        self.assertFalse(ref_is_code("12345"))
        self.assertFalse(ref_is_code("https://www.mohurd.gov.cn/x.html"))
        self.assertFalse(ref_is_code(""))


class ResolveExitTest(unittest.TestCase):
    """裸 GuijuError 让「没收录」变成 internal(1)：分流到对应退出码。"""

    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "c.db")
        self.addCleanup(self.store.close)

    def test_empty_ref_is_bad_usage(self):
        from guiju.cli.lookup import resolve

        with self.assertRaises(BadUsage):
            resolve(self.store, "  ")

    def test_unknown_url_is_not_indexed(self):
        from guiju.cli.lookup import resolve

        with self.assertRaises(NotIndexed):
            resolve(self.store, "https://www.mohurd.gov.cn/not-in-db.html")

    def test_unknown_ann_id_is_not_indexed(self):
        from guiju.cli.lookup import resolve

        with self.assertRaises(NotIndexed):
            resolve(self.store, "999999")


class PersistCodesTest(unittest.TestCase):
    """ambiguous 兜底 primary 没资格把同族编号写成 superseded_ref（之前一律改写）。"""

    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "c.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/x.html", "2021-07-15", "标题", "建筑拆除工程安全技术规范", "national"
        )

    def test_ambiguous_primary_does_not_demote(self):
        codes = classify("《建筑拆除工程安全技术规范》JGJ147-2016与JGJ147-2004均已列入目录。")
        persist_codes(self.store, 1, codes, "建筑拆除工程安全技术规范")
        roles = {r["code_key"]: r["role"] for r in self.store.mentions_for(1)}
        self.assertEqual(roles["JGJ147-2004"], "mentioned")
        self.assertEqual(roles["JGJ147-2016"], "mentioned")

    def test_decisive_primary_still_demotes(self):
        codes = classify("现批准《住宅性能评定标准》为国家标准，编号为GB/T50362-2022，自2023年5月1日起实施。原《住宅性能评定标准》GB/T50362-2005同时废止。")
        persist_codes(self.store, 1, codes, "住宅性能评定标准")
        roles = {r["code_key"]: r["role"] for r in self.store.mentions_for(1)}
        self.assertEqual(roles["GB/T50362-2005"], "superseded_ref")
        self.assertEqual(roles["GB/T50362-2022"], "primary")


class McpCurrencyTest(unittest.TestCase):
    """JSON-RPC {"error":...} 曾静默变 []（→31 没收录），掩盖 20 不可达。"""

    def _fake_urlopen(self, responses):
        class _Resp:
            def __init__(self, body, headers=None):
                self._body = body
                self.headers = headers or {}
            def read(self):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        it = iter(responses)
        return lambda req, timeout=None: _Resp(next(it))

    def test_rpc_error_is_unreachable_not_empty(self):
        from guiju.currency import McpCurrency

        err_body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "backend down"}}
        ).encode()
        cur = McpCurrency("http://example/mcp")
        with patch("urllib.request.urlopen", self._fake_urlopen([err_body])), self.assertRaises(SourceUnreachable):
            cur.lookup("GB 50016-2022")

    def test_bearer_token_sent(self):
        from guiju.currency import McpCurrency

        seen = []

        class _Resp:
            @property
            def headers(self):
                return {}

            def read(self):
                return b'{"jsonrpc":"2.0","id":1,"result":{}}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def capture(req, timeout=None):
            seen.append(req.headers.get("Authorization"))
            return _Resp()

        cur = McpCurrency("http://example/mcp", token="t123")
        with patch("urllib.request.urlopen", capture):
            cur.lookup("GB 50016-2022")
        self.assertTrue(all(h == "Bearer t123" for h in seen))
        self.assertTrue(seen)


if __name__ == "__main__":
    unittest.main()
