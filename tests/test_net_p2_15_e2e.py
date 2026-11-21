"""P2-15 real HTTP sidecar and two-stage download tests."""

# ruff: noqa: SIM117
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from guiju import compliance
from guiju.errors import ComplianceDenied, SourceUnreachable
from guiju.net.body import BodyClient, finalize
from guiju.net.policy import BodyPolicy
from guiju.net.throttle import Throttle
from tests.char_baseline_harness import LocalServer


def _client(**kwargs):
    return BodyClient(
        policy=BodyPolicy(check_url_fn=lambda url: url),
        throttle=Throttle(0),
        **kwargs,
    )


def _meta(part: Path) -> dict:
    raw = Path(str(part) + ".meta")
    return json.loads(raw.read_text("utf-8")) if raw.exists() else {}


class SidecarE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.part = Path(self.tmp.name) / "body.part"

    def _first(self, body: bytes, *, etag: str = '"v1"') -> LocalServer:
        return LocalServer(
            "truncated",
            body=body,
            headers={
                "Content-Length": str(len(body)),
                "ETag": etag,
                "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            },
        )

    def test_interrupted_download_persists_part_and_meta(self):
        body = b"%PDF-12345"
        with self._first(body) as server, self.assertRaises(SourceUnreachable):
            _client().download(server.url + "/a", self.part, attempts=1)
        self.assertEqual(self.part.read_bytes(), body[:5])
        m = _meta(self.part)
        self.assertEqual(m["etag"], '"v1"')
        self.assertEqual(m["last_modified"], "Wed, 01 Jan 2025 00:00:00 GMT")
        self.assertEqual(m["total"], 10)
        self.assertEqual(m["size"], 5)

    def test_resume_after_interrupt_sends_range_and_if_range(self):
        body = b"%PDF-12345"
        with self._first(body) as server, self.assertRaises(SourceUnreachable):
            _client().download(server.url + "/a", self.part, attempts=1)
        with LocalServer("range_ok", body=body, headers={
            "ETag": '"v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
        }) as server:
            result = _client().download(server.url + "/a", self.part, attempts=1)
            req = server.state.requests[0].headers
        self.assertEqual(result.bytes, 10)
        self.assertEqual(result.resumed_from, 5)
        self.assertEqual(req.get("range"), "bytes=5-")
        self.assertEqual(req.get("if-range"), '"v1"')
        self.assertEqual(self.part.read_bytes(), body)
        self.assertFalse(Path(str(self.part) + ".meta").exists())
        final = Path(self.tmp.name) / "final.pdf"
        finalize(self.part, final)
        self.assertEqual(final.read_bytes(), body)
        self.assertFalse(self.part.exists())
        self.assertFalse(Path(str(self.part) + ".meta").exists())

    def test_sidecar_size_updates_during_stream(self):
        body = b"%PDF-123456"
        with LocalServer("slow_stream", body=body, chunk_size=3) as server:
            results = {}

            def run():
                try:
                    results["result"] = _client().download(server.url + "/a", self.part, attempts=1, chunk_size=3)
                except BaseException as exc:  # pragma: no cover - surfaced below
                    results["error"] = exc

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(server.state.first_sent.wait(2), "server did not send first chunk")
            deadline = time.monotonic() + 2
            seen = []
            while time.monotonic() < deadline:
                m = _meta(self.part)
                if m:
                    seen.append(m.get("size"))
                if m.get("size") == 3:
                    break
                time.sleep(0.01)
            server.state.release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), results)
        self.assertIn(3, seen, f"intermediate sidecar sizes={seen}")
        self.assertEqual(results["result"].bytes, len(body))
        self.assertFalse(Path(str(self.part) + ".meta").exists())

    def test_empty_200_cl_zero(self):
        with LocalServer("ok", body=b"") as server:
            result = _client().download(server.url + "/a", self.part, attempts=1)
            self.assertEqual(result.bytes, 0)
        self.assertTrue(self.part.exists())
        self.assertEqual(self.part.stat().st_size, 0)
        self.assertFalse(Path(str(self.part) + ".meta").exists())

    def test_empty_first_chunk_sidecar(self):
        with LocalServer("chunked", body=b"") as server:
            result = _client().download(server.url + "/a", self.part, attempts=1)
            self.assertEqual(result.bytes, 0)
        self.assertTrue(self.part.exists())
        self.assertEqual(self.part.stat().st_size, 0)
        self.assertFalse(Path(str(self.part) + ".meta").exists())

    def test_206_without_part_and_without_if_range_is_complete(self):
        body = b"%PDF-1.7\n" + b"x" * 90
        with LocalServer("range_ok", body=body) as server:
            result = _client().download(server.url + "/a", self.part, attempts=1)
        self.assertEqual(result.bytes, len(body))
        self.assertEqual(result.resumed_from, 0)
        self.assertEqual(self.part.read_bytes(), body)
        self.assertFalse(Path(str(self.part) + ".meta").exists())

    def test_206_without_part_first_chunk_blocked(self):
        with LocalServer("range_ok", body="<html>验证码</html>".encode()) as server:
            with self.assertRaises(ComplianceDenied):
                _client().download(server.url + "/a", self.part, attempts=1)
        self.assertFalse(self.part.exists())
        self.assertFalse(Path(str(self.part) + ".meta").exists())

    def test_finalize_removes_sidecar(self):
        self.part.write_bytes(b"%PDF")
        Path(str(self.part) + ".meta").write_text("{}", encoding="utf-8")
        final = Path(self.tmp.name) / "final.pdf"
        finalize(self.part, final)
        self.assertEqual(final.read_bytes(), b"%PDF")
        self.assertFalse(self.part.exists())
        self.assertFalse(Path(str(self.part) + ".meta").exists())

    def test_finalize_replace_failure(self):
        self.part.write_bytes(b"%PDF")
        Path(str(self.part) + ".meta").write_text("{}", encoding="utf-8")
        with mock.patch("guiju.net.body.os.replace", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                finalize(self.part, Path(self.tmp.name) / "final.pdf")
        self.assertTrue(self.part.exists())
        self.assertTrue(Path(str(self.part) + ".meta").exists())

    def test_write_meta_first_failure_no_existing(self):
        with mock.patch("guiju.net.body._write_meta", side_effect=OSError("meta fail")):
            with LocalServer("ok", body=b"%PDF") as server:
                with self.assertRaises(OSError):
                    _client().download(server.url + "/a", self.part, attempts=1)
        self.assertFalse(self.part.exists())
        self.assertFalse(Path(str(self.part) + ".meta").exists())
        self.assertFalse(any(self.part.parent.glob("*.meta.tmp")))

    def test_write_meta_first_failure_preserves_existing(self):
        self.part.write_bytes(b"old!!")
        meta = Path(str(self.part) + ".meta")
        old = json.dumps({"etag": '"old"', "last_modified": "", "total": 5, "size": 5})
        meta.write_text(old, encoding="utf-8")
        with mock.patch("guiju.net.body._write_meta", side_effect=OSError("meta fail")):
            with LocalServer("ok", body=b"%PDF") as server:
                with self.assertRaises(OSError):
                    _client().download(server.url + "/a", self.part, attempts=1)
        self.assertEqual(self.part.read_bytes(), b"old!!")
        self.assertEqual(meta.read_text("utf-8"), old)

    def test_write_meta_midstream_update_failure(self):
        import guiju.net.body as body_mod
        original = body_mod._write_meta
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("midstream meta fail")
            return original(*args, **kwargs)

        with mock.patch("guiju.net.body._write_meta", side_effect=flaky):
            with LocalServer("ok", body=b"%PDF-123456") as server:
                with self.assertRaises(OSError):
                    _client().download(server.url + "/a", self.part, attempts=1, chunk_size=5)
        self.assertEqual(self.part.read_bytes(), b"%PDF-")  # first chunk is 5 bytes
        m = _meta(self.part)
        self.assertEqual(m.get("size"), 0)
        self.assertFalse(any(self.part.parent.glob("*.meta.tmp")))

    def test_compliance_download_two_stage(self):
        body = b"%PDF-12345"
        with mock.patch.object(compliance, "check_url", side_effect=lambda url: url):
            with self._first(body, etag='"v1"') as first, self.assertRaises(SourceUnreachable):
                compliance.download(first.url + "/a", self.part, attempts=1)
            with LocalServer("range_ok", body=body, headers={
                "ETag": '"v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            }) as second:
                info = compliance.download(second.url + "/a", self.part, attempts=1)
                req = second.state.requests[0].headers
        self.assertEqual(info["bytes"], 10)
        self.assertEqual(info["resumed_from"], 5)
        self.assertEqual(req.get("range"), "bytes=5-")
        self.assertEqual(req.get("if-range"), '"v1"')

    def test_source_download_to_two_stage(self):
        from guiju.sources import mohurd
        body = b"%PDF-12345"
        with mock.patch.object(compliance, "check_url", side_effect=lambda url: url):
            with self._first(body, etag='"v1"') as first, self.assertRaises(SourceUnreachable):
                mohurd.download_to(first.url + "/a", str(self.part), "")
            with LocalServer("range_ok", body=body, headers={
                "ETag": '"v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            }) as second:
                size, magic, resumed = mohurd.download_to(second.url + "/a", str(self.part), "")
                req = second.state.requests[0].headers
        self.assertEqual((size, magic, resumed), (10, "pdf", 5))
        self.assertEqual(req.get("range"), "bytes=5-")
        self.assertEqual(req.get("if-range"), '"v1"')

    def test_pull_two_stage(self):
        import argparse

        from guiju.cli.commands import pull
        from guiju.sources import mohurd
        from guiju.store.sqlite import Store

        body = b"%PDF-12345"
        tmp_home = Path(tempfile.mkdtemp(prefix="guiju-pull-e2e-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_home, ignore_errors=True))
        store = Store(tmp_home / "catalog.db")
        out_dir = tmp_home / "library"
        out_dir.mkdir()
        args = argparse.Namespace(force=False, readable=False)

        class LocalSource:
            id = "pull-e2e"
            display_name = "pull-e2e"
            api_mode = True
            download_url = ""

            def find(self, ref):
                return [{
                    "code": "TEST-1", "name": "测试规范", "status": "current",
                    "published_on": "2026-01-01", "page_url": "https://test.invalid/page",
                    "download_url": self.download_url,
                }]

            def download_to(self, url, part, referer=None):
                return mohurd.download_to(url, part, referer or "")

        source = LocalSource()
        with mock.patch.object(compliance, "check_url", side_effect=lambda url: url):
            with LocalServer("two_stage", body=body, headers={
                "ETag": '"pull-v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT",
            }) as server:
                source.download_url = server.url + "/a"
                with self.assertRaises(SourceUnreachable):
                    pull._pull_one(store, args, "TEST-1", source, out_dir)
                result = pull._pull_one(store, args, "TEST-1", source, out_dir)
                req = server.state.requests[1].headers
        self.assertEqual(result["bytes"], 10)
        self.assertEqual(Path(result["path"]).read_bytes(), body)
        self.assertEqual(req.get("range"), "bytes=5-")
        self.assertEqual(req.get("if-range"), '"pull-v1"')
        self.assertFalse(Path(str(out_dir / ".partial")).exists() and any((out_dir / ".partial").iterdir()))

if __name__ == "__main__":
    unittest.main()
