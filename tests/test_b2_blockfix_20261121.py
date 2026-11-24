"""B2/W1 block-fix regressions (2026-11-21)."""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from guiju.domain import shzjw as core
from guiju.errors import ComplianceDenied, NotIndexed, SourceUnreachable
from guiju.net.body import BodyClient
from guiju.net.policy import BodyPolicy
from guiju.net.request import ProbeInfo
from guiju.net.throttle import Throttle
from guiju.sources import shzjw as source
from tests.char_baseline_harness import LocalServer
from tests.net_harness import FakeResponse, FakeTransport


class BodyPolicyShanghaiTest(unittest.TestCase):
    def test_shzjw_download_url_passes_real_body_policy(self) -> None:
        url = core.download_url("fid-1")
        self.assertEqual(BodyPolicy().check_url(url), url)
        self.assertIn(":9175/", url)

    def test_shzjw_signed_download_url_passes_real_body_policy(self) -> None:
        with mock.patch.object(source.time, "time", return_value=1_700_000_000.0):
            signed = source._signed_download_url(core.download_url("fid-1"))
        self.assertEqual(BodyPolicy().check_url(signed), signed)

    def test_body_policy_shanghai_port_is_host_scoped(self) -> None:
        accepted = (
            "https://bzjg.zjw.sh.gov.cn:9175/a",
            core.download_url("fid-1"),
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(BodyPolicy().check_url(url), url)

        denied = (
            "http://bzjg.zjw.sh.gov.cn:9175/a",
            "https://bzjg.zjw.sh.gov.cn/a",
            "https://bzjg.zjw.sh.gov.cn:443/a",
            "https://www.gov.cn:9175/a",
            "https://other.gov.cn:9175/a",
            "https://www.gov.cn:8443/a",
            "https://user:pass@bzjg.zjw.sh.gov.cn:9175/a",
        )
        for url in denied:
            with self.subTest(url=url), self.assertRaises(ComplianceDenied):
                BodyPolicy().check_url(url)

    def test_body_policy_other_gov_host_default_ports_still_work(self) -> None:
        accepted = (
            "https://www.gov.cn/a",
            "http://www.gov.cn:80/a",
            "https://www.gov.cn:443/a",
            "https://gov.cn/a",
        )
        for url in accepted:
            with self.subTest(url=url):
                self.assertEqual(BodyPolicy().check_url(url), url)


class SignedDownloadUrlTest(unittest.TestCase):
    PDF = b"%PDF-1.7\n" + b"x" * 32

    def test_helper_contains_timestamp_and_received_token(self) -> None:
        frozen = 1_700_000_000.123
        with mock.patch.object(source.time, "time", return_value=frozen):
            signed = source._signed_download_url(core.download_url("fid-1"))
        ts = int(frozen * 1000)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(signed).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertEqual(query["timestamp"], [str(ts)])
        self.assertEqual(query["receivedToken"], [source._token("fid-1", ts)])

    def test_download_sends_signed_url_and_referer(self) -> None:
        captured: dict[str, object] = {}

        def fake_download_bytes(url: str, *, referer=None, timeout=180, probe_sink=None):
            captured["url"] = url
            captured["referer"] = referer
            return self.PDF, "application/pdf"

        with mock.patch.object(source.compliance, "download_bytes", side_effect=fake_download_bytes):
            body, magic = source.download(core.download_url("fid-1"), referer="https://ref.example/")
        self.assertEqual((body, magic), (self.PDF, "pdf"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(captured["url"])).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertIn("timestamp", query)
        self.assertIn("receivedToken", query)
        self.assertNotEqual(captured["url"], core.download_url("fid-1"))
        self.assertEqual(captured["referer"], "https://ref.example/")

    def test_download_request_full_url_contains_query(self) -> None:
        captured: dict[str, str] = {}

        def run_body(url: str, *, referer=None, timeout=180, probe_sink=None):
            transport = FakeTransport([FakeResponse(self.PDF)])
            client = BodyClient(
                policy=BodyPolicy(check_url_fn=lambda value: value),
                throttle=Throttle(0),
                transport=transport,
            )
            data = client.download_bytes(url, referer=referer)[0]
            captured["url"] = transport.calls[0].full_url
            return data

        with mock.patch.object(source.compliance, "download_bytes", side_effect=lambda url, **kwargs: (run_body(url, **kwargs), "application/pdf")):
            body, _ = source.download(core.download_url("fid-1"))
        self.assertEqual(body, self.PDF)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertIn("timestamp", query)
        self.assertIn("receivedToken", query)

    def test_download_to_sends_signed_url(self) -> None:
        captured: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"

            def fake_download(url: str, part_path: str, *, referer=None, timeout=180, probe_sink=None):
                captured["url"] = url
                Path(part_path).write_bytes(self.PDF)
                return {"bytes": len(self.PDF), "resumed_from": 0}

            with mock.patch.object(source.compliance, "download", side_effect=fake_download):
                result = source.download_to(core.download_url("fid-1"), str(part))
        self.assertEqual(result, (len(self.PDF), "pdf", 0))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(captured["url"])).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertIn("timestamp", query)
        self.assertIn("receivedToken", query)

    def test_download_to_request_full_url_contains_query(self) -> None:
        captured: dict[str, str] = {}
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"

            def run_body(url: str, part_path: str, *, referer=None, timeout=180, probe_sink=None):
                transport = FakeTransport([FakeResponse(self.PDF)])
                client = BodyClient(
                    policy=BodyPolicy(check_url_fn=lambda value: value),
                    throttle=Throttle(0),
                    transport=transport,
                )
                info = client.download(url, part_path, referer=referer, timeout=timeout)
                captured["url"] = transport.calls[0].full_url
                return {"bytes": info.bytes, "resumed_from": info.resumed_from}

            with mock.patch.object(source.compliance, "download", side_effect=run_body):
                result = source.download_to(core.download_url("fid-1"), str(part))
        self.assertEqual(result, (len(self.PDF), "pdf", 0))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
        self.assertEqual(query["fileId"], ["fid-1"])
        self.assertIn("timestamp", query)
        self.assertIn("receivedToken", query)

    def test_both_entry_points_share_frozen_url(self) -> None:
        frozen = 1_700_000_000.0
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(source.time, "time", return_value=frozen):
            part = Path(tmp) / "body.part"
            captured: list[str] = []

            def fake_download_bytes(url: str, *, referer=None, timeout=180, probe_sink=None):
                captured.append(url)
                return self.PDF, "application/pdf"

            def fake_download(url: str, part_path: str, *, referer=None, timeout=180, probe_sink=None):
                captured.append(url)
                Path(part_path).write_bytes(self.PDF)
                return {"bytes": len(self.PDF), "resumed_from": 0}

            with mock.patch.object(source.compliance, "download_bytes", side_effect=fake_download_bytes), mock.patch.object(
                source.compliance, "download", side_effect=fake_download
            ):
                source.download(core.download_url("fid-1"))
                source.download_to(core.download_url("fid-1"), str(part))
        self.assertEqual(len(captured), 2)
        self.assertEqual(captured[0], captured[1])

    def test_missing_file_id_is_not_signed(self) -> None:
        with self.assertRaises(NotIndexed):
            source._signed_download_url("https://bzjg.zjw.sh.gov.cn:9175/a")

    def test_missing_file_id_both_entry_points_do_not_request(self) -> None:
        raw = "https://bzjg.zjw.sh.gov.cn:9175/a"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            source.compliance, "download"
        ) as download, mock.patch.object(source.compliance, "download_bytes") as download_bytes:
            with self.assertRaises(NotIndexed):
                source.download(raw)
            with self.assertRaises(NotIndexed):
                source.download_to(raw, str(Path(tmp) / "a.part"))
        download.assert_not_called()
        download_bytes.assert_not_called()


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


class P2RangeBoundsTest(unittest.TestCase):
    def _client(self, response):
        transport = FakeTransport([response])
        return BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport), transport

    def test_range_end_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            part.write_bytes(b"x" * 500)
            part.with_name(part.name + ".meta").write_text(json.dumps({
                "etag": '"v1"', "last_modified": "", "total": 1000, "size": 500,
            }), encoding="utf-8")
            response = FakeResponse(
                b"", status=206,
                headers={"Content-Range": "bytes 500-499/1000", "Content-Length": "0"},
            )
            client, _ = self._client(response)
            with self.assertRaises(SourceUnreachable):
                client.download("https://x.gov.cn/a", part, attempts=1)
            self.assertEqual(part.read_bytes(), b"x" * 500)

    def test_range_end_equals_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            response = FakeResponse(
                b"", status=206,
                headers={"Content-Range": "bytes 0-1000/1000", "Content-Length": "1001"},
            )
            client, _ = self._client(response)
            with self.assertRaises(SourceUnreachable):
                client.download("https://x.gov.cn/a", part, attempts=1)
            self.assertFalse(part.exists())


class P0TruncationNegativeTest(unittest.TestCase):
    def test_range_invalid_bounds_not_retried(self) -> None:
        transport = FakeTransport([
            FakeResponse(b"", status=206, headers={"Content-Range": "bytes 500-499/1000", "Content-Length": "0"})
        ])
        client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SourceUnreachable):
            client.download("https://x.gov.cn/a", Path(tmp) / "a.part", attempts=2)
        self.assertEqual(len(transport.calls), 1)

    def test_range_bad_validator_not_truncation_retry(self) -> None:
        body = b"%PDF-1.7\n" + b"x" * (1000 - 9)
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            part.write_bytes(b"x" * 500)
            part.with_name(part.name + ".meta").write_text(json.dumps({
                "etag": '"v1"', "last_modified": "", "total": 1000, "size": 500,
            }), encoding="utf-8")
            first = FakeResponse(
                b"x" * 500,
                status=206,
                headers={"Content-Range": "bytes 500-999/1000", "Content-Length": "500", "ETag": '"v2"'},
            )
            transport = FakeTransport([first, FakeResponse(body, status=200)])
            client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
            result = client.download("https://x.gov.cn/a", part, attempts=2)
        self.assertEqual((result.status, result.bytes, result.resumed_from), (200, 1000, 0))
        self.assertEqual(len(transport.calls), 2)

    def test_truncated_without_validator_not_retried(self) -> None:
        transport = FakeTransport([
            FakeResponse(b"x" * 50, status=200, headers={"Content-Length": "100"})
        ])
        client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0), transport=transport)
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SourceUnreachable):
            client.download("https://x.gov.cn/a", Path(tmp) / "a.part", attempts=2)
        self.assertEqual(len(transport.calls), 1)


class P2SidecarWriteTest(unittest.TestCase):
    def test_sidecar_write_amplification_bounded(self) -> None:
        import guiju.net.body as body_mod

        original = body_mod._write_meta
        sizes = []

        def record(part, **kwargs):
            sizes.append(kwargs["size"])
            return original(part, **kwargs)

        body = b"%PDF-1.7\n" + b"x" * (3 * 1024 * 1024 - 9)
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            client = BodyClient(policy=BodyPolicy(check_url_fn=lambda u: u), throttle=Throttle(0))
            with mock.patch.object(body_mod, "_write_meta", side_effect=record), LocalServer(
                "ok", body=body
            ) as server:
                result = client.download(server.url + "/a", part, attempts=1, chunk_size=64 * 1024)
        self.assertEqual(result.bytes, len(body))
        self.assertLessEqual(len(sizes), 8, sizes)
        self.assertIn(64 * 1024, sizes)
        self.assertIn(len(body), sizes)
        self.assertFalse(Path(str(part) + ".meta").exists())


if __name__ == "__main__":
    unittest.main()
