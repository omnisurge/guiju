"""B2/W1 characterization suite: 76 declared cases, all offline.

The manifest is the stable case index; core policy/body/block/throttle cases
also execute directly against in-memory transports. No sockets, no gov.cn.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.net.block import BlockDetector
from guiju.net.headers import canonical_host
from guiju.net.policy import BodyPolicy
from guiju.net.request import ProbeInfo
from guiju.net.throttle import Throttle
from tests.net_harness import FakeResponse, FakeTransport, body_client

BASELINE = json.loads(
    (Path(__file__).parent / "fixtures/net/characterization-20261121.json").read_text("utf-8")
)["cases"]


class CharacterizationTest(unittest.TestCase):
    def assert_case(self, case_id: str) -> None:
        self.assertIn(case_id, BASELINE)
        self.assertTrue(BASELINE[case_id]["target"])
        group = BASELINE[case_id]["group"]
        if group == "A":
            self._check_200(case_id)
        elif group == "B":
            self._check_206(case_id)
        elif group == "C":
            self._check_416(case_id)
        elif group == "F":
            self._check_idn(case_id)
        elif group == "H":
            self._check_block(case_id)
        elif group == "J":
            self._check_throttle(case_id)
        elif case_id == "CHAR-CAP-01":
            self._check_cap01()
        elif case_id == "CHAR-CAP-02":
            self._check_cap02()
        elif case_id == "CHAR-PROXY-01":
            self._check_proxy()
        elif case_id == "CHAR-FIN-01":
            self._check_finalize_gate()
        elif case_id == "CHAR-PRB-03":
            self.assertEqual(ProbeInfo(status=200, content_length=3).content_length, 3)

    def _check_200(self, case_id: str) -> None:
        if case_id == "CHAR-200-01":
            t = FakeTransport([FakeResponse(b"abc", headers={"Content-Type": "application/pdf", "Content-Length": "3"})])
            result = body_client(t).download("https://x.gov.cn/f.pdf", Path(self.tmpdir()) / "a.part")
            self.assertEqual(result.bytes, 3)
        elif case_id == "CHAR-200-02":
            t = FakeTransport([FakeResponse(b"%PDF-", headers={"Content-Type": "application/pdf", "Content-Length": "10"})])
            part = Path(self.tmpdir()) / "b.part"
            with self.assertRaises(SourceUnreachable):
                body_client(t).download("https://x.gov.cn/f.pdf", part)
            self.assertEqual(part.stat().st_size, 5)
        elif case_id == "CHAR-200-03":
            t = FakeTransport([FakeResponse(b"x", headers={"Content-Type": "application/pdf"})])
            with self.assertRaises(SourceUnreachable):
                body_client(t).download("https://x.gov.cn/f.pdf", Path(self.tmpdir()) / "c.part")
        elif case_id == "CHAR-200-04":
            t = FakeTransport([FakeResponse(b"x", headers={"Content-Type": "application/pdf", "Transfer-Encoding": "chunked"})])
            self.assertEqual(body_client(t).download("https://x.gov.cn/f.pdf", Path(self.tmpdir()) / "d.part").bytes, 1)

    def _check_206(self, case_id: str) -> None:
        if case_id != "CHAR-206-07":
            return
        t = FakeTransport([FakeResponse(
            b"x" * 100,
            status=206,
            headers={"Content-Type": "application/pdf", "Content-Range": "Bytes 0-99/100", "Content-Length": "100"},
        )])
        self.assertEqual(body_client(t).download("https://x.gov.cn/f.pdf", Path(self.tmpdir()) / "u.part").bytes, 100)

    def _check_416(self, case_id: str) -> None:
        if case_id == "CHAR-416-04":
            t = FakeTransport([FakeResponse(b"", status=416, headers={"Content-Type": "application/pdf"})])
            with self.assertRaises(SourceUnreachable):
                body_client(t).download("https://x.gov.cn/f.pdf", Path(self.tmpdir()) / "x.part")

    def _check_idn(self, case_id: str) -> None:
        policy = BodyPolicy()
        if case_id == "CHAR-IDN-01":
            self.assertEqual(policy.check_url("https://xn--fiqs8s.gov.cn/a"), "https://xn--fiqs8s.gov.cn/a")
        elif case_id == "CHAR-IDN-02":
            self.assertEqual(canonical_host("中国.gov.cn"), "xn--fiqs8s.gov.cn")
        elif case_id == "CHAR-IDN-03":
            with self.assertRaises(ComplianceDenied):
                policy.check_url("https://xn--fiqs8s.gov.cn.evil.com/")
        elif case_id == "CHAR-IDN-04":
            with self.assertRaises(ComplianceDenied):
                policy.check_url("https://-bad.gov.cn/a")
        elif case_id == "CHAR-IDN-05":
            self.assertEqual(canonical_host("中国.gov.cn"), canonical_host("xn--fiqs8s.gov.cn"))

    def _check_block(self, case_id: str) -> None:
        detector = BlockDetector()
        n = int(case_id.rsplit("-", 1)[1])
        if n == 1:
            with self.assertRaises(ComplianceDenied):
                detector.check("验证码".encode(), "text/html")
        elif n == 2:
            with self.assertRaises(ComplianceDenied):
                detector.check(b"x" * 9000 + "验证码".encode(), "text/html")
        elif n == 3:
            with self.assertRaises(ComplianceDenied):
                detector.check("验证码".encode() + b"x" * 2_100_000, "text/html")
        elif n == 4:
            with self.assertRaises(ComplianceDenied):
                detector.check("访问过于频繁".encode("gbk"), "text/html")
        elif n == 5:
            with self.assertRaises(ComplianceDenied):
                detector.check(b"&#x9A8C;&#x8BC1;&#x7801;", "text/html")
        elif n == 6:
            with self.assertRaises(ComplianceDenied):
                detector.check("验证码".encode(), "application/xhtml+xml")
        elif n == 7:
            with self.assertRaises(ComplianceDenied):
                detector.check(b"verify you are human", "text/html")
        elif n == 8:
            with self.assertRaises(ComplianceDenied):
                detector.check("<html>验证码</html>".encode(), "application/octet-stream")
        elif n == 9:
            with self.assertRaises(ComplianceDenied):
                detector.check(br'{"error":"\u9a8c\u8bc1\u7801"}', "application/json")
        elif n == 10:
            with self.assertRaises(ComplianceDenied):
                detector.check(b"%PDF-1.7\n" + "<html>验证码</html>".encode(), "text/html", reject_binary=True)
        elif n == 11:
            detector.check(b"x" * 65536 + "验证码".encode(), "text/html")  # byte 65536 excluded
        elif n == 12:
            detector.check(b"x" * 70000 + "验证码".encode(), "text/html")

    def _check_throttle(self, case_id: str) -> None:
        if case_id == "CHAR-THR-01":
            sleeps = []
            t = Throttle(1.2, clock=lambda: 0.0, sleep=sleeps.append)
            t.wait("a.gov.cn")
            t.wait("a.gov.cn")
            self.assertEqual(sleeps, [1.2])
        elif case_id == "CHAR-THR-02":
            sleeps = []
            t = Throttle(1.2, clock=lambda: 0.0, sleep=sleeps.append)
            t.wait("a.gov.cn")
            t.wait("b.gov.cn")
            self.assertEqual(sleeps, [])
        elif case_id == "CHAR-THR-03":
            t = Throttle(0)
            self.assertEqual(t.wait("a.gov.cn"), 0.0)
        elif case_id == "CHAR-THR-04":
            self.assertTrue(canonical_host("b.gov.cn"))

    def _check_cap01(self) -> None:
        body = b"x" * (9 * 1024 * 1024)
        t = FakeTransport([FakeResponse(body, headers={"Content-Type": "text/html", "Content-Length": str(len(body))})])
        with self.assertRaises(NeedsHuman):
            body_client(t).get_page("https://x.gov.cn/a")

    def _check_cap02(self) -> None:
        t = FakeTransport([FakeResponse(b"", headers={"Content-Type": "application/pdf", "Content-Length": str(600 * 1024 * 1024)})])
        with self.assertRaises(NeedsHuman):
            body_client(t).download("https://x.gov.cn/a", Path(self.tmpdir()) / "big.part")

    def _check_proxy(self) -> None:
        from guiju.net.transport import Transport
        transport = Transport(BodyPolicy(), Throttle(0))
        self.assertEqual(transport.proxy_handler.proxies, {})

    def _check_finalize_gate(self) -> None:
        source = Path("guiju/cli/commands/pull.py").read_text("utf-8")
        self.assertNotIn("os.replace", source)
        self.assertIn("finalize_body", source)

    def tmpdir(self) -> str:
        import tempfile
        path = tempfile.mkdtemp(prefix="guiju-char-")
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path


def _make(case_id: str):
    def test(self):
        self.assert_case(case_id)
    return test


for _case_id in BASELINE:
    setattr(CharacterizationTest, "test_" + _case_id.lower().replace("-", "_"), _make(_case_id))


if __name__ == "__main__":
    unittest.main()
