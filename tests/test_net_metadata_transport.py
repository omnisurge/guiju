"""MetadataClient transport-focused offline tests (C3 migration subset)."""

from __future__ import annotations

import io
import unittest
from urllib.error import HTTPError

from guiju.errors import ComplianceDenied, SourceUnreachable
from guiju.net.metadata import MetadataClient
from guiju.net.policy import MetadataPolicy
from guiju.net.throttle import Throttle
from tests.net_harness import FakeResponse, FakeTransport

URL = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"


def client(responses):
    return MetadataClient(policy=MetadataPolicy.default(), throttle=Throttle(0), transport=FakeTransport(responses))


class MetadataClientTransportTest(unittest.TestCase):
    def test_post_form_returns_json_bytes(self):
        c = client([FakeResponse(b'{"Total":0,"List":[]}', url=URL,
                                 headers={"Content-Type": "application/json"})])
        self.assertEqual(c.post_form(URL, {"a": "1"}), b'{"Total":0,"List":[]}')

    def test_429_without_marker_retries(self):
        err = HTTPError(URL, 429, "slow", {"Content-Type": "text/html"}, io.BytesIO(b"slow down"))
        c = client([err, FakeResponse(b"{}", url=URL,
                                     headers={"Content-Type": "application/json", "Content-Length": "2"})])
        self.assertEqual(c.post_form(URL, {"a": "1"}), b"{}")

    def test_html_block_body_is_denied(self):
        c = client([FakeResponse("<html>验证码</html>".encode(), url=URL,
                                 headers={"Content-Type": "text/html"})])
        with self.assertRaises(ComplianceDenied):
            c.post_form(URL, {"a": "1"})

    def test_truncated_body_is_source_unreachable(self):
        c = client([FakeResponse(b"{}", url=URL,
                                 headers={"Content-Type": "application/json", "Content-Length": "10"})])
        with self.assertRaises(SourceUnreachable):
            c.post_form(URL, {"a": "1"})


if __name__ == "__main__":
    unittest.main()
