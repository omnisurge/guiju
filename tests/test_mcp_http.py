"""MCP HTTP 安全边界：Content-Type/Length、Host/Origin 门控、非回环 Bearer、超时、并发上限与 CLI 解析。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from guiju import cli
from guiju import mcp as mcp_mod
from guiju.currency.model import Record
from guiju.currency.native import Outcome
from guiju.mcp import McpServer, make_http_server


def _record(code="GB 50011-2010", status="current", *, source="csres", fallback=""):
    return Record(code, status, "建筑抗震设计标准", "2010-12-01", source, fallback=fallback)


class _FakeBackend:
    name = "fake"

    def query(self, code):
        return Outcome((), self.name)


def _serve(server, **kwargs):
    httpd = make_http_server(server, "127.0.0.1", 0, **kwargs)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port, thread


def _stop(httpd, thread):
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _send_raw(port, method, path, headers=None, body=b"", timeout=5.0):
    """极简 HTTP 客户端：返回 (status, header dict, body bytes)。

    headers 若未给 Host 默认写 127.0.0.1:port；Content-Type 默认 application/json。
    """
    headers = list(headers or [])
    names = {line.split(":", 1)[0].strip().lower() for line in headers}
    if "host" not in names:
        headers.insert(0, f"Host: 127.0.0.1:{port}")
    if method == "POST" and "content-type" not in names:
        headers.append("Content-Type: application/json")
    head = [f"{method} {path} HTTP/1.1", *headers]
    raw = ("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + body
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    payload = b"".join(chunks)
    head_bytes, _, body_bytes = payload.partition(b"\r\n\r\n")
    lines = head_bytes.split(b"\r\n")
    status = int(lines[0].split(b" ", 2)[1])
    parsed = {}
    for line in lines[1:]:
        key, _, value = line.partition(b":")
        parsed[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return status, parsed, body_bytes


def _post_raw(port, body, headers, path="/mcp"):
    """裸 socket 发 POST（http.client 没法发"缺 Content-Length"/负值）。"""
    return _send_raw(port, "POST", path, headers, body)[0]


def _post(port, payload, path="/mcp"):
    body = json.dumps(payload).encode("utf-8")
    return _post_raw(port, body, [f"Content-Length: {len(body)}"], path)


def _post_json(port, payload, headers=None, path="/mcp", timeout=5.0):
    body = json.dumps(payload).encode("utf-8")
    all_headers = [f"Content-Length: {len(body)}", *(headers or [])]
    return _send_raw(port, "POST", path, all_headers, body, timeout=timeout)


class HttpSecurityTest(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server, httpd, thread in self.servers:
            server.close()
            _stop(httpd, thread)

    def _start(self, server, **kwargs):
        httpd, port, thread = _serve(server, **kwargs)
        self.servers.append((server, httpd, thread))
        return port

    def test_missing_content_type_rejected(self):
        port = self._start(McpServer(_FakeBackend()))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        status, _, _ = _send_raw(
            port, "POST", "/mcp",
            [f"Content-Length: {len(body)}", "Content-Type: "], body)
        self.assertEqual(status, 415)

    def test_charset_content_type_accepted(self):
        port = self._start(McpServer(_FakeBackend()))
        status, _, _ = _post_json(
            port, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers=["Content-Type: application/json; charset=utf-8"])
        self.assertEqual(status, 200)

    def test_evil_origin_rejected(self):
        port = self._start(McpServer(_FakeBackend()))
        status, _, _ = _post_json(
            port, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers=["Origin: http://evil.example"])
        self.assertEqual(status, 403)

    def test_malformed_origin_rejected(self):
        port = self._start(McpServer(_FakeBackend()))
        status, _, _ = _post_json(
            port, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers=["Origin: http://[::1"])
        self.assertEqual(status, 403)

    def test_origin_null_rejected(self):
        port = self._start(McpServer(_FakeBackend()))
        status, _, _ = _post_json(
            port, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers=["Origin: null"])
        self.assertEqual(status, 403)

    def test_evil_host_rejected(self):
        port = self._start(McpServer(_FakeBackend()))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        status, _, _ = _send_raw(
            port, "POST", "/mcp",
            ["Host: evil.example", f"Content-Length: {len(body)}"], body)
        self.assertEqual(status, 403)

    def test_public_host_origin_explicitly_allowed(self):
        port = self._start(
            McpServer(_FakeBackend()),
            allowed_hosts=["guiju.example.com"],
            allowed_origins=["https://guiju.example.com"],
        )
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        status, _, _ = _send_raw(
            port, "POST", "/mcp",
            [
                "Host: guiju.example.com",
                "Origin: https://guiju.example.com",
                f"Content-Length: {len(body)}",
            ],
            body,
        )
        self.assertEqual(status, 200)

    def test_public_env_allowlist_is_used(self):
        with patch.dict(os.environ, {
            "GUIJU_MCP_ALLOWED_HOSTS": "guiju.env.example",
            "GUIJU_MCP_ALLOWED_ORIGINS": "https://guiju.env.example",
        }):
            port = self._start(McpServer(_FakeBackend()))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        status, _, _ = _send_raw(
            port, "POST", "/mcp",
            [
                "Host: guiju.env.example",
                "Origin: https://guiju.env.example",
                f"Content-Length: {len(body)}",
            ],
            body,
        )
        self.assertEqual(status, 200)

    def test_http_origin_rejected_when_only_https_origin_allowed(self):
        port = self._start(
            McpServer(_FakeBackend()),
            allowed_hosts=["guiju.example.com"],
            allowed_origins=["https://guiju.example.com"],
        )
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        status, _, _ = _send_raw(
            port, "POST", "/mcp",
            [
                "Host: guiju.example.com",
                "Origin: http://guiju.example.com",
                f"Content-Length: {len(body)}",
            ],
            body,
        )
        self.assertEqual(status, 403)

    def test_default_https_port_origin_allowed(self):
        port = self._start(
            McpServer(_FakeBackend()),
            allowed_hosts=["guiju.example.com"],
            allowed_origins=["https://guiju.example.com"],
        )
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        status, _, _ = _send_raw(
            port, "POST", "/mcp",
            [
                "Host: guiju.example.com",
                "Origin: https://guiju.example.com:443",
                f"Content-Length: {len(body)}",
            ],
            body,
        )
        self.assertEqual(status, 200)

    def test_get_mcp_returns_405(self):
        port = self._start(McpServer(_FakeBackend()))
        status, headers, _ = _send_raw(port, "GET", "/mcp")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("allow"), "POST")

    def test_health_still_works(self):
        port = self._start(McpServer(_FakeBackend()))
        status, _, body = _send_raw(port, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["ok"], True)

    def test_slow_body_times_out(self):
        port = self._start(McpServer(_FakeBackend()), request_timeout=0.2)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            raw = (
                "POST /mcp HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Content-Type: application/json\r\n"
                "Content-Length: 10\r\n\r\n"
                "{"
            ).encode()
            sock.sendall(raw)
            sock.settimeout(5)
            data = sock.recv(4096)
        self.assertIn(b" 408 ", data.split(b"\r\n", 1)[0])

    def test_worker_limit_caps_concurrent_handlers(self):
        active = 0
        peak = 0
        lock = threading.Lock()

        class SlowBackend:
            def query(self, code):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.15)
                with lock:
                    active -= 1
                return Outcome((_record(),), "slow")

        port = self._start(McpServer(SlowBackend()), max_workers=2)
        results = []

        def worker():
            status, _, _ = _post_json(
                port, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "query", "arguments": {"code": "GB50011"}}},
                timeout=10)
            results.append(status)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(results, [200, 200, 200, 200])
        self.assertLessEqual(peak, 2, f"并发线程超过上限：peak={peak}")


class TestContentLengthGuard(unittest.TestCase):
    """do_POST 不能再无条件信任客户端声明的 Content-Length。"""

    @classmethod
    def setUpClass(cls):
        cls.server = McpServer(_FakeBackend())
        cls.httpd, cls.port, cls.thread = _serve(cls.server)

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        _stop(cls.httpd, cls.thread)

    def test_oversized_content_length_rejected_413(self):
        status = _post_raw(
            self.port, b"{}",
            [f"Content-Length: {mcp_mod.MAX_BODY_BYTES + 1}"])
        self.assertEqual(status, 413)

    def test_at_limit_boundary_accepted(self):
        status = _post_raw(
            self.port, b" ",
            [f"Content-Length: {mcp_mod.MAX_BODY_BYTES}"])
        self.assertEqual(status, 400)

    def test_missing_content_length_rejected(self):
        self.assertEqual(_post_raw(self.port, b"{}", []), 400)

    def test_negative_content_length_rejected(self):
        self.assertEqual(
            _post_raw(self.port, b"{}", ["Content-Length: -1"]), 400)

    def test_non_numeric_content_length_rejected(self):
        self.assertEqual(
            _post_raw(self.port, b"{}", ["Content-Length: abc"]), 400)

    def test_normal_request_still_works(self):
        status = _post(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 200)


class TestNonLoopbackGate(unittest.TestCase):
    """B4：非回环绑定必须 --allow-remote + Bearer，不能再只 warning。"""

    def _make(self, host, **kwargs):
        err = io.StringIO()
        srv = McpServer(_FakeBackend())
        try:
            with contextlib.redirect_stderr(err):
                httpd = make_http_server(srv, host, 0, **kwargs)
            httpd.server_close()
        finally:
            srv.close()
        return err.getvalue()

    def test_non_loopback_requires_explicit_remote_gate(self):
        with self.assertRaisesRegex(ValueError, "allow-remote"):
            self._make("0.0.0.0")
        with self.assertRaisesRegex(ValueError, "GUIJU_MCP_BEARER_TOKEN"):
            self._make("0.0.0.0", allow_remote=True)
        with patch.dict(os.environ, {"GUIJU_MCP_BEARER_TOKEN": "secret"}):
            srv = McpServer(_FakeBackend())
            httpd, port, thread = _serve(srv, allow_remote=True)
            try:
                self.assertTrue(httpd.require_bearer)
                self.assertEqual(httpd.bearer_token, "secret")
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"}
                ).encode("utf-8")
                length = f"Content-Length: {len(body)}"
                self.assertEqual(_post_raw(port, body, [length]), 401)
                self.assertEqual(
                    _post_raw(port, body, [length, "Authorization: Bearer wrong"]),
                    401,
                )
                self.assertEqual(
                    _post_raw(port, body, [length, "Authorization: Bearer secret"]),
                    200,
                )
            finally:
                srv.close()
                _stop(httpd, thread)

        # 真实非回环绑定（0.0.0.0）+ Bearer：401/200 必须走同一条 Handler。
        with patch.dict(os.environ, {"GUIJU_MCP_BEARER_TOKEN": "secret"}):
            srv2 = McpServer(_FakeBackend())
            httpd2 = make_http_server(srv2, "0.0.0.0", 0, allow_remote=True)
            port = httpd2.server_address[1]
            thread2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
            thread2.start()
            try:
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
                length = f"Content-Length: {len(body)}"
                self.assertEqual(_post_raw(port, body, [length]), 401)
                self.assertEqual(
                    _post_raw(port, body, [length, "Authorization: Bearer secret"]), 200)
            finally:
                srv2.close()
                _stop(httpd2, thread2)

        # CLI 路径同样必须 exit 2，且不能泄露 traceback。
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"GUIJU_HOME": tmp, "GUIJU_MCP_BEARER_TOKEN": ""}):
            rc = cli.main(["mcp", "--http", "--allow-remote", "--host", "0.0.0.0"])
            self.assertEqual(rc, 2)

    def test_loopback_quiet(self):
        self.assertNotIn("非回环", self._make("127.0.0.1"))

    def test_loopback_detection_covers_whole_127_block(self):
        # 127.x 全段都是回环（判定函数层面测，macOS 绑不了 127.0.0.2）
        self.assertTrue(mcp_mod._is_loopback("127.0.0.2"))
        self.assertTrue(mcp_mod._is_loopback("::1"))
        self.assertTrue(mcp_mod._is_loopback("localhost"))
        self.assertFalse(mcp_mod._is_loopback("0.0.0.0"))
        self.assertFalse(mcp_mod._is_loopback("192.168.1.10"))


class CliParserTest(unittest.TestCase):
    def test_cli_parser_accepts_repeatable_allowlists(self):
        parser = cli.build_parser()
        args = parser.parse_args([
            "mcp", "--allowed-host", "a.example", "--allowed-host", "b.example",
            "--allowed-origin", "https://a.example", "--allowed-origin", "https://b.example",
        ])
        self.assertEqual(args.allowed_host, ["a.example", "b.example"])
        self.assertEqual(args.allowed_origin, ["https://a.example", "https://b.example"])


if __name__ == "__main__":
    unittest.main()
