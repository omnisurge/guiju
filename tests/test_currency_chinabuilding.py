"""国标电子书库图集时效适配器测试（内存 HTTPS transport + S0 脱敏夹具）。"""

from __future__ import annotations

import socket
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from guiju.currency.chinabuilding import ChinaBuildingCurrency
from guiju.currency.model import QueryCancelled, Record
from guiju.currency.native import NativeCurrency
from guiju.currency.registry import default_registry
from guiju.domain import atlas
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.net import metadata as net_metadata
from guiju.net.metadata import MetadataClient
from guiju.net.throttle import Throttle
from guiju.store.currency import DiskCurrencyCache
from tests.currency_helpers import real_response
from tests.net_harness import build_metadata_opener

FIXTURES = Path(__file__).parent / "fixtures" / "atlas"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _http_bytes(body: bytes, *, status: int = 200, reason: str = "OK", content_type: str = "text/html; charset=utf-8"):
    wire = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("ascii") + body
    return real_response(wire)


class _AtlasMemoryRouter:
    """按完整 HTTPS URL 分发内存 HTTPResponse，记录所有上游访问并严禁真实联网。"""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[str] = []

    def build_client(self, *, probes: list | None = None, attempts: int = 1) -> MetadataClient:
        router = self

        class MemoryHTTPS(urllib.request.HTTPSHandler):
            def https_open(self, req):
                router.calls.append(req.full_url)
                resp = router.handler(req.full_url)
                resp.geturl = lambda url=req.full_url: url
                return resp

        return MetadataClient(
            attempts=attempts,
            throttle=Throttle(0),
            transport=build_metadata_opener(MemoryHTTPS()),
            probe_sink=(probes.append if probes is not None else None),
        )


def _paginated_fixture(total: int, page: int) -> bytes:
    text = _fixture("search_22g101_1.html").decode("utf-8")
    card_start = text.find('<h4 class="search-tit')
    footer_start = text.find('<div id="pageEx"')
    single_card = text[card_start:footer_start]
    offset = (page - 1) * atlas.SEARCH_PAGE_SIZE
    count_on_page = max(0, min(atlas.SEARCH_PAGE_SIZE, total - offset))
    cards = "\n".join(
        single_card
        .replace("153493", str(200000 + offset + idx))
        .replace("22G101-1", f"22G101-{1 + offset + idx}" if (offset + idx) > 0 else "22G101-1")
        for idx in range(count_on_page)
    )
    html = text[:card_start].replace('"count":1', f'"count":{total}') + cards + text[footer_start:]
    return html.encode("utf-8")


class ChinaBuildingAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        mock.patch.object(net_metadata.time, "sleep").start()
        mock.patch("socket.create_connection", side_effect=AssertionError("禁止真实 socket")).start()
        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("禁止真实 socket")).start()
        self.addCleanup(mock.patch.stopall)

    def _standard_router(self, *, detail_61268: bytes | None = None) -> _AtlasMemoryRouter:
        def handle(url: str):
            parsed = urllib.parse.urlsplit(url)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/zbooklib/search":
                key = qs.get("SearchKey", [""])[0]
                if key == "22G101-1":
                    return _http_bytes(_fixture("search_22g101_1.html"))
                if key in ("16G101-1", "B16G101-1"):
                    return _http_bytes(_fixture("search_16g101_1.html"))
                if key in ("G101-1", "11G101-1"):
                    return _http_bytes(_fixture("search_g101_1.html"))
                if key == "99Z999":
                    return _http_bytes(_fixture("search_empty_99z999.html"))
                return _http_bytes(_fixture("search_empty_99z999.html"))
            if parsed.path == "/zbooklib/book/detail/show":
                bid = qs.get("bookID", [""])[0]
                if bid == "61268":
                    return _http_bytes(detail_61268 or _fixture("detail_61268_16g101_1.html"))
                if bid == "60070":
                    raw_en = (
                        _fixture("detail_61268_16g101_1.html")
                        .replace(b"61268", b"60070")
                        .replace(b"16G101-1", b"11G101-1 \xe8\x8b\xb1\xe6\x96\x87\xe7\x89\x88")
                        .replace(
                            b'<a style="font-weight: bold;" href="https://ebook.chinabuilding.com.cn/zbooklib/book/detail/show?SiteID=1&bookID=153493">22G101-1</a>',
                            b"-",
                        )
                    )
                    return _http_bytes(raw_en)
                if bid == "153493":
                    return _http_bytes(_fixture("detail_153493_22g101_1.html"))
            raise AssertionError(f"未预期 URL: {url}")

        return _AtlasMemoryRouter(handle)

    def test_exact_current_22g101_1_single_search_and_five_field_payload(self) -> None:
        probes = []
        router = self._standard_router()
        adapter = ChinaBuildingCurrency(router.build_client(probes=probes))
        with adapter:
            rows = adapter.lookup("22G101-1")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.code, "22G101-1")
        self.assertEqual(row.status, "current")
        self.assertEqual(row.replaced_by, "")
        self.assertEqual(row.implement_date, "2022-05-01")
        self.assertEqual(row.publish_date, "2022-05-01")
        self.assertEqual(row.source, "chinabuilding")
        self.assertEqual(set(row.as_payload()), {"code", "status", "name", "implement_date", "source"})
        self.assertEqual(len(router.calls), 1)
        self.assertTrue(adapter.complete)
        self.assertFalse(adapter.empty_authoritative)
        self.assertEqual([p.outcome for p in probes], ["ok"])

    def test_scheme_a_withdrawn_chases_detail_and_ignores_bilingual_trap(self) -> None:
        probes = []
        router = self._standard_router()
        adapter = ChinaBuildingCurrency(router.build_client(probes=probes))
        rows = adapter.lookup("16G101-1")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.code, "16G101-1")
        self.assertEqual(row.status, "superseded")
        self.assertEqual(row.replaced_by, "22G101-1")
        self.assertEqual(row.implement_date, "2016-09-01")
        self.assertEqual(row.publish_date, "2016-09-01")
        self.assertIn("abolish_date=2022-05-01", row.raw)
        self.assertEqual(row.as_payload()["replaced_by"], "22G101-1")
        self.assertEqual(len(router.calls), 2)
        self.assertIn("/zbooklib/search", router.calls[0])
        self.assertIn("/zbooklib/book/detail/show", router.calls[1])
        self.assertIn("bookID=61268", router.calls[1])
        self.assertEqual([p.outcome for p in probes], ["ok", "ok"])

    def test_withdrawn_without_replacement_stays_withdrawn(self) -> None:
        detail_no_rep = _fixture("detail_61268_16g101_1.html").replace(
            b'<a style="font-weight: bold;" href="https://ebook.chinabuilding.com.cn/zbooklib/book/detail/show?SiteID=1&bookID=153493">22G101-1</a>',
            b"-",
        )
        router = self._standard_router(detail_61268=detail_no_rep)
        rows = ChinaBuildingCurrency(router.build_client()).lookup("16G101-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "withdrawn")
        self.assertEqual(rows[0].replaced_by, "")
        self.assertNotIn("replaced_by", rows[0].as_payload())

    def test_bilingual_and_english_editions_isolated_strictly(self) -> None:
        router = self._standard_router()
        client = router.build_client()
        b_rows = ChinaBuildingCurrency(client).lookup("B16G101-1")
        self.assertEqual([(r.code, r.status) for r in b_rows], [("B16G101-1", "current")])
        self.assertEqual(len(router.calls), 1)

        router.calls.clear()
        en_rows = ChinaBuildingCurrency(client).lookup("11G101-1 英文版")
        self.assertEqual([(r.code, r.status) for r in en_rows], [("11G101-1 英文版", "withdrawn")])
        self.assertEqual(len(router.calls), 2)
        self.assertIn("SearchKey=11G101-1", router.calls[0])
        self.assertIn("bookID=60070", router.calls[1])

    def test_base_query_keeps_only_chinese_series_with_zero_detail_calls(self) -> None:
        router = self._standard_router()
        adapter = ChinaBuildingCurrency(router.build_client())
        rows = adapter.lookup("G101-1")
        self.assertEqual(
            [(r.code, r.status) for r in rows],
            [
                ("22G101-1", "current"),
                ("16G101-1", "withdrawn"),
                ("11G101-1", "withdrawn"),
                ("03G101-1", "withdrawn"),
            ],
        )
        self.assertEqual(len(router.calls), 1)
        self.assertTrue(adapter.complete)

    def test_authoritative_empty_marks_complete_and_empty_authoritative(self) -> None:
        router = self._standard_router()
        adapter = ChinaBuildingCurrency(router.build_client())
        rows = adapter.lookup("99Z999")
        self.assertEqual(rows, [])
        self.assertTrue(adapter.complete)
        self.assertTrue(adapter.empty_authoritative)
        self.assertIs(adapter.has_more, False)

    def test_pagination_multi_page_and_max_pages_truncation(self) -> None:
        def handle_two_pages(url: str):
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            page = int(qs.get("PageIndex", ["1"])[0])
            return _http_bytes(_paginated_fixture(total=25, page=page))

        router = _AtlasMemoryRouter(handle_two_pages)
        adapter = ChinaBuildingCurrency(router.build_client())
        rows = adapter.lookup("22G101-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "22G101-1")
        self.assertEqual(len(router.calls), 2)
        self.assertTrue(adapter.complete)
        self.assertFalse(adapter.truncated)

        # 超过 MAX_PAGES 上限应标记 truncated=True, complete=False 且不得写缓存。
        def handle_many_pages(url: str):
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            page = int(qs.get("PageIndex", ["1"])[0])
            return _http_bytes(_paginated_fixture(total=45, page=page))

        trunc_router = _AtlasMemoryRouter(handle_many_pages)
        with mock.patch.object(ChinaBuildingCurrency, "MAX_PAGES", 2):
            trunc_adapter = ChinaBuildingCurrency(trunc_router.build_client())
            trunc_rows = trunc_adapter.lookup("22G101-1")
        self.assertEqual(len(trunc_rows), 1)
        self.assertTrue(trunc_adapter.truncated)
        self.assertFalse(trunc_adapter.complete)
        self.assertIn("上限", trunc_adapter.incomplete_reason)

    def test_detail_mismatch_and_invalid_utf8_raise_needs_human_50(self) -> None:
        mismatched = _fixture("detail_61268_16g101_1.html").replace(b"16G101-1", b"22G101-1")
        router = self._standard_router(detail_61268=mismatched)
        with self.assertRaises(NeedsHuman) as ctx:
            ChinaBuildingCurrency(router.build_client()).lookup("16G101-1")
        self.assertEqual(ctx.exception.exit, 50)

        bad_utf8 = _AtlasMemoryRouter(lambda url: _http_bytes(b"\xff\xfe<broken"))
        with self.assertRaises(NeedsHuman) as ctx2:
            ChinaBuildingCurrency(bad_utf8.build_client()).lookup("22G101-1")
        self.assertEqual(ctx2.exception.exit, 50)

    def test_cancellation_stops_before_detail_and_unsupported_is_no_io(self) -> None:
        state = {"cancel": False}

        def handle_then_cancel(url: str):
            state["cancel"] = True
            return _http_bytes(_fixture("search_16g101_1.html"))

        router = _AtlasMemoryRouter(handle_then_cancel)
        with self.assertRaises(QueryCancelled):
            ChinaBuildingCurrency(router.build_client(), cancelled=lambda: state["cancel"]).lookup("16G101-1")
        self.assertEqual(len(router.calls), 1)

        noop_router = _AtlasMemoryRouter(lambda url: _http_bytes(b""))
        adapter = ChinaBuildingCurrency(noop_router.build_client())
        self.assertEqual(adapter.lookup("GB 55037-2022"), [])
        self.assertEqual(noop_router.calls, [])
        self.assertTrue(adapter.empty_authoritative)
        self.assertTrue(adapter.complete)

    def test_accepts_record_and_native_cache_stale_and_no_fallback_contracts(self) -> None:
        self.assertTrue(ChinaBuildingCurrency.accepts_record("16G101-1", Record("16G101-1", "superseded", source="chinabuilding", replaced_by="22G101-1")))
        for bad in (
            Record("B 16G101-1", "current", source="chinabuilding"),
            Record("22G101-1", "current", source="csres"),
            Record("16G101-1", "withdrawn", source="chinabuilding", fallback="csres"),
            Record("16G101-1", "unknown", source="chinabuilding"),
        ):
            self.assertFalse(ChinaBuildingCurrency.accepts_record("16G101-1", bad))
        self.assertFalse(ChinaBuildingCurrency.accepts_record("G101-1", Record("23G101-11", "current", source="chinabuilding")))

        with tempfile.TemporaryDirectory(prefix="guiju-atlas-cache-") as tmp:
            cache_dir = Path(tmp) / "cache"
            now = [1_800_000_000.0]
            mode = ["ok"]

            def handle(url: str):
                if mode[0] == "down":
                    return _http_bytes(b"down", status=503, reason="Service Unavailable")
                if mode[0] == "captcha":
                    return _http_bytes("<html>请输入验证码</html>".encode())
                if "/zbooklib/search" in url and "SearchKey=99Z999" in url:
                    return _http_bytes(_fixture("search_empty_99z999.html"))
                if "/zbooklib/search" in url:
                    return _http_bytes(_fixture("search_16g101_1.html"))
                if "/zbooklib/book/detail/show" in url:
                    return _http_bytes(_fixture("detail_61268_16g101_1.html"))
                raise AssertionError(url)

            router = _AtlasMemoryRouter(handle)
            client = router.build_client()
            registry = default_registry(client)
            cache = DiskCurrencyCache(cache_dir, clock=lambda: now[0])
            native = NativeCurrency(registry, cache, clock=lambda: now[0])

            # 1) 首次查询写入磁盘并重放 replaced_by，期间绝不触碰 csres/ccsn。
            with mock.patch("guiju.currency.csres.CsresCurrency.lookup", side_effect=AssertionError("禁止回落 csres")):
                first = native.query("16G101-1")
                self.assertEqual(first.source, "chinabuilding")
                self.assertEqual(first.results[0].replaced_by, "22G101-1")
                self.assertEqual(len(router.calls), 2)

                now[0] += 60
                restarted = NativeCurrency(registry, DiskCurrencyCache(cache_dir, clock=lambda: now[0]), clock=lambda: now[0])
                cached = restarted.query("16G101-1")
                self.assertTrue(cached.from_cache)
                self.assertEqual(cached.results[0].replaced_by, "22G101-1")
                self.assertEqual(len(router.calls), 2)

                # 2) 权威空写入负缓存。
                empty_first = restarted.query("99Z999")
                self.assertEqual(empty_first.results, ())
                self.assertTrue(empty_first.empty_authoritative)
                empty_cached = restarted.query("99Z999")
                self.assertTrue(empty_cached.from_cache)
                self.assertEqual(empty_cached.results, ())

                # 3) 超 TTL 且上游 503 → 回退 30 天内陈旧缓存；40 合规拦截与超 30 天均不得用旧缓存遮蔽。
                now[0] += 2 * 86400
                mode[0] = "down"
                stale_hit = restarted.query("16G101-1")
                self.assertTrue(stale_hit.stale)
                self.assertEqual(stale_hit.results[0].replaced_by, "22G101-1")

                mode[0] = "captcha"
                with self.assertRaises(ComplianceDenied):
                    restarted.query("16G101-1")

                mode[0] = "down"
                now[0] += 29 * 86400
                with self.assertRaises(SourceUnreachable):
                    restarted.query("16G101-1")


if __name__ == "__main__":
    unittest.main()
