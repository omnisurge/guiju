"""IDN canonical URL, probe failure and 9MiB stream regression tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju import compliance
from guiju.errors import SourceUnreachable
from guiju.net.body import BodyClient
from guiju.net.policy import BodyPolicy
from guiju.net.redirect import RedirectGuard
from guiju.net.throttle import Throttle
from guiju.net.transport import Transport
from tests.char_baseline_harness import LocalServer
from tests.net_harness import FakeResponse, FakeTransport


class IdnRequestTest(unittest.TestCase):
    def test_body_policy_check_url_returns_punycode(self):
        self.assertEqual(
            BodyPolicy().check_url("https://中国.gov.cn/a"),
            "https://xn--fiqs8s.gov.cn/a",
        )

    def test_body_request_full_url_is_punycode(self):
        transport = FakeTransport([FakeResponse(b"%PDF-1.7", headers={"Content-Type": "application/pdf", "Content-Length": "8"})])
        client = BodyClient(policy=BodyPolicy(), throttle=Throttle(0), transport=transport)
        with tempfile.TemporaryDirectory() as tmp:
            client.download("https://中国.gov.cn/a", Path(tmp) / "a.part")
        self.assertEqual(transport.calls[0].full_url, "https://xn--fiqs8s.gov.cn/a")

    def test_redirect_request_full_url_is_punycode(self):
        guard = RedirectGuard(BodyPolicy(), Throttle(0))
        req = __import__("urllib.request", fromlist=["Request"]).Request("https://xn--fiqs8s.gov.cn/a")
        redirect = guard.redirect_request(req, None, 302, "Found", {}, "https://中国.gov.cn/b")
        self.assertEqual(redirect.full_url, "https://xn--fiqs8s.gov.cn/b")


class ProbeFailureTest(unittest.TestCase):
    def test_probe_failure_405(self):
        with LocalServer("head_405") as server:
            info = Transport(BodyPolicy(check_url_fn=lambda u: u), Throttle(0)).probe(server.url + "/a")
        self.assertIsNone(info.status)
        self.assertIsNone(info.content_length)

    def test_probe_failure_500(self):
        with LocalServer("head_500") as server:
            info = Transport(BodyPolicy(check_url_fn=lambda u: u), Throttle(0)).probe(server.url + "/a")
        self.assertIsNone(info.status)

    def test_probe_timeout(self):
        t = Transport(BodyPolicy(check_url_fn=lambda u: u), Throttle(0))
        with mock.patch.object(t, "open", side_effect=TimeoutError("timeout")):
            info = t.probe("https://x.gov.cn/a")
        self.assertIsNone(info.status)

    def test_probe_redirect(self):
        with LocalServer("redirect_loop", body=b"") as server:
            info = Transport(BodyPolicy(check_url_fn=lambda u: u), Throttle(0)).probe(server.url + "/a")
        self.assertIsNone(info.status)

    def test_truncated_probe_sink(self):
        sink = []
        body = b"%PDF-12345"
        with LocalServer("truncated", body=body, headers={"Content-Length": "10"}) as server:
            c = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), probe_sink=sink.append)
            with self.assertRaises(SourceUnreachable):
                c.download(server.url + "/a", Path(tempfile.mkdtemp()) / "a.part", attempts=1)
        self.assertTrue(any(r.outcome == "truncated" for r in sink), sink)


class LargeStreamTest(unittest.TestCase):
    def test_body_download_bytes_9mib(self):
        size = 9_437_193
        body = b"%PDF-1.7\n" + b"x" * (size - 9)
        with LocalServer("ok", body=body) as server:
            data, ctype = BodyClient(
                policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0)
            ).download_bytes(server.url + "/a", max_bytes=128 * 1024 * 1024)
        self.assertEqual(len(data), size)
        self.assertEqual(ctype, "application/pdf")

    def test_mohurd_download_9mib(self):
        from guiju.sources import mohurd
        size = 9_437_193
        body = b"%PDF-1.7\n" + b"x" * (size - 9)
        with mock.patch.object(compliance, "check_url", side_effect=lambda u: u):
            with LocalServer("ok", body=body) as server:
                data, magic = mohurd.download(server.url + "/a", "")
        self.assertEqual(len(data), size)
        self.assertEqual(magic, "pdf")


if __name__ == "__main__":
    unittest.main()
