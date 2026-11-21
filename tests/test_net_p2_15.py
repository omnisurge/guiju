"""P2-15 sidecar / If-Range / 416 focused tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from guiju.net.request import ProbeInfo
from tests.net_harness import FakeResponse, FakeTransport, body_client


def meta(part: Path, *, etag='"v1"', last_modified="Wed, 01 Jan 2025 00:00:00 GMT", total=5, size=5):
    part.with_name(part.name + ".meta").write_text(json.dumps({
        "etag": etag, "last_modified": last_modified, "total": total, "size": size,
    }), encoding="utf-8")


class IfRangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.part = Path(self.tmp.name) / "a.part"

    def test_resume_sends_if_range_and_accepts_matching_206(self):
        self.part.write_bytes(b"12345")
        meta(self.part)
        seen = []

        def open_side(req, timeout=0):
            seen.append(req.headers)
            return FakeResponse(b"67890", status=206, headers={
                "Content-Type": "application/pdf", "Content-Range": "bytes 5-9/10",
                "Content-Length": "5", "ETag": '"v1"',
                "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            })

        # FakeTransport with a tiny adapter around the response callable.
        class T(FakeTransport):
            def open(inner, req, timeout=0):
                return open_side(req, timeout)
        result = body_client(T()).download("https://x.gov.cn/a.pdf", self.part)
        self.assertEqual(result.bytes, 10)
        self.assertEqual(result.resumed_from, 5)
        self.assertEqual({k.lower(): v for k, v in seen[0].items()}.get("if-range"), '"v1"')

    def test_weak_etag_forces_full_restart(self):
        self.part.write_bytes(b"12345")
        meta(self.part, etag='W/"weak"', last_modified="")
        seen = []

        def open_side(req, timeout=0):
            seen.append(req.headers)
            return FakeResponse(b"fresh", headers={"Content-Type": "application/pdf", "Content-Length": "5"})

        class T(FakeTransport):
            def open(inner, req, timeout=0):
                return open_side(req, timeout)
        result = body_client(T()).download("https://x.gov.cn/a.pdf", self.part)
        self.assertEqual(result.bytes, 5)
        self.assertEqual(result.resumed_from, 0)
        self.assertIsNone({k.lower(): v for k, v in seen[0].items()}.get("range"))

    def test_changed_etag_rejects_206_and_restarts(self):
        self.part.write_bytes(b"12345")
        meta(self.part)
        calls = []

        def open_side(req, timeout=0):
            calls.append(req.headers)
            if len(calls) == 1:
                return FakeResponse(b"67890", status=206, headers={
                    "Content-Type": "application/pdf", "Content-Range": "bytes 5-9/10",
                    "Content-Length": "5", "ETag": '"v2"',
                })
            return FakeResponse(b"fresh", headers={"Content-Type": "application/pdf", "Content-Length": "5"})

        class T(FakeTransport):
            def open(inner, req, timeout=0):
                return open_side(req, timeout)
        result = body_client(T()).download("https://x.gov.cn/a.pdf", self.part)
        self.assertEqual(result.bytes, 5)
        self.assertEqual(result.resumed_from, 0)
        self.assertIsNone({k.lower(): v for k, v in calls[1].items()}.get("range"))

    def test_416_requires_matching_strong_validator(self):
        self.part.write_bytes(b"12345")
        meta(self.part)
        t = FakeTransport([FakeResponse(b"", status=416)], probe_info=ProbeInfo(content_length=5, etag='"v1"'))
        result = body_client(t).download("https://x.gov.cn/a.pdf", self.part)
        self.assertEqual(result.bytes, 5)

    def test_416_without_validator_restarts(self):
        self.part.write_bytes(b"12345")
        meta(self.part, etag="")
        calls = []

        def open_side(req, timeout=0):
            calls.append(req.headers)
            if len(calls) == 1:
                return FakeResponse(b"", status=416)
            return FakeResponse(b"fresh", headers={"Content-Type": "application/pdf", "Content-Length": "5"})

        class T(FakeTransport):
            def open(inner, req, timeout=0):
                return open_side(req, timeout)
        result = body_client(T(probe_info=ProbeInfo(content_length=5))).download("https://x.gov.cn/a.pdf", self.part)
        self.assertEqual(result.bytes, 5)
        self.assertEqual(result.resumed_from, 0)


if __name__ == "__main__":
    unittest.main()
