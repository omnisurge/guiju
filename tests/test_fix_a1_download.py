"""A1：下载完整性 —— 声明长度/范围与实际字节不符必须失败，不能把截断当成功。"""

import http.server
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from guiju import compliance
from guiju.errors import SourceUnreachable

PAYLOAD = b"%PDF-1.7\n" + b"0123456789" * 200


class RangeHandler(http.server.BaseHTTPRequestHandler):
    mode = "truncated_200"

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
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(compliance, "check_url", lambda url: url).start()
        mock.patch.object(compliance, "_throttle", lambda host: None).start()
        mock.patch.object(compliance.time, "sleep", lambda _s: None).start()

    def test_truncated_body_raises_and_keeps_partial(self):
        part = self.dir / "broken.part"
        with self.assertRaises(SourceUnreachable) as ctx:
            compliance.download(self.url, part, attempts=2)
        self.assertIn("截断", str(ctx.exception))
        self.assertEqual(part.stat().st_size, 100)
        self.assertFalse((self.dir / "broken.pdf").exists())

    def test_206_without_content_range_is_rejected(self):
        RangeHandler.mode = "missing_content_range"
        part = self.dir / "no-range.part"
        with self.assertRaises(SourceUnreachable) as ctx:
            compliance.download(self.url, part, attempts=2)
        self.assertIn("Content-Range", str(ctx.exception))
        self.assertFalse(part.exists())

    def test_206_wrong_start_is_rejected_and_keeps_part(self):
        RangeHandler.mode = "wrong_start"
        part = self.dir / "wrong-start.part"
        part.write_bytes(PAYLOAD[:100])
        with self.assertRaises(SourceUnreachable) as ctx:
            compliance.download(self.url, part, attempts=2)
        self.assertIn("起点", str(ctx.exception))
        self.assertEqual(part.stat().st_size, 100)

    def test_206_truncated_segment_is_rejected(self):
        RangeHandler.mode = "truncated_206"
        part = self.dir / "short-206.part"
        with self.assertRaises(SourceUnreachable):
            compliance.download(self.url, part, attempts=2)
        self.assertLess(part.stat().st_size, len(PAYLOAD))

    def test_206_unknown_total_is_rejected(self):
        RangeHandler.mode = "unknown_total"
        part = self.dir / "unknown-total.part"
        with self.assertRaises(SourceUnreachable) as ctx:
            compliance.download(self.url, part, attempts=2)
        self.assertIn("总长", str(ctx.exception))
        self.assertFalse(part.exists())

    def test_206_partial_known_total_is_rejected(self):
        RangeHandler.mode = "partial_known_total"
        part = self.dir / "partial-total.part"
        with self.assertRaises(SourceUnreachable):
            compliance.download(self.url, part, attempts=2)

    def test_206_body_longer_than_declared_range_is_rejected(self):
        RangeHandler.mode = "body_longer_than_range"
        part = self.dir / "longer-body.part"
        with self.assertRaises(SourceUnreachable):
            compliance.download(self.url, part, attempts=2)

    def test_206_uppercase_unit_is_accepted(self):
        RangeHandler.mode = "uppercase_unit"
        part = self.dir / "uppercase.part"
        result = compliance.download(self.url, part, attempts=2)
        self.assertEqual(result["bytes"], 100)
        self.assertEqual(part.stat().st_size, 100)

    def test_416_without_head_length_does_not_accept_part(self):
        RangeHandler.mode = "416_head_405"
        part = self.dir / "stale.part"
        part.write_bytes(b"%PDF-")
        with self.assertRaises(SourceUnreachable):
            compliance.download(self.url, part, attempts=2)
        self.assertFalse(part.exists())

    def test_header_parsers_are_defensive(self):
        self.assertIsNone(compliance._header_int("²"))
        self.assertIsNone(compliance._header_int("9" * 5000))
        self.assertEqual(
            compliance._parse_content_range("bytes 0-1/" + "9" * 5000),
            (None, None, None),
        )
        self.assertEqual(
            compliance._parse_content_range("Bytes 0-99/100"),
            (0, 99, 100),
        )


if __name__ == "__main__":
    unittest.main()
