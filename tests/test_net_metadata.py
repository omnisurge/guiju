"""Metadata HTTP egress, throttle concurrency, HTML block rejection, and catalog shape tests."""

from __future__ import annotations

import contextlib
import http.client
import json
import threading
import unittest
import urllib.error
from unittest import mock

from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.net.metadata import MetadataClient
from guiju.net.policy import MetadataPolicy
from guiju.net.throttle import Throttle
from guiju.sources import mohurd, shzjw
from tests.net_harness import FakeResponse, FakeTransport, ShortRead

META_URL = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
SH_URL = "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWebApi/external/localStandard/list"


def _metadata_client(responses, *, attempts=3):
    return MetadataClient(
        policy=MetadataPolicy.default(),
        attempts=attempts,
        throttle=Throttle(0),
        transport=FakeTransport(responses),
    )


def _http_error(status: int, fp, *, url: str = META_URL, ctype: str = "text/html") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "blocked", {"Content-Type": ctype}, fp)


class _SyncDict(dict):
    """让两个线程都先读到旧值，再允许写；用来暴露"只锁写"的竞态。"""

    def __init__(self):
        super().__init__()
        self.armed = True
        self.barrier = threading.Barrier(2)

    def get(self, key, default=None):
        value = super().get(key, default)
        if self.armed:
            self.armed = False
            with contextlib.suppress(threading.BrokenBarrierError):
                self.barrier.wait(timeout=0.2)
        return value


class CurrencyThrottleTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.throttle = Throttle(min_interval=0.5, sleep=lambda s: self.sleeps.append(round(s, 3)))

    def test_serial_calls_wait(self):
        self.throttle.wait("www.csres.com")
        self.throttle.wait("www.csres.com")
        self.assertEqual(len(self.sleeps), 1)
        self.assertGreaterEqual(self.sleeps[0], 0.4)

    def test_concurrent_calls_are_serialized(self):
        self.throttle._last = _SyncDict()
        threads = [threading.Thread(target=self.throttle.wait, args=("www.csres.com",)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        # 有锁：第一个线程在 get 里等 0.2s 超时后写 last，第二个线程读到 last 后 sleep。
        # 无锁：两个线程都读到 None，sleeps 为空。
        self.assertTrue(self.sleeps, "并发限速没有生效：两个线程都判定无需等待")
        self.assertGreaterEqual(self.sleeps[0], 0.4)


class MetadataErrorBodyShortReadTest(unittest.TestCase):
    def test_metadata_short_read_marker_is_denied(self):
        fp = ShortRead([b"<html>", "验证码".encode()], headers={"Content-Type": "text/html"})
        err = _http_error(429, fp, url=META_URL)
        with self.assertRaises(ComplianceDenied):
            _metadata_client([err], attempts=1).post_form(META_URL, {"a": "1"})

    def test_metadata_incomplete_read_marker_is_denied(self):
        fp = ShortRead(
            [],
            headers={"Content-Type": "text/html"},
            error=http.client.IncompleteRead(b"<html>" + "验证码".encode(), 100),
        )
        err = _http_error(429, fp, url=META_URL)
        with self.assertRaises(ComplianceDenied):
            _metadata_client([err], attempts=1).post_form(META_URL, {"a": "1"})


class MetadataShortReadBodyTest(unittest.TestCase):
    def test_metadata_200_short_read_then_reset_marker_is_denied(self):
        fp = ShortRead(
            [b"<html>", "验证码".encode()],
            headers={"Content-Type": "text/html"},
            error=ConnectionResetError("reset"),
            url=META_URL,
        )
        with self.assertRaises(ComplianceDenied):
            _metadata_client([fp], attempts=1).post_form(META_URL, {"a": "1"})

    def test_metadata_200_short_read_without_marker_is_unreachable(self):
        fp = ShortRead(
            [b"<html>", b"maintenance"],
            headers={"Content-Type": "text/html"},
            error=ConnectionResetError("reset"),
            url=META_URL,
        )
        with self.assertRaises(SourceUnreachable):
            _metadata_client([fp], attempts=1).post_form(META_URL, {"a": "1"})


class MetadataJsonHtmlTest(unittest.TestCase):
    def _call(self, body, content_type):
        response = FakeResponse(
            body,
            url=SH_URL,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        return _metadata_client([response]).post_json(SH_URL, {"pageNo": 1})

    def test_html_shapes_are_denied(self):
        cases = (
            b"<body>x</body>",
            b"<div>x</div>",
            b'<?xml version="1.0"?><x/>',
            b"   \n\t<html><body>x</body></html>",
            b"\xef\xbb\xbf<html><body>x</body></html>",
        )
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ComplianceDenied):
                self._call(body, "application/json")

    def test_html_content_type_is_denied_even_for_json_like_body(self):
        with self.assertRaises(ComplianceDenied):
            self._call(b'{"ok": true}', "text/html")

    def test_normal_json_is_returned(self):
        self.assertEqual(self._call(b'{"ok": true}', "application/json"), b'{"ok": true}')


class PostJsonTest(unittest.TestCase):
    def test_post_json_html_body_is_compliance_denied(self) -> None:
        with self.assertRaises(ComplianceDenied) as denied_host:
            MetadataClient(throttle=Throttle(0)).post_json_dict(
                "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard", {"a": 1}
            )
        self.assertEqual(denied_host.exception.exit, 40)

        body = b"<html><body>403 Forbidden</body></html>"
        transport = FakeTransport(
            [
                FakeResponse(
                    body,
                    status=200,
                    url=shzjw.QUERY_URL,
                    headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
                )
            ]
        )
        client = MetadataClient(throttle=Throttle(0), attempts=1, transport=transport)
        with self.assertRaises(ComplianceDenied) as html_body:
            client.post_json_dict(shzjw.QUERY_URL, {"a": 1})
        self.assertEqual(html_body.exception.exit, 40)


class MohurdCatalogShapeTest(unittest.TestCase):
    def _call(self, payload_text):
        fake_client = mock.Mock()
        fake_client.get_page.return_value = mock.Mock(text=payload_text)
        with mock.patch.object(mohurd, "_body_client", return_value=fake_client):
            return mohurd.catalog_page(1, 100)

    def test_non_object_payload_is_needs_human(self):
        for text in ("[]", "null", '"x"', "1"):
            with self.subTest(text=text), self.assertRaises(NeedsHuman):
                self._call(text)

    def test_non_object_data_is_needs_human(self):
        for text in (
            '{"success": true, "data": []}',
            '{"success": true, "data": "x"}',
            '{"success": true, "data": 1}',
        ):
            with self.subTest(text=text), self.assertRaises(NeedsHuman):
                self._call(text)

    def test_non_string_fragment_is_needs_human(self):
        for fragment in (123, {"a": 1}, [1], None):
            text = json.dumps({"success": True, "data": {"html": fragment}})
            with self.subTest(fragment=fragment), self.assertRaises(NeedsHuman):
                self._call(text)

    def test_normal_payload_is_parsed(self):
        item = (
            '<li class="date">'
            '<a href="/gongkai/zc/wjk/art/2026/art_17339_1.html" title="标题">x</a>'
            '<span class="date-info">2026-01-01</span>'
            "</li>"
        )
        text = json.dumps({"success": True, "data": {"html": 'count="7"' + item}})
        items, total = self._call(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date"], "2026-01-01")
        self.assertEqual(total, 7)

    def test_empty_or_missing_fragment_keeps_unknown_total(self):
        for text in (
            '{"success": true, "data": {"html": ""}}',
            '{"success": true, "data": {}}',
            '{"success": true}',
        ):
            with self.subTest(text=text):
                self.assertEqual(self._call(text), ([], None))


if __name__ == "__main__":
    unittest.main()
