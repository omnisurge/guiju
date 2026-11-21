"""IDN canonical URL, probe failure and 9MiB stream regression tests."""

# ruff: noqa: SIM117
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from guiju import compliance
from guiju.errors import ComplianceDenied, SourceUnreachable
from guiju.net.body import BodyClient
from guiju.net.policy import BodyPolicy, MetadataPolicy
from guiju.net.redirect import RedirectGuard
from guiju.net.request import ProbeInfo
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


class ProbeHardeningTest(unittest.TestCase):
    def test_probe_checks_policy_and_propagates_compliance_denied(self):
        def deny(url):
            raise ComplianceDenied("not allowed")

        transport = Transport(BodyPolicy(check_url_fn=deny), Throttle(0))
        with mock.patch.object(transport, "open") as opened, self.assertRaises(ComplianceDenied):
            transport.probe("https://x.gov.cn/a")
        opened.assert_not_called()

    def test_probe_waits_throttle_and_sends_honest_headers(self):
        class RecordingThrottle:
            def __init__(self):
                self.calls = []

            def wait(self, host):
                self.calls.append(host)

        throttle = RecordingThrottle()
        with LocalServer("head_ok") as server:
            info = Transport(BodyPolicy(check_url_fn=lambda u: u), throttle).probe(
                server.url + "/a", referer="https://ref.example/"
            )
            headers = server.state.requests[0].headers
        self.assertEqual(throttle.calls, ["127.0.0.1"])
        self.assertEqual(info.status, 200)
        self.assertEqual(headers.get("accept"), "*/*")
        self.assertEqual(headers.get("referer"), "https://ref.example/")
        self.assertTrue(headers.get("user-agent", "").startswith("guiju/"))
        self.assertNotIn("python-urllib", headers.get("user-agent", "").lower())

    def test_probe_supports_metadata_policy_host_result(self):
        """MetadataPolicy.check_url returns a host, not a URL; probe must use the original URL."""
        target = "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWebApi/external/localStandard/list"
        transport = Transport(MetadataPolicy.default(), Throttle(0))
        with mock.patch.object(
            transport, "open", return_value=FakeResponse(b"", status=200, headers={"Content-Length": "0"})
        ) as opened:
            info = transport.probe(target)
        self.assertEqual(info.status, 200)
        self.assertEqual(opened.call_args.args[0].full_url, target)

    def test_probe_metadata_policy_denies_bad_host_without_valueerror(self):
        transport = Transport(MetadataPolicy.default(), Throttle(0))
        with self.assertRaises(ComplianceDenied):
            transport.probe("https://evil.example/a")

    def test_probe_swallows_only_network_protocol_errors(self):
        transport = Transport(BodyPolicy(check_url_fn=lambda u: u), Throttle(0))
        with mock.patch.object(transport, "open", side_effect=RuntimeError("program bug")):
            with self.assertRaises(RuntimeError):
                transport.probe("https://x.gov.cn/a")


class ProbeRefererTest(unittest.TestCase):
    def test_416_body_path_passes_referer_to_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            part.write_bytes(b"x" * 500)
            Path(str(part) + ".meta").write_text(json.dumps({
                "etag": '"v1"', "last_modified": "", "total": 500, "size": 500,
            }), encoding="utf-8")
            transport = FakeTransport(
                [FakeResponse(b"", status=416, headers={"Content-Length": "0"})],
                probe_info=ProbeInfo(content_length=500, etag='"v1"'),
            )
            client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
            result = client.download("https://x.gov.cn/a", part, attempts=1, referer="https://shzjw.example/")
        self.assertEqual(result.status, 416)
        self.assertEqual(transport.probe_calls[0]["referer"], "https://shzjw.example/")


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
        t = Transport(BodyPolicy(check_url_fn=lambda u: u), Throttle(0))
        with mock.patch.object(t, "open", side_effect=urllib.error.HTTPError("https://x.gov.cn/a", 302, "Found", {}, None)):
            info = t.probe("https://x.gov.cn/a")
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
