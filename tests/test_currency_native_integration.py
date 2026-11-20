"""真实编排 + HttpClient + 原始 golden + 临时磁盘缓存的离线整链路。"""

import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from guiju.currency.cache import DEFAULT_STALE_LIMIT
from guiju.currency.httpx import HttpClient
from guiju.currency.native import NativeCurrency, NoCurrencySource
from guiju.currency.registry import default_registry
from guiju.errors import ComplianceDenied, SourceUnreachable
from guiju.store.currency import DiskCurrencyCache
from tests.currency_helpers import FakeResponse

CSRES = "http://www.csres.com/s.jsp"
CCSN = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
CODE = "GB50011"


def down():
    return urllib.error.HTTPError(CSRES, 503, "Unavailable", {}, io.BytesIO(b"temporary error"))


class NativeDiskIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ccsn = (Path(__file__).parent / "fixtures/currency/ccsn_gb50011.json").read_bytes()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="guiju-m2-integration-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "currency"
        self.now = 1_800_000_000.0
        self.client = HttpClient()
        self.client._opener = mock.Mock()
        self.cache = DiskCurrencyCache(self.directory, clock=lambda: self.now)
        self.native = NativeCurrency(default_registry(self.client), self.cache, clock=lambda: self.now)
        mock.patch("guiju.currency.httpx._throttle").start()
        mock.patch("guiju.currency.httpx.time.sleep").start()
        mock.patch("socket.create_connection", side_effect=AssertionError("不应真实联网")).start()
        self.addCleanup(mock.patch.stopall)

    def fallback_response(self):
        return FakeResponse(self.ccsn, url=CCSN, headers={"Content-Type": "application/json"})

    def test_fallback_golden_disk_restart_fresh_stale_and_40(self):
        # 默认 csres 组合根显式开启明文回退：HTTPS 3 次重试 + HTTP 3 次重试
        # 后才轮到 ccsn 的 POST（这是 CSRES 无 443 的已知传输事实）。
        self.client._opener.open.side_effect = [down() for _ in range(6)] + [self.fallback_response()]
        first = self.native.query(CODE)
        self.assertEqual(first.source, "csres")
        self.assertEqual(first.fallback_source, "ccsn")
        self.assertEqual(len(first.results), 1)
        self.assertEqual(first.results[0].code, "GB/T50011-2010（2024年版）")
        self.assertEqual(first.results[0].implement_date, "2024-08-01")
        self.assertEqual(first.results[0].publish_date, "2024-04-24")
        self.assertTrue(all(row.source == "ccsn" and row.fallback == "ccsn" for row in first.results))
        self.assertEqual(self.client._opener.open.call_count, 7)
        self.assertEqual([call.args[0].get_method() for call in self.client._opener.open.call_args_list],
                         ["GET"] * 6 + ["POST"])
        paths = list(self.directory.iterdir())
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].suffix, ".json")
        before = paths[0].read_bytes()
        # 新后端实例模拟进程重启；源码与上游都不是离线缓存命中的依赖。
        self.native.cache = DiskCurrencyCache(self.directory, clock=lambda: self.now)
        self.client._opener.open.side_effect = AssertionError("fresh cache 不该联网")
        fresh = self.native.query(CODE)
        self.assertTrue(fresh.from_cache)
        self.assertFalse(fresh.stale)
        self.assertEqual(fresh.results, first.results)
        self.assertEqual(fresh.fallback_source, "ccsn")
        # 三个上游都失败，原始备源风险与陈旧风险必须同时可见。
        self.now += 3 * 86400
        self.client._opener.open.side_effect = [down() for _ in range(12)]
        stale = self.native.query(CODE)
        self.assertTrue(stale.stale)
        self.assertEqual(stale.results, first.results)
        self.assertEqual(len(stale.critical_notes()), 2)
        self.assertEqual(paths[0].read_bytes(), before)
        # 合规拒绝不能被相同旧缓存救回，也不能刷新它的时间戳。
        self.client._opener.open.reset_mock()
        self.client._opener.open.side_effect = urllib.error.HTTPError(
            CSRES, 403, "Denied", {}, io.BytesIO("请输入验证码".encode("gbk")))
        with self.assertRaises(ComplianceDenied):
            self.native.query(CODE)
        self.client._opener.open.assert_called_once()
        self.assertEqual(paths[0].read_bytes(), before)
        self.assertIsNone(self.native.last_outcome)

    def test_primary_success_empty_stops_fallback_but_is_not_negative_cached(self):
        # ADR 0002：主源成功空仍是终局，不转备源。
        # W3/P1-4：但"没有总页数证据的空页"不是权威空结果，不许写 24h 负缓存。
        # 每次请求都给一个新响应对象：HttpClient 会关闭 resp，复用同一个会报 closed file。
        self.client._opener.open.side_effect = lambda *args, **kwargs: FakeResponse(b"<html></html>", url=CSRES)
        first = self.native.query(CODE)
        self.assertEqual(first.results, ())
        self.assertFalse(first.stale)
        self.assertFalse(first.complete)
        self.assertTrue(any("不完整" in note for note in first.critical_notes()))
        self.client._opener.open.assert_called_once()
        second = self.native.query(CODE)
        self.assertFalse(second.from_cache)
        self.assertEqual(second.results, ())
        self.assertEqual(self.client._opener.open.call_count, 2)
        self.assertFalse(self.directory.exists(), "不完整的空结果不得落盘")

    def test_too_old_disk_result_cannot_hide_whole_chain_failure(self):
        self.client._opener.open.side_effect = [down() for _ in range(6)] + [self.fallback_response()]
        self.native.query(CODE)
        self.now += DEFAULT_STALE_LIMIT + 1
        self.client._opener.open.side_effect = [down() for _ in range(12)]
        with self.assertRaises(SourceUnreachable):
            self.native.query(CODE)
        self.assertIsNone(self.native.last_outcome)

    def test_unconfirmed_shanghai_prefix_is_not_sent_to_general_db(self):
        with self.assertRaises(NoCurrencySource):
            self.native.query("DB31/373-2010")
        self.client._opener.open.assert_not_called()
        self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
