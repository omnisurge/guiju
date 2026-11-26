"""Net body client, policy, redirect, short-read block detection, throttle, and download integrity tests."""

from __future__ import annotations

import http.client
import http.server
import itertools
import json
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

from guiju.domain import shzjw as shzjw_core
from guiju.errors import ComplianceDenied, NotIndexed, SourceUnreachable
from guiju.net.block import BlockDetector
from guiju.net.body import BodyClient
from guiju.net.headers import header_int, parse_content_range
from guiju.net.policy import BodyPolicy
from guiju.net.redirect import RedirectGuard
from guiju.net.request import DownloadResult
from guiju.net.throttle import Throttle, default_throttle
from guiju.sources import shzjw as shzjw_source
from tests.char_baseline_harness import LocalServer
from tests.net_harness import FakeResponse, FakeTransport, ShortRead, body_client

BODY_URL = "https://www.mohurd.gov.cn/gongkai/a.pdf"
PAYLOAD = b"%PDF-1.7\n" + b"0123456789" * 200


def _http_error(status: int, fp, *, url: str = BODY_URL, ctype: str = "text/html") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, status, "blocked", {"Content-Type": ctype}, fp)


class BodyPolicyShanghaiTest(unittest.TestCase):
    def test_shzjw_download_url_passes_real_body_policy(self) -> None:
        url = shzjw_core.download_url("fid-1")
        self.assertEqual(BodyPolicy().check_url(url), url)
        self.assertIn(":9175/", url)

    def test_shzjw_signed_download_url_passes_real_body_policy(self) -> None:
        with mock.patch.object(shzjw_source.time, "time", return_value=1_700_000_000.0):
            signed = shzjw_source._signed_download_url(shzjw_core.download_url("fid-1"))
        self.assertEqual(BodyPolicy().check_url(signed), signed)

    def test_body_policy_shanghai_port_is_host_scoped(self) -> None:
        accepted = (
            "https://bzjg.zjw.sh.gov.cn:9175/a",
            shzjw_core.download_url("fid-1"),
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(BodyPolicy().check_url(url), url)

        denied = (
            "http://bzjg.zjw.sh.gov.cn:9175/a",
            "https://bzjg.zjw.sh.gov.cn/a",
            "https://bzjg.zjw.sh.gov.cn:443/a",
            "https://www.gov.cn:9175/a",
            "https://other.gov.cn:9175/a",
            "https://www.gov.cn:8443/a",
            "https://user:pass@bzjg.zjw.sh.gov.cn:9175/a",
        )
        for url in denied:
            with self.subTest(url=url), self.assertRaises(ComplianceDenied):
                BodyPolicy().check_url(url)

    def test_body_policy_other_gov_host_default_ports_still_work(self) -> None:
        accepted = (
            "https://www.gov.cn/a",
            "http://www.gov.cn:80/a",
            "https://www.gov.cn:443/a",
            "https://gov.cn/a",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(BodyPolicy().check_url(url), url)


class MalformedUrlTest(unittest.TestCase):
    def test_invalid_ipv6_is_compliance_denied(self) -> None:
        with self.assertRaises(ComplianceDenied):
            BodyPolicy().check_url("https://[::1/gov.cn")

    def test_nfkc_invalid_netloc_is_compliance_denied(self):
        with self.assertRaises(ComplianceDenied):
            BodyPolicy().check_url("https://www.gov.cn\uff0f.evil.gov.cn/a")

    def test_normal_government_url_still_passes(self):
        self.assertEqual(BodyPolicy().check_url("https://www.gov.cn/a"), "https://www.gov.cn/a")


class SignedDownloadUrlTest(unittest.TestCase):
    PDF = b"%PDF-1.7\n" + b"x" * 32

    def test_helper_contains_timestamp_and_received_token(self) -> None:
        frozen = 1_700_000_000.123
        with mock.patch.object(shzjw_source.time, "time", return_value=frozen):
            signed = shzjw_source._signed_download_url(shzjw_core.download_url("fid-1"))
        ts = int(frozen * 1000)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(signed).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertEqual(query["timestamp"], [str(ts)])
        self.assertEqual(query["receivedToken"], [shzjw_source._token("fid-1", ts)])

    def test_download_sends_signed_url_and_referer(self) -> None:
        captured: dict[str, object] = {}
        fake_client = mock.Mock()

        def fake_download_bytes(url: str, *, referer=None, timeout=180):
            captured["url"] = url
            captured["referer"] = referer
            return self.PDF, "application/pdf"

        fake_client.download_bytes.side_effect = fake_download_bytes
        with mock.patch.object(shzjw_source, "_body_client", return_value=fake_client):
            body, magic = shzjw_source.download(shzjw_core.download_url("fid-1"), referer="https://ref.example/")
        self.assertEqual((body, magic), (self.PDF, "pdf"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(captured["url"])).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertIn("timestamp", query)
        self.assertIn("receivedToken", query)
        self.assertNotEqual(captured["url"], shzjw_core.download_url("fid-1"))
        self.assertEqual(captured["referer"], "https://ref.example/")

    def test_download_request_full_url_contains_query(self) -> None:
        transport = FakeTransport([FakeResponse(self.PDF)])
        client = BodyClient(
            policy=BodyPolicy(check_url_fn=lambda value: value),
            throttle=Throttle(0),
            transport=transport,
        )
        with mock.patch.object(shzjw_source, "_body_client", return_value=client):
            body, _ = shzjw_source.download(shzjw_core.download_url("fid-1"))
        self.assertEqual(body, self.PDF)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0].full_url).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertIn("timestamp", query)
        self.assertIn("receivedToken", query)

    def test_download_to_sends_signed_url(self) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            fake_client = mock.Mock()

            def fake_download(url: str, part_path: str, *, referer=None, timeout=180):
                captured["url"] = url
                Path(part_path).write_bytes(self.PDF)
                return DownloadResult(url=url, bytes=len(self.PDF), resumed_from=0, status=200)

            fake_client.download.side_effect = fake_download
            with mock.patch.object(shzjw_source, "_body_client", return_value=fake_client):
                result = shzjw_source.download_to(shzjw_core.download_url("fid-1"), str(part))
        self.assertEqual(result, (len(self.PDF), "pdf", 0))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(captured["url"])).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertIn("timestamp", query)
        self.assertIn("receivedToken", query)

    def test_download_to_request_full_url_contains_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            transport = FakeTransport([FakeResponse(self.PDF)])
            client = BodyClient(
                policy=BodyPolicy(check_url_fn=lambda value: value),
                throttle=Throttle(0),
                transport=transport,
            )
            with mock.patch.object(shzjw_source, "_body_client", return_value=client):
                result = shzjw_source.download_to(shzjw_core.download_url("fid-1"), str(part))
        self.assertEqual(result, (len(self.PDF), "pdf", 0))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0].full_url).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertIn("timestamp", query)
        self.assertIn("receivedToken", query)

    def test_both_entry_points_share_frozen_url(self) -> None:
        frozen = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(shzjw_source.time, "time", return_value=frozen):
            part = Path(tmp) / "body.part"
            captured: list[str] = []
            fake_client = mock.Mock()

            def fake_download_bytes(url: str, *, referer=None, timeout=180):
                captured.append(url)
                return self.PDF, "application/pdf"

            def fake_download(url: str, part_path: str, *, referer=None, timeout=180):
                captured.append(url)
                Path(part_path).write_bytes(self.PDF)
                return DownloadResult(url=url, bytes=len(self.PDF), resumed_from=0, status=200)

            fake_client.download_bytes.side_effect = fake_download_bytes
            fake_client.download.side_effect = fake_download
            with mock.patch.object(shzjw_source, "_body_client", return_value=fake_client):
                shzjw_source.download(shzjw_core.download_url("fid-1"))
                shzjw_source.download_to(shzjw_core.download_url("fid-1"), str(part))
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0], captured[1])

    def test_missing_file_id_is_not_signed(self) -> None:
        with self.assertRaises(NotIndexed):
            shzjw_source._signed_download_url("https://bzjg.zjw.sh.gov.cn:9175/a")

    def test_missing_file_id_both_entry_points_do_not_request(self) -> None:
        raw = "https://bzjg.zjw.sh.gov.cn:9175/a"
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(shzjw_source, "_body_client") as mocked_client,
        ):
            with self.assertRaises(NotIndexed):
                shzjw_source.download(raw)
            with self.assertRaises(NotIndexed):
                shzjw_source.download_to(raw, str(Path(tmp) / "a.part"))
        mocked_client.assert_not_called()


class GetPageShortReadTest(unittest.TestCase):
    def test_marker_in_second_short_read_is_compliance_denied(self):
        head = b"<html><hea"
        tail = b"d>" + "验证码".encode() + b"</html>"
        response = ShortRead(
            [head, tail],
            headers={"Content-Type": "text/html", "Content-Length": str(len(head + tail))},
        )
        with self.assertRaises(ComplianceDenied):
            body_client(FakeTransport([response])).get_page(BODY_URL, attempts=1)

    def test_incomplete_read_partial_with_marker_is_compliance_denied(self):
        partial = b"<html>" + "验证码".encode()
        response = ShortRead(
            [],
            headers={"Content-Type": "text/html"},
            error=http.client.IncompleteRead(partial, 100),
        )
        with self.assertRaises(ComplianceDenied):
            body_client(FakeTransport([response])).get_page(BODY_URL, attempts=1)

    def test_incomplete_read_without_marker_stays_unreachable(self):
        response = ShortRead(
            [],
            headers={"Content-Type": "text/html"},
            error=http.client.IncompleteRead(b"<html>maintenance", 100),
        )
        with self.assertRaises(SourceUnreachable):
            body_client(FakeTransport([response])).get_page(BODY_URL, attempts=1)

    def test_connection_reset_without_marker_stays_unreachable(self):
        response = ShortRead(
            [b"<html>ma"],
            headers={"Content-Type": "text/html"},
            error=ConnectionResetError("reset"),
        )
        with self.assertRaises(SourceUnreachable):
            body_client(FakeTransport([response])).get_page(BODY_URL, attempts=1)

    def test_long_body_without_marker_is_unchanged(self):
        body = b"x" * 200_000
        response = ShortRead(
            [body[:1000], body[1000:65536], body[65536:]],
            headers={"Content-Type": "application/pdf", "Content-Length": str(len(body))},
        )
        result = body_client(FakeTransport([response])).get_page(BODY_URL, attempts=1)
        self.assertEqual(result.body, body)
        self.assertEqual(result.probe.bytes, len(body))

    def test_marker_after_the_64kib_scan_window_is_ignored(self):
        body = b"x" * 70_000 + "验证码".encode()
        response = ShortRead(
            [body],
            headers={"Content-Type": "text/plain", "Content-Length": str(len(body))},
        )
        result = body_client(FakeTransport([response])).get_page(BODY_URL, attempts=1)
        self.assertEqual(len(result.body), len(body))


class WriteResponseShortReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.part = Path(self.tmp.name) / "body.part"

    def test_block_marker_after_short_first_chunk_is_denied_and_cleaned(self):
        head = b"<html><hea"
        tail = b"d>" + "验证码".encode() + b"</html>"
        response = ShortRead(
            [head, tail],
            headers={
                "Content-Type": "text/html",
                "Content-Length": str(len(head + tail)),
                "ETag": '"v1"',
            },
        )
        with self.assertRaises(ComplianceDenied):
            body_client(FakeTransport([response])).download(BODY_URL, self.part, attempts=1)
        self.assertFalse(self.part.exists())
        self.assertFalse(Path(str(self.part) + ".meta").exists())

    def test_incomplete_read_partial_is_kept_and_resumed(self):
        body = b"%PDF-" + b"x" * 95
        first = ShortRead(
            [b"%PDF-"],
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(len(body)),
                "ETag": '"v1"',
            },
            error=http.client.IncompleteRead(b"x" * 45, len(body)),
        )
        rest = body[50:]
        second = FakeResponse(
            rest,
            status=206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Range": f"bytes 50-{len(body) - 1}/{len(body)}",
                "Content-Length": str(len(rest)),
                "ETag": '"v1"',
            },
        )
        transport = FakeTransport([first, second])
        result = body_client(transport).download(BODY_URL, self.part, attempts=2)
        self.assertEqual(result.bytes, len(body))
        self.assertEqual(result.resumed_from, 50)
        self.assertEqual(transport.calls[1].get_header("Range"), "bytes=50-")
        self.assertEqual(self.part.read_bytes(), body)

    def test_connection_reset_without_validator_stays_unreachable(self):
        response = ShortRead(
            [b"%PDF-"],
            headers={"Content-Type": "application/pdf", "Content-Length": "100"},
            error=ConnectionResetError("reset"),
        )
        with self.assertRaises(SourceUnreachable):
            body_client(FakeTransport([response])).download(BODY_URL, self.part, attempts=1)

    def test_block_during_resume_restores_the_validated_prefix(self):
        validated = b"%PDF-" + b"x" * 45
        self.part.write_bytes(validated)
        Path(str(self.part) + ".meta").write_text(
            json.dumps({"etag": '"v1"', "last_modified": "", "size": 50, "total": 100}),
            encoding="utf-8",
        )
        response = ShortRead(
            [b"y" * 10, b"<html>" + "验证码".encode()],
            headers={
                "Content-Type": "text/html",
                "Content-Range": "bytes 50-99/100",
                "Content-Length": "50",
                "ETag": '"v1"',
            },
            url=BODY_URL,
            status=206,
        )
        with self.assertRaises(ComplianceDenied):
            body_client(FakeTransport([response])).download(BODY_URL, self.part, attempts=1)
        self.assertEqual(self.part.read_bytes(), validated)
        meta = json.loads(Path(str(self.part) + ".meta").read_text("utf-8"))
        self.assertEqual(meta["size"], 50)


class ErrorBodyShortReadTest(unittest.TestCase):
    def test_http_error_short_read_marker_is_denied(self):
        fp = ShortRead([b"<html>", "验证码".encode()], headers={"Content-Type": "text/html"})
        transport = FakeTransport([_http_error(403, fp)])
        with self.assertRaises(ComplianceDenied):
            body_client(transport).get_page(BODY_URL, attempts=1)
        self.assertEqual(len(transport.calls), 1)

    def test_http_error_incomplete_read_marker_is_denied(self):
        fp = ShortRead(
            [],
            headers={"Content-Type": "text/html"},
            error=http.client.IncompleteRead(b"<html>" + "验证码".encode(), 100),
        )
        transport = FakeTransport([_http_error(403, fp)])
        with self.assertRaises(ComplianceDenied):
            body_client(transport).get_page(BODY_URL, attempts=1)
        self.assertEqual(len(transport.calls), 1)


class RedirectShortReadTest(unittest.TestCase):
    def _headers(self):
        headers = Message()
        headers["Location"] = "https://www.mohurd.gov.cn/b"
        headers["Content-Type"] = "text/html"
        return headers

    def _guard(self):
        return RedirectGuard(BodyPolicy(check_url_fn=lambda url: url), Throttle(0))

    def test_redirect_short_read_marker_is_denied(self):
        request = urllib.request.Request("https://www.mohurd.gov.cn/a")
        fp = ShortRead([b"<html>", "验证码".encode()], headers={"Content-Type": "text/html"})
        with self.assertRaises(ComplianceDenied):
            self._guard().http_error_302(request, fp, 302, "Found", self._headers())

    def test_redirect_incomplete_read_marker_is_denied(self):
        request = urllib.request.Request("https://www.mohurd.gov.cn/a")
        fp = ShortRead(
            [],
            headers={"Content-Type": "text/html"},
            error=http.client.IncompleteRead(b"<html>" + "验证码".encode(), 100),
        )
        with self.assertRaises(ComplianceDenied):
            self._guard().http_error_302(request, fp, 302, "Found", self._headers())


class ThrottleTest(unittest.TestCase):
    def test_throttle_lock_exists(self):
        """节流读-睡-写必须由锁互斥（并发绕过修复的存在性验证）。"""
        self.assertIsInstance(default_throttle()._lock, type(threading.Lock()))

    def test_concurrent_throttle_same_host_serializes(self):
        """同 host 并发请求必须串行间隔 >= 阈值，不能齐刷刷同刻通过。"""
        interval = 0.05
        throttle = Throttle(min_interval=interval)
        stamps = []
        gate = threading.Barrier(5)

        def worker():
            gate.wait()
            throttle.wait("parallel.gov.cn")
            stamps.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stamps.sort()
        gaps = [b - a for a, b in itertools.pairwise(stamps)]
        self.assertTrue(
            all(g >= interval * 0.8 for g in gaps),
            "并发同 host 的节流间隔应互斥展开，实测 gaps=" + repr(gaps),
        )

    def test_redirect_target_host_is_throttled(self):
        """白名单过关后，重定向目标 host 也必须先过 Throttle.wait 再放行。"""
        req = urllib.request.Request("https://a.gov.cn/x")
        throttle = mock.Mock()
        handler = RedirectGuard(BodyPolicy(), throttle, BlockDetector())
        handler.redirect_request(
            req, None, 302, "Found", {}, "https://b.gov.cn/y"
        )
        throttle.wait.assert_called_once_with("b.gov.cn")

    def test_redirect_same_host_is_throttled(self):
        """同 host 重定向照样再计一拍（限速按请求数，不按 host 去重）。"""
        req = urllib.request.Request("https://a.gov.cn/x")
        throttle = mock.Mock()
        handler = RedirectGuard(BodyPolicy(), throttle, BlockDetector())
        handler.redirect_request(
            req, None, 302, "Found", {}, "/x2"
        )
        throttle.wait.assert_called_once_with("a.gov.cn")


class RangeHandler(http.server.BaseHTTPRequestHandler):
    mode = "truncated_200"
    etag = '"a1-wrong-start-v1"'
    last_modified = "Wed, 01 Jan 2025 00:00:00 GMT"

    def _range_start(self):
        match = re.match(r"bytes=(\d+)-", self.headers.get("Range") or "")
        return int(match.group(1)) if match else 0

    def _body(self, count=100):
        start = self._range_start()
        return PAYLOAD[start:start + count]

    def do_GET(self):
        if self.mode == "truncated_200":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD[:100])
            self.close_connection = True
            return
        if self.mode == "missing_content_range":
            self.send_response(206)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(self._body(100))
            return
        if self.mode == "wrong_start":
            self.send_response(206)
            self.send_header("Content-Range", "bytes 0-99/2000")
            self.send_header("Content-Length", "100")
            self.send_header("ETag", self.etag)
            self.send_header("Last-Modified", self.last_modified)
            self.end_headers()
            self.wfile.write(PAYLOAD[:100])
            return
        if self.mode == "truncated_206":
            self.send_response(206)
            self.send_header("Content-Range", "bytes 0-199/2000")
            self.send_header("Content-Length", "200")
            self.end_headers()
            self.wfile.write(PAYLOAD[:100])
            self.close_connection = True
            return
        if self.mode == "partial_known_total":
            self.send_response(206)
            self.send_header("Content-Range", "bytes 0-999/2009")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(PAYLOAD[:1000])
            return
        if self.mode == "body_longer_than_range":
            self.send_response(206)
            self.send_header("Content-Range", "bytes 0-99/200")
            self.send_header("Content-Length", "200")
            self.end_headers()
            self.wfile.write(PAYLOAD[:200])
            return
        if self.mode == "unknown_total":
            start = self._range_start()
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{start + 99}/*")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(self._body(100))
            return
        if self.mode == "uppercase_unit":
            self.send_response(206)
            self.send_header("Content-Range", "Bytes 0-99/100")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(PAYLOAD[:100])
            return
        if self.mode == "416_head_405":
            self.send_response(416)
            self.end_headers()
            return
        raise AssertionError(f"unknown mode {self.mode}")

    def do_HEAD(self):
        self.send_response(405)
        self.end_headers()

    def log_message(self, *_args):
        return


class DownloadIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        RangeHandler.mode = "truncated_200"
        self.addCleanup(setattr, RangeHandler, "mode", "truncated_200")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/file.pdf"
        self.client = BodyClient(
            policy=BodyPolicy(check_url_fn=lambda url: url),
            throttle=Throttle(0),
        )

    def test_truncated_body_raises_and_keeps_partial(self):
        part = self.dir / "broken.part"
        with self.assertRaises(SourceUnreachable) as ctx:
            self.client.download(self.url, part, attempts=2)
        self.assertIn("截断", str(ctx.exception))
        self.assertEqual(part.stat().st_size, 100)
        self.assertFalse((self.dir / "broken.pdf").exists())

    def test_206_without_content_range_is_rejected(self):
        RangeHandler.mode = "missing_content_range"
        part = self.dir / "no-range.part"
        with self.assertRaises(SourceUnreachable) as ctx:
            self.client.download(self.url, part, attempts=2)
        self.assertIn("Content-Range", str(ctx.exception))
        self.assertFalse(part.exists())

    def test_206_wrong_start_is_rejected_and_keeps_part(self):
        RangeHandler.mode = "wrong_start"
        part = self.dir / "wrong-start.part"
        part.write_bytes(PAYLOAD[:100])
        (self.dir / "wrong-start.part.meta").write_text(
            json.dumps({"etag": '"a1-wrong-start-v1"',
                        "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                        "total": 2000, "size": 100}), encoding="utf-8")
        with self.assertRaises(SourceUnreachable) as ctx:
            self.client.download(self.url, part, attempts=2)
        self.assertIn("起点", str(ctx.exception))
        self.assertEqual(part.stat().st_size, 100)

    def test_206_truncated_segment_is_rejected(self):
        RangeHandler.mode = "truncated_206"
        part = self.dir / "short-206.part"
        with self.assertRaises(SourceUnreachable):
            self.client.download(self.url, part, attempts=2)
        self.assertLess(part.stat().st_size, len(PAYLOAD))

    def test_206_unknown_total_is_rejected(self):
        RangeHandler.mode = "unknown_total"
        part = self.dir / "unknown-total.part"
        with self.assertRaises(SourceUnreachable) as ctx:
            self.client.download(self.url, part, attempts=2)
        self.assertIn("总长", str(ctx.exception))
        self.assertFalse(part.exists())

    def test_206_partial_known_total_is_rejected(self):
        RangeHandler.mode = "partial_known_total"
        part = self.dir / "partial-total.part"
        with self.assertRaises(SourceUnreachable):
            self.client.download(self.url, part, attempts=2)

    def test_206_body_longer_than_declared_range_is_rejected(self):
        RangeHandler.mode = "body_longer_than_range"
        part = self.dir / "longer-body.part"
        with self.assertRaises(SourceUnreachable):
            self.client.download(self.url, part, attempts=2)

    def test_206_uppercase_unit_is_accepted(self):
        RangeHandler.mode = "uppercase_unit"
        part = self.dir / "uppercase.part"
        result = self.client.download(self.url, part, attempts=2)
        self.assertEqual(result.bytes, 100)
        self.assertEqual(part.stat().st_size, 100)

    def test_416_without_head_length_does_not_accept_part(self):
        RangeHandler.mode = "416_head_405"
        part = self.dir / "stale.part"
        part.write_bytes(b"%PDF-")
        with self.assertRaises(SourceUnreachable):
            self.client.download(self.url, part, attempts=2)
        self.assertFalse(part.exists())

    def test_header_parsers_are_defensive(self):
        self.assertIsNone(header_int("²"))
        self.assertIsNone(header_int("9" * 5000))
        self.assertEqual(
            parse_content_range("bytes 0-1/" + "9" * 5000),
            (None, None, None),
        )
        self.assertEqual(
            parse_content_range("Bytes 0-99/100"),
            (0, 99, 100),
        )


class P2RangeBoundsTest(unittest.TestCase):
    def _client(self, response):
        transport = FakeTransport([response])
        return BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport), transport

    def test_range_end_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            part.write_bytes(b"x" * 500)
            part.with_name(part.name + ".meta").write_text(json.dumps({
                "etag": '"v1"', "last_modified": "", "total": 1000, "size": 500,
            }), encoding="utf-8")
            response = FakeResponse(
                b"", status=206,
                headers={"Content-Range": "bytes 500-499/1000", "Content-Length": "0"},
            )
            client, _ = self._client(response)
            with self.assertRaises(SourceUnreachable):
                client.download("https://x.gov.cn/a", part, attempts=1)
            self.assertEqual(part.read_bytes(), b"x" * 500)

    def test_range_end_equals_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            response = FakeResponse(
                b"", status=206,
                headers={"Content-Range": "bytes 0-1000/1000", "Content-Length": "1001"},
            )
            client, _ = self._client(response)
            with self.assertRaises(SourceUnreachable):
                client.download("https://x.gov.cn/a", part, attempts=1)
            self.assertFalse(part.exists())


class P0TruncationNegativeTest(unittest.TestCase):
    def test_range_invalid_bounds_not_retried(self) -> None:
        transport = FakeTransport([
            FakeResponse(b"", status=206, headers={"Content-Range": "bytes 500-499/1000", "Content-Length": "0"})
        ])
        client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SourceUnreachable):
            client.download("https://x.gov.cn/a", Path(tmp) / "a.part", attempts=2)
        self.assertEqual(len(transport.calls), 1)

    def test_range_bad_validator_not_truncation_retry(self) -> None:
        body = b"%PDF-1.7\n" + b"x" * (1000 - 9)
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            part.write_bytes(b"x" * 500)
            part.with_name(part.name + ".meta").write_text(json.dumps({
                "etag": '"v1"', "last_modified": "", "total": 1000, "size": 500,
            }), encoding="utf-8")
            first = FakeResponse(
                b"x" * 500,
                status=206,
                headers={"Content-Range": "bytes 500-999/1000", "Content-Length": "500", "ETag": '"v2"'},
            )
            transport = FakeTransport([first, FakeResponse(body, status=200)])
            client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
            result = client.download("https://x.gov.cn/a", part, attempts=2)
        self.assertEqual((result.status, result.bytes, result.resumed_from), (200, 1000, 0))
        self.assertEqual(len(transport.calls), 2)

    def test_truncated_without_validator_not_retried(self) -> None:
        transport = FakeTransport([
            FakeResponse(b"x" * 50, status=200, headers={"Content-Length": "100"})
        ])
        client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SourceUnreachable):
            client.download("https://x.gov.cn/a", Path(tmp) / "a.part", attempts=2)
        self.assertEqual(len(transport.calls), 1)


class P2SidecarWriteTest(unittest.TestCase):
    def test_sidecar_write_amplification_bounded(self) -> None:
        import guiju.net.body as body_mod

        original = body_mod._write_meta
        sizes = []

        def record(part, **kwargs):
            sizes.append(kwargs["size"])
            return original(part, **kwargs)

        body = b"%PDF-1.7\n" + b"x" * (3 * 1024 * 1024 - 9)
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0))
            with mock.patch.object(body_mod, "_write_meta", side_effect=record), LocalServer(
                "ok", body=body
            ) as server:
                result = client.download(server.url + "/a", part, attempts=1, chunk_size=64 * 1024)
        self.assertEqual(result.bytes, len(body))
        self.assertLessEqual(len(sizes), 8, sizes)
        self.assertIn(64 * 1024, sizes)
        self.assertIn(len(body), sizes)
        self.assertFalse(Path(str(part) + ".meta").exists())


if __name__ == "__main__":
    unittest.main()
