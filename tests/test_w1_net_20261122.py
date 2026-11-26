"""W1（1.2.0a4）网络/合规回归：短读 block、sidecar 续传、200 HTML、畸形 URL、mohurd 结构。"""

from __future__ import annotations

import http.client
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.net.body import META_CHECKPOINT_BYTES, _reload_part_state
from guiju.net.metadata import MetadataClient
from guiju.net.policy import BodyPolicy, MetadataPolicy
from guiju.net.redirect import RedirectGuard
from guiju.net.throttle import Throttle
from guiju.sources import mohurd
from tests.net_harness import FakeResponse, FakeTransport, body_client

BLOCK = "<html>验证码</html>".encode()
BODY_URL = "https://www.mohurd.gov.cn/gongkai/a.pdf"
META_URL = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
SH_URL = "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWebApi/external/localStandard/list"


class ShortRead:
    """Response-like object: controlled short reads, then an optional error."""

    def __init__(self, pieces=(), *, headers=None, error=None, url=BODY_URL, status=200):
        self.pieces = [bytes(piece) for piece in pieces]
        self.headers = dict(headers or {})
        self.error = error
        self._url = url
        self.status = status
        self.closed = False

    def read(self, size=-1):
        if self.pieces:
            piece = self.pieces.pop(0)
            if size is not None and 0 <= size < len(piece):
                self.pieces.insert(0, piece[size:])
                piece = piece[:size]
            return piece
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return b""

    def geturl(self):
        return self._url

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def metadata_client(responses, *, attempts=3):
    return MetadataClient(
        policy=MetadataPolicy.default(),
        attempts=attempts,
        throttle=Throttle(0),
        transport=FakeTransport(responses),
    )


def http_error(status, fp, *, url=BODY_URL, ctype="text/html"):
    return urllib.error.HTTPError(url, status, "blocked", {"Content-Type": ctype}, fp)


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
        transport = FakeTransport([http_error(403, fp)])
        with self.assertRaises(ComplianceDenied):
            body_client(transport).get_page(BODY_URL, attempts=1)
        self.assertEqual(len(transport.calls), 1)

    def test_http_error_incomplete_read_marker_is_denied(self):
        fp = ShortRead(
            [],
            headers={"Content-Type": "text/html"},
            error=http.client.IncompleteRead(b"<html>" + "验证码".encode(), 100),
        )
        transport = FakeTransport([http_error(403, fp)])
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


class MetadataErrorBodyShortReadTest(unittest.TestCase):
    def test_metadata_short_read_marker_is_denied(self):
        fp = ShortRead([b"<html>", "验证码".encode()], headers={"Content-Type": "text/html"})
        err = http_error(429, fp, url=META_URL)
        with self.assertRaises(ComplianceDenied):
            metadata_client([err], attempts=1).post_form(META_URL, {"a": "1"})

    def test_metadata_incomplete_read_marker_is_denied(self):
        fp = ShortRead(
            [],
            headers={"Content-Type": "text/html"},
            error=http.client.IncompleteRead(b"<html>" + "验证码".encode(), 100),
        )
        err = http_error(429, fp, url=META_URL)
        with self.assertRaises(ComplianceDenied):
            metadata_client([err], attempts=1).post_form(META_URL, {"a": "1"})


class MetadataShortReadBodyTest(unittest.TestCase):
    def test_metadata_200_short_read_then_reset_marker_is_denied(self):
        fp = ShortRead(
            [b"<html>", "验证码".encode()],
            headers={"Content-Type": "text/html"},
            error=ConnectionResetError("reset"),
            url=META_URL,
        )
        with self.assertRaises(ComplianceDenied):
            metadata_client([fp], attempts=1).post_form(META_URL, {"a": "1"})

    def test_metadata_200_short_read_without_marker_is_unreachable(self):
        fp = ShortRead(
            [b"<html>", b"maintenance"],
            headers={"Content-Type": "text/html"},
            error=ConnectionResetError("reset"),
            url=META_URL,
        )
        with self.assertRaises(SourceUnreachable):
            metadata_client([fp], attempts=1).post_form(META_URL, {"a": "1"})


class MetadataJsonHtmlTest(unittest.TestCase):
    def _call(self, body, content_type):
        response = FakeResponse(
            body,
            url=SH_URL,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        return metadata_client([response]).post_json(SH_URL, {"pageNo": 1})

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


class SidecarStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.part = Path(self.tmp.name) / "body.part"

    def _meta_path(self):
        return Path(str(self.part) + ".meta")

    def _seed(self, data, payload):
        self.part.write_bytes(data)
        self._meta_path().write_text(json.dumps(payload), encoding="utf-8")

    def test_missing_size_drops_part_and_meta(self):
        self._seed(b"12345", {"etag": '"v1"', "last_modified": ""})
        self.assertEqual(_reload_part_state(self.part), (0, {}))
        self.assertFalse(self.part.exists())
        self.assertFalse(self._meta_path().exists())

    def test_string_size_drops_part_and_meta(self):
        self._seed(b"12345", {"etag": '"v1"', "last_modified": "", "size": "5"})
        self.assertEqual(_reload_part_state(self.part), (0, {}))
        self.assertFalse(self.part.exists())

    def test_negative_and_boolean_sizes_drop_part_and_meta(self):
        for size in (-1, True):
            with self.subTest(size=size):
                self._seed(b"12345", {"etag": '"v1"', "last_modified": "", "size": size})
                self.assertEqual(_reload_part_state(self.part), (0, {}))
                self.assertFalse(self.part.exists())

    def test_size_below_part_truncates_instead_of_deleting(self):
        self._seed(b"12345", {"etag": '"v1"', "last_modified": "", "size": 3})
        part_size, sidecar = _reload_part_state(self.part)
        self.assertEqual(part_size, 3)
        self.assertEqual(sidecar["size"], 3)
        self.assertEqual(self.part.read_bytes(), b"123")

    def test_size_above_part_drops_part_and_meta(self):
        self._seed(b"12345", {"etag": '"v1"', "last_modified": "", "size": 7})
        self.assertEqual(_reload_part_state(self.part), (0, {}))
        self.assertFalse(self.part.exists())

    def test_matching_size_is_returned_unchanged(self):
        self._seed(b"12345", {"etag": '"v1"', "last_modified": "", "size": 5})
        part_size, sidecar = _reload_part_state(self.part)
        self.assertEqual(part_size, 5)
        self.assertEqual(sidecar["etag"], '"v1"')

    def test_checkpoint_lag_truncates_to_sidecar_size(self):
        self._seed(
            b"a" * (META_CHECKPOINT_BYTES + 500),
            {"etag": '"v1"', "last_modified": "", "size": META_CHECKPOINT_BYTES},
        )
        part_size, sidecar = _reload_part_state(self.part)
        self.assertEqual(part_size, META_CHECKPOINT_BYTES)
        self.assertEqual(sidecar["size"], META_CHECKPOINT_BYTES)
        self.assertEqual(self.part.stat().st_size, META_CHECKPOINT_BYTES)


class RangeAwareTransport(FakeTransport):
    def __init__(self, body, *, etag='"v1"'):
        super().__init__([])
        self.body = body
        self.etag = etag

    def open(self, req, timeout=0):
        self.calls.append(req)
        header = req.get_header("Range") or ""
        start = 0
        if header.startswith("bytes="):
            start = int(header[6:].split("-", 1)[0])
        if start:
            rest = self.body[start:]
            return FakeResponse(
                rest,
                status=206,
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Range": f"bytes {start}-{len(self.body) - 1}/{len(self.body)}",
                    "Content-Length": str(len(rest)),
                    "ETag": self.etag,
                },
            )
        return FakeResponse(
            self.body,
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": str(len(self.body)),
                "ETag": self.etag,
            },
        )


class ResumeAfterCheckpointTest(unittest.TestCase):
    def test_part_ahead_of_sidecar_resumes_from_checkpoint(self):
        body = b"%PDF-1.7\n" + b"x" * (4096 - 9)
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            part.write_bytes(body[:2048])
            Path(str(part) + ".meta").write_text(
                json.dumps({"etag": '"v1"', "last_modified": "", "size": 1024, "total": len(body)}),
                encoding="utf-8",
            )
            transport = RangeAwareTransport(body)
            result = body_client(transport).download(BODY_URL, part, attempts=1)
            self.assertEqual(result.resumed_from, 1024)
            self.assertEqual(result.bytes, len(body))
            self.assertEqual(part.read_bytes(), body)
            self.assertEqual(transport.calls[0].get_header("Range"), "bytes=1024-")


class MalformedUrlTest(unittest.TestCase):
    def test_invalid_ipv6_is_compliance_denied(self):
        with self.assertRaises(ComplianceDenied):
            BodyPolicy().check_url("https://[::1/gov.cn")

    def test_nfkc_invalid_netloc_is_compliance_denied(self):
        with self.assertRaises(ComplianceDenied):
            BodyPolicy().check_url("https://www.gov.cn\uff0f.evil.gov.cn/a")

    def test_normal_government_url_still_passes(self):
        self.assertEqual(BodyPolicy().check_url("https://www.gov.cn/a"), "https://www.gov.cn/a")


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
