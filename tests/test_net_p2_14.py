"""P2-14: 403/429 error-body inspection and metadata 429 retry semantics."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from guiju.errors import ComplianceDenied, SourceUnreachable
from guiju.net.metadata import MetadataClient
from guiju.net.policy import MetadataPolicy
from tests.net_harness import FakeResponse, FakeTransport, body_client

URL = "https://www.mohurd.gov.cn/a.pdf"
META_URL = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"


def error(status: int, body: bytes, content_type: str = "text/html") -> HTTPError:
    return HTTPError(URL, status, "error", {"Content-Type": content_type}, io.BytesIO(body))


class BodyErrorBodyTest(unittest.TestCase):
    def test_403_marker_is_compliance_denied_once(self):
        t = FakeTransport([error(403, "访问过于频繁".encode("gbk"))])
        with self.assertRaises(ComplianceDenied):
            body_client(t).download(URL, Path(tempfile.mkdtemp()) / "a.part")
        self.assertEqual(len(t.calls), 1)

    def test_403_without_marker_is_source_unreachable_once(self):
        t = FakeTransport([error(403, b"<html>Forbidden</html>")])
        with self.assertRaises(SourceUnreachable):
            body_client(t).download(URL, Path(tempfile.mkdtemp()) / "a.part")
        self.assertEqual(len(t.calls), 1)

    def test_429_marker_is_compliance_denied_once(self):
        t = FakeTransport([error(429, "请求过于频繁".encode())])
        with self.assertRaises(ComplianceDenied):
            body_client(t).download(URL, Path(tempfile.mkdtemp()) / "a.part")
        self.assertEqual(len(t.calls), 1)

    def test_429_without_marker_is_source_unreachable_once(self):
        t = FakeTransport([error(429, b"slow down")])
        with self.assertRaises(SourceUnreachable):
            body_client(t).download(URL, Path(tempfile.mkdtemp()) / "a.part")
        self.assertEqual(len(t.calls), 1)


class MetadataRetryTest(unittest.TestCase):
    def client(self, responses):
        return MetadataClient(
            policy=MetadataPolicy.default(), throttle=__import__("guiju.net.throttle", fromlist=["Throttle"]).Throttle(0),
            transport=FakeTransport(responses),
        )

    def test_429_marker_is_compliance_denied(self):
        client = self.client([error(429, "验证码".encode())])
        with self.assertRaises(ComplianceDenied):
            client.post_form(META_URL, {"a": "1"})

    def test_429_without_marker_retries_then_succeeds(self):
        client = self.client([error(429, b"slow down"),
                              FakeResponse(b"{}", url=META_URL, headers={"Content-Type": "application/json", "Content-Length": "2"})])
        self.assertEqual(client.post_form(META_URL, {"a": "1"}), b"{}")


if __name__ == "__main__":
    unittest.main()
