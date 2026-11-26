"""断点续传：206 追加、服务端无视 Range 时重下、HTML 页拒绝落盘。"""

import http.server
import json
import tempfile
import threading
import unittest
from pathlib import Path

from guiju.errors import ComplianceDenied
from guiju.net.block import BLOCK_MARKERS
from guiju.net.body import BodyClient
from guiju.net.policy import BodyPolicy
from guiju.net.throttle import Throttle

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


if __name__ == "__main__":
    unittest.main()
