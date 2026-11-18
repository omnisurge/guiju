"""A1：下载完整性 —— 声明长度与实际字节不符必须失败，不能把截断当成功。"""

import http.server
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from guiju import compliance
from guiju.errors import SourceUnreachable

PAYLOAD = b"%PDF-1.7\n" + b"0123456789" * 200


class TruncatingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # 故意声明完整长度，只发前 100 字节，然后断开
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD[:100])
        self.close_connection = True

    def log_message(self, *_args):
        return


class DownloadIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TruncatingHandler)
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


if __name__ == "__main__":
    unittest.main()
