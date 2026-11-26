"""断点续传、416 校验与 sidecar 恢复回归测试。"""

from __future__ import annotations

import http.server
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

from guiju.errors import ComplianceDenied, SourceUnreachable
from guiju.net.block import BLOCK_MARKERS
from guiju.net.body import META_CHECKPOINT_BYTES, BodyClient, _reload_part_state
from guiju.net.policy import BodyPolicy
from guiju.net.request import ProbeInfo
from guiju.net.throttle import Throttle
from tests.net_harness import FakeResponse, FakeTransport, body_client

BODY_URL = "https://www.mohurd.gov.cn/gongkai/a.pdf"
PAYLOAD = b"%PDF-1.7\n" + b"0123456789" * 200
MARKER = BLOCK_MARKERS[0]


class Handler(http.server.BaseHTTPRequestHandler):
    support_range = True
    as_html = False
    etag = '"resume-v1"'
    last_modified = "Wed, 01 Jan 2025 00:00:00 GMT"

    def do_GET(self):
        if self.as_html:
            body = f"<html><body>{MARKER}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        data = PAYLOAD
        start = 0
        rng = self.headers.get("Range")
        if self.support_range and rng and rng.startswith("bytes="):
            start = int(rng.split("=", 1)[1].split("-", 1)[0])
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            self.send_header("ETag", self.etag)
            self.send_header("Last-Modified", self.last_modified)
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # 静音
        return


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/file.pdf"
        self.client = BodyClient(
            policy=BodyPolicy(check_url_fn=lambda url: url),
            throttle=Throttle(0),
        )

    def tearDown(self):
        Handler.support_range = True
        Handler.as_html = False

    def test_range_resume_appends(self):
        part = self.dir / "a.part"
        part.write_bytes(PAYLOAD[:500])
        (self.dir / "a.part.meta").write_text(
            json.dumps({"etag": '"resume-v1"',
                        "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                        "total": len(PAYLOAD), "size": 500}), encoding="utf-8")
        info = self.client.download(self.url, part)
        self.assertEqual(info.resumed_from, 500)
        self.assertEqual(info.bytes, len(PAYLOAD))
        self.assertEqual(part.read_bytes(), PAYLOAD)

    def test_server_without_range_restarts(self):
        Handler.support_range = False
        part = self.dir / "b.part"
        part.write_bytes(PAYLOAD[:500])
        info = self.client.download(self.url, part)
        self.assertEqual(info.bytes, len(PAYLOAD))
        self.assertEqual(part.read_bytes(), PAYLOAD)

    def test_html_page_is_denied(self):
        Handler.as_html = True
        part = self.dir / "c.part"
        with self.assertRaises(ComplianceDenied):
            self.client.download(self.url, part)
        self.assertFalse(part.exists() and part.stat().st_size > 0)

    def test_fresh_download_from_zero(self):
        part = self.dir / "d.part"
        info = self.client.download(self.url, part)
        self.assertEqual(info.resumed_from, 0)
        self.assertEqual(part.read_bytes(), PAYLOAD)


class _Resp:
    def __init__(self, status=200, body=b"data", ctype="application/pdf"):
        self.status = status
        self.headers = {"Content-Type": ctype, "Content-Length": str(len(body))}
        self._body = body
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        if self._pos >= len(self._body):
            return b""
        chunk = self._body[self._pos : self._pos + (n if n > 0 else len(self._body))]
        self._pos += len(chunk)
        return chunk


def _http416() -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x.gov.cn/f.pdf", 416, "Range", {}, None)


class _CallbackTransport:
    def __init__(self, open_side, probe_info):
        self.open_side = open_side
        self.probe_info = probe_info
        self.probe_calls = []

    def open(self, req, timeout=0):
        return self.open_side(req, timeout)

    def probe(self, url, *, timeout=15, referer=None):
        self.probe_calls.append({"url": url, "timeout": timeout, "referer": referer})
        return self.probe_info


def _write_meta(part: Path, *, etag='"v1"', total=5, size=5):
    part.with_name(part.name + ".meta").write_text(
        json.dumps({"etag": etag, "last_modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                    "total": total, "size": size}), encoding="utf-8")


class Download416Test(unittest.TestCase):
    def _client(self, open_side, probe_info):
        return BodyClient(
            policy=BodyPolicy(check_url_fn=lambda url: url),
            throttle=Throttle(min_interval=0),
            transport=_CallbackTransport(open_side, probe_info),
        )

    def test_416_accepts_when_remote_size_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "x.part"
            part.write_bytes(b"12345")
            _write_meta(part, etag='"v1"', total=5, size=5)
            calls = []

            def open_side(req, timeout=0):
                calls.append(req.headers.get("Range"))
                raise _http416()

            result = self._client(open_side, ProbeInfo(content_length=5, etag='"v1"')).download(
                "https://x.gov.cn/f.pdf", part, timeout=10)
            self.assertEqual(result.bytes, 5)
            self.assertEqual(result.resumed_from, 5)
            self.assertEqual(part.read_bytes(), b"12345")
            self.assertEqual(calls, ["bytes=5-"])

    def test_416_restarts_when_remote_size_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "x.part"
            part.write_bytes(b"corrupted-longer-than-remote")
            _write_meta(part, etag='"v1"', total=5, size=28)
            calls = []

            def open_side(req, timeout=0):
                calls.append(req.headers.get("Range"))
                if req.headers.get("Range"):
                    raise _http416()
                return _Resp(status=200, body=b"fresh")

            result = self._client(open_side, ProbeInfo(content_length=5, etag='"v1"')).download(
                "https://x.gov.cn/f.pdf", part, timeout=10)
            self.assertEqual(result.bytes, 5)
            self.assertEqual(part.read_bytes(), b"fresh")
            self.assertEqual(calls[0], "bytes=28-")
            self.assertIsNone(calls[1])

    def test_416_rejects_when_head_answers_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "x.part"
            part.write_bytes(b"done")
            _write_meta(part, etag='"v1"', total=4, size=4)

            def open_side(req, timeout=0):
                raise _http416()

            with self.assertRaises(SourceUnreachable):
                self._client(open_side, ProbeInfo()).download("https://x.gov.cn/f.pdf", part, timeout=10)
            self.assertFalse(part.exists())


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


class P2AttemptsRestartTest(unittest.TestCase):
    def _seed(self, part: Path, size: int = 500) -> None:
        part.write_bytes(b"x" * size)
        part.with_name(part.name + ".meta").write_text(json.dumps({
            "etag": '"v1"', "last_modified": "", "total": 1000, "size": size,
        }), encoding="utf-8")

    def test_attempts_one_restarts_on_206_validator_mismatch(self) -> None:
        body = b"%PDF-1.7\n" + b"x" * (1000 - 9)
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            self._seed(part)
            first = FakeResponse(
                b"x" * 500,
                status=206,
                headers={"Content-Type": "application/pdf", "Content-Range": "bytes 500-999/1000",
                         "Content-Length": "500", "ETag": '"v2"'},
            )
            second = FakeResponse(body, status=200)
            transport = FakeTransport([first, second])
            client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
            result = client.download("https://x.gov.cn/a", part, attempts=1)
        self.assertEqual((result.status, result.bytes, result.resumed_from), (200, 1000, 0))
        self.assertEqual(len(transport.calls), 2)
        self.assertIsNotNone(transport.calls[0].get_header("Range"))
        self.assertIsNone(transport.calls[1].get_header("Range"))

    def test_attempts_one_restarts_on_416_validator_mismatch(self) -> None:
        body = b"%PDF-1.7\n" + b"x" * (1000 - 9)
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            self._seed(part)
            first = FakeResponse(b"", status=416, headers={"Content-Length": "0"})
            second = FakeResponse(body, status=200)
            transport = FakeTransport(
                [first, second],
                probe_info=ProbeInfo(content_length=999, etag='"v2"'),
            )
            client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
            result = client.download("https://x.gov.cn/a", part, attempts=1, referer="https://ref.example/")
        self.assertEqual((result.status, result.bytes, result.resumed_from), (200, 1000, 0))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.probe_calls[0]["referer"], "https://ref.example/")
        self.assertIsNone(transport.calls[1].get_header("Range"))


if __name__ == "__main__":
    unittest.main()
