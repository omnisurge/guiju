"""W5 MCP 修复回归（guiju 1.2.0a4，2026-11-22）。

覆盖 W5-1 batch 40 硬停止、W5-2 --db 注入、W5-3 错误/缓存告警、
W5-4 HTTP 安全（Content-Type/Host/Origin/超时/线程上限/GET 405）、
W5-5 announcement 真只读库（缺库引导、旧 schema 拒绝、损坏检测）。

所有测试离线：announcement 使用本地公告行 + api_mode 源（不抓页面），
不写真实 GUIJU_HOME（临时目录隔离）。
"""

import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from guiju import cli
from guiju.currency import MisconfiguredCurrency, NullCurrency
from guiju.currency.model import Record
from guiju.currency.native import Outcome
from guiju.errors import ComplianceDenied, NeedsHuman, NoCurrencyRecord
from guiju.mcp import McpServer, _query_result, make_http_server
from guiju.store.sqlite import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
SHZJW_URL = "https://bzjg.zjw.sh.gov.cn/api/notice/{}"


# --------------------------------------------------------------------------- helpers


def _record(code="GB 50011-2010", status="current", *, source="csres", fallback=""):
    return Record(code, status, "建筑抗震设计标准", "2010-12-01", source, fallback=fallback)


class _FakeBackend:
    name = "fake"

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = []

    def query(self, code):
        self.calls.append(code)
        value = self.rows.get(code, [])
        if isinstance(value, Exception):
            raise value
        return Outcome(tuple(value), self.name)

    def lookup(self, code):
        return list(self.query(code).results)


class _OutcomeBackend:
    """只有 query() 的新后端；lookup()/共享状态属性一旦被读就失败。"""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def query(self, code):
        self.calls.append(code)
        return self.outcomes[code]

    def lookup(self, code):
        raise AssertionError("MCP 不应在 query() 可用时回退到 lookup()")

    @property
    def stale(self):
        raise AssertionError("MCP 不应读取共享 backend.last_outcome")

    @property
    def from_cache(self):
        raise AssertionError("MCP 不应读取共享 backend.last_outcome")

    @property
    def cache_age_days(self):
        raise AssertionError("MCP 不应读取共享 backend.last_outcome")


class _RacingOutcomeBackend:
    """两个 query 交错，故意让共享 last_outcome 指向后返回的一方。"""

    def __init__(self):
        self.last_outcome = None
        self.a_set = threading.Event()
        self.b_set = threading.Event()

    def query(self, code):
        if code == "A":
            outcome = Outcome((_record("A"),), "csres", stale=True, from_cache=True, cache_age=3 * 86400)
            self.last_outcome = outcome
            self.a_set.set()
            self.b_set.wait(2)
        else:
            self.a_set.wait(2)
            outcome = Outcome((_record("B"),), "csres")
            self.last_outcome = outcome
            self.b_set.set()
        return outcome

    @property
    def stale(self):
        return bool(self.last_outcome and self.last_outcome.stale)

    @property
    def from_cache(self):
        return bool(self.last_outcome and self.last_outcome.from_cache)

    @property
    def cache_age_days(self):
        return self.last_outcome.cache_age / 86400 if self.last_outcome else None


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


def _post_json(port, payload, headers=None, path="/mcp", timeout=5.0):
    body = json.dumps(payload).encode("utf-8")
    all_headers = [f"Content-Length: {len(body)}", *(headers or [])]
    return _send_raw(port, "POST", path, all_headers, body, timeout=timeout)


def _call(server, name, arguments):
    return server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def _error_payload(result):
    return json.loads(result["content"][0]["text"])


# --------------------------------------------------------------------------- W5-1


class BatchComplianceStopTest(unittest.TestCase):
    """batch 命中 40 必须立即整批停止，且 1.3.0 彻底拒收 list/out 文件路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"GUIJU_HOME": str(self.home)}).start()
        self.server = McpServer(_FakeBackend())
        self.addCleanup(self.server.close)

    def test_compliance_denied_stops_batch_and_keeps_payload(self):
        self.server.backend = _FakeBackend({
            "A": ComplianceDenied("命中合规护栏", hint="立即停止"),
            "B": [_record()],
            "C": [_record()],
        })
        result = self.server._call("batch", {"codes": ["A", "B", "C"]})
        self.assertTrue(result["isError"])
        payload = _error_payload(result)
        self.assertEqual(payload["exit"], 40)
        self.assertEqual(payload["reason"], "compliance")
        self.assertIn("合规", payload["error"])
        self.assertEqual(self.server.backend.calls, ["A"])
        # 混传 out 必须在任何后端调用前被拒绝，更不能借 out 建 mcp 目录。
        self.server.backend = _FakeBackend({"A": [_record()], "B": [_record()]})
        rejected = self.server._call("batch", {"codes": ["A", "B"], "out": "codes-out.tsv"})
        self.assertTrue(rejected["isError"])
        self.assertIn("1.3.0", rejected["content"][0]["text"])
        self.assertEqual(self.server.backend.calls, [])
        self.assertFalse((self.home / "mcp").exists())
        # 非法/空 codes 不得静默返回空成功。
        self.assertTrue(self.server._call("batch", {"codes": []})["isError"])
        self.assertTrue(self.server._call("batch", {"codes": "A"})["isError"])

    def test_non_compliance_error_still_counted_and_continues(self):
        self.server.backend = _FakeBackend({
            "A": NoCurrencyRecord("时效未覆盖"),
            "B": [_record()],
            "C": NoCurrencyRecord("时效未覆盖"),
        })
        result = self.server._call("batch", {"codes": ["A", "B", "C"]})
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("总数 3", text)
        self.assertIn("完成 1", text)
        self.assertIn("失败 2", text)
        self.assertEqual(self.server.backend.calls, ["A", "B", "C"])

    def test_compliance_from_tool_call_has_json_error_payload(self):
        self.server.backend = _FakeBackend({"A": ComplianceDenied("blocked")})
        response = _call(self.server, "batch", {"codes": ["A"]})
        self.assertTrue(response["result"]["isError"])
        payload = _error_payload(response["result"])
        self.assertEqual(payload["exit"], 40)

    def test_list_or_out_arguments_are_rejected_without_backend_calls(self):
        self.server.backend = _FakeBackend({"A": [_record()]})
        for args in (
            {"list": "list.txt"},
            {"out": "out.tsv"},
            {"codes": ["A"], "list": "list.txt"},
        ):
            with self.subTest(args=args):
                res = _call(self.server, "batch", args)["result"]
                self.assertTrue(res["isError"])
                self.assertIn("codes[]", res["content"][0]["text"])
        self.assertEqual(self.server.backend.calls, [])
        self.assertFalse((self.home / "mcp").exists())


class StrictOutcomeBackendContractTest(unittest.TestCase):
    """S4：删除 lookup() 兼容路径，强制后端提供 query(code) -> Outcome。"""

    def test_lookup_only_backend_rejected_everywhere(self):
        class LookupOnly:
            def lookup(self, code):
                return [_record()]

        with self.assertRaises(TypeError):
            McpServer(LookupOnly())
        with self.assertRaises(TypeError):
            _query_result(LookupOnly(), "GB50011")

    def test_query_without_results_attribute_rejected(self):
        class NonOutcomeBackend:
            def query(self, code):
                return [_record()]

        with self.assertRaises(TypeError):
            _query_result(NonOutcomeBackend(), "GB50011")
        server = McpServer(NonOutcomeBackend())
        self.addCleanup(server.close)
        resp = _call(server, "query", {"code": "GB50011"})["result"]
        self.assertTrue(resp["isError"])

    def test_null_and_misconfigured_currencies_work_under_strict_contract(self):
        null_server = McpServer(NullCurrency())
        self.addCleanup(null_server.close)
        q = _call(null_server, "query", {"code": "GB50011"})["result"]
        self.assertFalse(q["isError"])
        self.assertEqual(q["content"][0]["text"], "无命中")
        v = _call(null_server, "verify", {"code": "GB50011"})["result"]
        self.assertEqual(v["content"][0]["text"], "unknown")
        b = _call(null_server, "batch", {"codes": ["GB50011"]})["result"]
        self.assertIn("无源 1", b["content"][0]["text"])

        bad_server = McpServer(MisconfiguredCurrency("broken", "配置损坏"))
        self.addCleanup(bad_server.close)
        err = _call(bad_server, "query", {"code": "GB50011"})["result"]
        self.assertTrue(err["isError"])
        self.assertEqual(_error_payload(err)["exit"], 20)


# --------------------------------------------------------------------------- W5-3


class ErrorPayloadTest(unittest.TestCase):
    """query/verify 的错误必须是完整 exc.payload()，不是仅 message。"""

    def _server(self, error):
        server = McpServer(_FakeBackend({"X": error}))
        self.addCleanup(server.close)
        return server

    def test_query_no_currency_record_payload(self):
        result = _call(self._server(NoCurrencyRecord("编号没有记录")), "query", {"code": "X"})["result"]
        self.assertTrue(result["isError"])
        payload = _error_payload(result)
        self.assertEqual(payload["exit"], 34)
        self.assertEqual(payload["reason"], "no_currency_record")
        self.assertIn("hint", payload)
        self.assertFalse(payload["ok"])

    def test_verify_needs_human_payload(self):
        result = _call(self._server(NeedsHuman("站点改版")), "verify", {"code": "X"})["result"]
        payload = _error_payload(result)
        self.assertEqual(payload["exit"], 50)
        self.assertEqual(payload["reason"], "needs_human")

    def test_query_ambiguous_code_keeps_candidates(self):
        from guiju.errors import AmbiguousCode

        exc = AmbiguousCode(
            "编号存在歧义",
            candidates=[
                {"code": "GB 1-2020", "status": "current"},
                {"code": "GB 1-2010", "status": "superseded"},
            ],
        )
        server = McpServer(_FakeBackend({"GB1": exc}))
        self.addCleanup(server.close)
        result = _call(server, "query", {"code": "GB1"})["result"]
        payload = _error_payload(result)
        self.assertEqual(payload["exit"], 32)
        self.assertEqual(payload["reason"], "ambiguous_code")
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertEqual(payload["candidates"][0]["code"], "GB 1-2020")

    def test_query_source_unreachable_payload_has_exit_20(self):
        from guiju.errors import SourceUnreachable

        result = _call(self._server(SourceUnreachable("超时")), "query", {"code": "X"})["result"]
        payload = _error_payload(result)
        self.assertEqual(payload["exit"], 20)
        self.assertEqual(payload["reason"], "unreachable")


class CacheWarningTest(unittest.TestCase):
    """stale/from_cache 必须上报，但 verify 的 content[0] 必须保持精确 code\\tstatus。"""

    class CacheBackend:
        def query(self, code):
            return Outcome(
                (_record(),),
                "csres",
                stale=True,
                from_cache=True,
                cache_age=9.5 * 86400,
            )

    def test_verify_content_zero_exact_and_second_item_reports_cache(self):
        server = McpServer(self.CacheBackend())
        self.addCleanup(server.close)
        result = _call(server, "verify", {"code": "GB50011"})["result"]
        self.assertEqual(result["content"][0]["text"], "GB 50011-2010\tcurrent\n")
        self.assertGreaterEqual(len(result["content"]), 2, "stale 时必须追加告警 content")
        meta = json.loads(result["content"][1]["text"])
        self.assertTrue(meta["stale"])
        self.assertTrue(meta["from_cache"])
        self.assertEqual(meta["cache_age_days"], 9.5)

        # Outcome 路径：完整性和备源告警也必须随 content[1] 透传。
        outcome = Outcome(
            (_record(source="csres", fallback="ccsn"),),
            "csres",
            complete=False,
            incomplete_reason="明文回退，结果不完整",
        )
        outcome_server = McpServer(_OutcomeBackend({"GB50011": outcome}))
        self.addCleanup(outcome_server.close)
        outcome_result = _call(outcome_server, "verify", {"code": "GB50011"})["result"]
        self.assertEqual(outcome_result["content"][0]["text"], "GB 50011-2010\tcurrent\n")
        outcome_meta = json.loads(outcome_result["content"][1]["text"])
        self.assertFalse(outcome_meta["complete"])
        self.assertEqual(outcome_meta["incomplete_reason"], "明文回退，结果不完整")
        self.assertEqual(outcome_meta["fallback_source"], "ccsn")
        # batch 也必须把同一 Outcome 的完整性/备源状态作为第二条 JSON content。
        batch_server = McpServer(_OutcomeBackend({"GB50011": outcome}))
        self.addCleanup(batch_server.close)
        batch_result = _call(batch_server, "batch", {"codes": ["GB50011"]})["result"]
        self.assertFalse(batch_result["isError"])
        batch_meta = json.loads(batch_result["content"][1]["text"])
        self.assertEqual(batch_meta["warnings"][0]["code"], "GB50011")
        self.assertFalse(batch_meta["warnings"][0]["complete"])
        self.assertEqual(batch_meta["warnings"][0]["fallback_source"], "ccsn")

    def test_query_appends_warning_at_end(self):
        server = McpServer(self.CacheBackend())
        self.addCleanup(server.close)
        result = _call(server, "query", {"code": "GB50011"})["result"]
        text = result["content"][0]["text"]
        self.assertTrue(text.startswith("GB 50011-2010\tcurrent\t"))
        last = text.rstrip().splitlines()[-1]
        self.assertIn("stale", last)
        self.assertIn("9.5", last)

        # Outcome 路径：从不可变对象读取，不碰共享 backend 状态。
        outcome = Outcome((_record(),), "csres", from_cache=True, cache_age=9.5 * 86400)
        outcome_server = McpServer(_OutcomeBackend({"GB50011": outcome}))
        self.addCleanup(outcome_server.close)
        outcome_text = _call(outcome_server, "query", {"code": "GB50011"})["result"]["content"][0]["text"]
        self.assertIn("# 缓存告警", outcome_text)
        outcome_meta = json.loads(outcome_text.split("# 缓存告警 ", 1)[1])
        self.assertTrue(outcome_meta["from_cache"])
        self.assertEqual(outcome_meta["cache_age_days"], 9.5)

        # 两请求交错：A 的 stale 不能被 B 的 fresh 覆盖。
        racing = McpServer(_RacingOutcomeBackend())
        self.addCleanup(racing.close)
        results = {}

        def query(code):
            results[code] = _call(racing, "query", {"code": code})["result"]["content"][0]["text"]

        first = threading.Thread(target=query, args=("A",))
        second = threading.Thread(target=query, args=("B",))
        first.start()
        second.start()
        first.join(2)
        second.join(2)
        self.assertIn("# 缓存告警", results["A"])
        self.assertNotIn("# 缓存告警", results["B"])

    def test_backend_without_cache_attributes_has_no_warning(self):
        server = McpServer(_FakeBackend({"GB50011": [_record()]}))
        self.addCleanup(server.close)
        verify = _call(server, "verify", {"code": "GB50011"})["result"]
        self.assertEqual(len(verify["content"]), 1)
        self.assertEqual(verify["content"][0]["text"], "GB 50011-2010\tcurrent\n")
        query = _call(server, "query", {"code": "GB50011"})["result"]
        self.assertNotIn("stale", query["content"][0]["text"])

        clean = McpServer(_OutcomeBackend({"GB50011": Outcome((_record(),), "csres")}))
        self.addCleanup(clean.close)
        clean_query = _call(clean, "query", {"code": "GB50011"})["result"]
        self.assertEqual(len(clean_query["content"]), 1)
        self.assertNotIn("# 缓存告警", clean_query["content"][0]["text"])


# --------------------------------------------------------------------------- W5-4


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
        with mock.patch.dict(os.environ, {
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


# --------------------------------------------------------------------------- W5-2


class CliParserTest(unittest.TestCase):
    def test_cli_parser_accepts_repeatable_allowlists(self):
        parser = cli.build_parser()
        args = parser.parse_args([
            "mcp", "--allowed-host", "a.example", "--allowed-host", "b.example",
            "--allowed-origin", "https://a.example", "--allowed-origin", "https://b.example",
        ])
        self.assertEqual(args.allowed_host, ["a.example", "b.example"])
        self.assertEqual(args.allowed_origin, ["https://a.example", "https://b.example"])


class CliDbInjectionTest(unittest.TestCase):
    """--db 必须传给 McpServer，announcement 读的是同一个库。"""

    def _seed(self, db: Path, ann_id_url: str = SHZJW_URL.format(42)) -> int:
        store = Store(db)
        try:
            ann_id, _ = store.upsert_announcement(
                ann_id_url, "2024-01-05", "公告标题", "某标准", "other")
            store.upsert_edition("DG/TJ08-2007-2026", "DG/TJ08-2007-2026", None, "parsed")
            store.add_mention(ann_id, "DG/TJ08-2007-2026", "primary", "发布")
            store.commit()
        finally:
            store.close()
        return ann_id

    def test_cli_passes_db_path_and_reads_custom_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "custom.db"
            ann_id = self._seed(db)
            captured = {}

            def fake_serve(server):
                captured["server"] = server
                return 0

            with mock.patch.dict(
                    os.environ,
                    {"GUIJU_HOME": str(root / "home"), "GUIJU_NO_AUTO_SYNC": "1"}), \
                    mock.patch("guiju.mcp.serve_stdio", side_effect=fake_serve):
                rc = cli.main(["--db", str(db), "mcp"])
            self.assertEqual(rc, 0)
            server = captured["server"]
            try:
                self.assertEqual(Path(server.store_path), db)
                response = _call(server, "announcement", {"ref": str(ann_id)})
                payload = json.loads(response["result"]["content"][0]["text"])
                self.assertFalse(response["result"]["isError"])
                self.assertEqual(payload["announcement_id"], ann_id)
                self.assertEqual(payload["source"], "shzjw")
            finally:
                server.close()

    def test_default_library_unaffected_without_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            default_db = home / "catalog.db"
            ann_id = self._seed(default_db, SHZJW_URL.format(7))
            captured = {}

            def fake_serve(server):
                captured["server"] = server
                return 0

            with mock.patch.dict(
                    os.environ,
                    {"GUIJU_HOME": str(home), "GUIJU_NO_AUTO_SYNC": "1"}), \
                    mock.patch("guiju.mcp.serve_stdio", side_effect=fake_serve):
                rc = cli.main(["mcp"])
            self.assertEqual(rc, 0)
            server = captured["server"]
            try:
                self.assertEqual(Path(server.store_path), default_db)
                ok = _call(server, "announcement", {"ref": str(ann_id)})
                self.assertFalse(ok["result"]["isError"])
                missing = _call(server, "announcement", {"ref": "42"})
                self.assertTrue(missing["result"]["isError"])
                self.assertEqual(_error_payload(missing["result"])["exit"], 31)
            finally:
                server.close()

    def test_subprocess_cli_db_announcement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "sub.db"
            ann_id = self._seed(db)
            payloads = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "announcement", "arguments": {"ref": str(ann_id)}}},
            ]
            env = dict(os.environ)
            env.update({"GUIJU_HOME": str(root / "home"), "GUIJU_NO_AUTO_SYNC": "1"})
            proc = subprocess.run(
                [sys.executable, "-m", "guiju", "--db", str(db), "mcp"],
                input="\n".join(json.dumps(item, ensure_ascii=False) for item in payloads) + "\n",
                capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            response = next(item for item in responses if item.get("id") == 2)
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(payload["announcement_id"], ann_id)


# --------------------------------------------------------------------------- W5-5


class ReadOnlyAnnouncementStoreTest(unittest.TestCase):
    """announcement 的库必须是结构只读；旧 schema/损坏库显式 50，不替用户迁移。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = self.home / "catalog.db"
        patcher = mock.patch.dict(
            os.environ,
            {"GUIJU_HOME": str(self.home), "GUIJU_NO_AUTO_SYNC": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = McpServer(_FakeBackend())
        self.addCleanup(self.server.close)

    def _seed(self, url: str = SHZJW_URL.format(42)) -> int:
        store = Store(self.db)
        try:
            ann_id, _ = store.upsert_announcement(
                url, "2024-01-05", "公告标题", "某标准", "other")
            store.upsert_edition("DG/TJ08-2007-2026", "DG/TJ08-2007-2026", None, "parsed")
            store.add_mention(ann_id, "DG/TJ08-2007-2026", "primary", "发布")
            store.commit()
        finally:
            store.close()
        return ann_id

    def _tables(self) -> set:
        db = sqlite3.connect(self.db)
        try:
            return {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        finally:
            db.close()

    def _assert_announcement(self, ref, expected_id):
        response = _call(self.server, "announcement", {"ref": str(ref)})
        self.assertFalse(response["result"]["isError"], response)
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["announcement_id"], expected_id)

    def _assert_error(self, ref, exit_code):
        response = _call(self.server, "announcement", {"ref": str(ref)})
        self.assertTrue(response["result"]["isError"])
        payload = _error_payload(response["result"])
        self.assertEqual(payload["exit"], exit_code, payload)
        return payload

    def test_missing_db_bootstraps_empty_schema_and_returns_31(self):
        self._assert_error(1, 31)
        self.assertTrue(self.db.exists())
        self.assertIn("announcement", self._tables())
        store = Store(self.db)
        try:
            self.assertEqual(store.count_announcements(), 0)
        finally:
            store.close()

    def test_zero_byte_db_bootstraps(self):
        self.db.write_bytes(b"")
        self._assert_error(1, 31)
        self.assertIn("announcement", self._tables())

    def test_empty_db_without_tables_bootstraps(self):
        db = sqlite3.connect(self.db)
        db.execute("PRAGMA user_version = 0")
        db.commit()
        db.close()
        self._assert_error(1, 31)
        self.assertIn("announcement", self._tables())

    def test_existing_v2_db_is_readable(self):
        ann_id = self._seed()
        self._assert_announcement(ann_id, ann_id)

    def test_v0_with_tables_is_needs_human_and_not_rebuilt(self):
        self.db.unlink(missing_ok=True)
        db = sqlite3.connect(self.db)
        db.execute("CREATE TABLE announcement (id INTEGER PRIMARY KEY, url TEXT)")
        db.execute("PRAGMA user_version = 0")
        db.commit()
        db.close()
        before = self.db.read_bytes()
        payload = self._assert_error(1, 50)
        self.assertIn("schema", payload["error"])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(list(self.home.glob("catalog.db.legacy-*")), [])

    def test_v2_missing_core_table_is_needs_human(self):
        self._seed()
        db = sqlite3.connect(self.db)
        db.execute("DROP TABLE edition")
        db.commit()
        db.close()
        payload = self._assert_error(1, 50)
        self.assertIn("edition", payload["error"])
        self.assertNotIn("edition", self._tables())

    def test_v2_only_missing_currency_cache_is_readable(self):
        ann_id = self._seed()
        db = sqlite3.connect(self.db)
        db.execute("DROP TABLE IF EXISTS currency_cache")
        db.commit()
        db.close()
        self._assert_announcement(ann_id, ann_id)
        self.assertNotIn("currency_cache", self._tables(), "只读路径不得补 DDL")

    def test_old_v2_without_mention_confidence_reads_edition_fallback(self):
        ann_id = self._seed()
        db = sqlite3.connect(self.db)
        try:
            db.execute("ALTER TABLE mention DROP COLUMN confidence")
            db.commit()
        except sqlite3.OperationalError as exc:  # pragma: no cover - 老 SQLite
            db.close()
            self.skipTest(f"SQLite 不支持 DROP COLUMN：{exc}")
        finally:
            with contextlib.suppress(sqlite3.ProgrammingError):
                db.close()
        ro = self.server._ann_store()
        rows = ro.mentions_for(ann_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["confidence"], "parsed")
        self._assert_announcement(ann_id, ann_id)

    def test_unknown_schema_version_is_needs_human(self):
        self._seed()
        db = sqlite3.connect(self.db)
        db.execute("PRAGMA user_version = 999")
        db.commit()
        db.close()
        payload = self._assert_error(1, 50)
        self.assertIn("999", payload["error"])

    def test_corrupt_file_is_needs_human(self):
        self.db.write_bytes(b"this is not a sqlite database at all" * 10)
        payload = self._assert_error(1, 50)
        self.assertIn("数据库", payload["error"])

    def test_announcement_does_not_mutate_mentions(self):
        ann_id = self._seed()
        store = Store(self.db)
        try:
            before = (store.count_announcements(), store.stats()["mentions"], store.stats()["editions"])
        finally:
            store.close()
        self._assert_announcement(ann_id, ann_id)
        store = Store(self.db)
        try:
            after = (store.count_announcements(), store.stats()["mentions"], store.stats()["editions"])
        finally:
            store.close()
        self.assertEqual(before, after)

    def test_direct_delete_on_underlying_db_is_rejected(self):
        self._seed()
        ro = self.server._ann_store()
        with self.assertRaises(AttributeError):
            _ = ro.db
        with self.assertRaises(sqlite3.OperationalError):
            ro._inner.db.execute("DELETE FROM announcement")
        self.assertEqual(ro.count_announcements(), 1)

    def test_readonly_facade_keeps_blocking_known_writes(self):
        ann_id = self._seed()
        ro = self.server._ann_store()
        for attr in (
            "upsert_announcement",
            "upsert_edition",
            "add_mention",
            "mark_scanned",
            "persist_codes",
            "log_probe",
            "record_body_hash",
            "set_meta",
            "commit",
            "delete_all",
        ):
            with self.assertRaises(AttributeError, msg=attr):
                getattr(ro, attr)
        self.assertEqual(ro.count_announcements(), 1)
        self.assertIsNotNone(ro.get_ann(ann_id))
        self.assertEqual(len(ro.mentions_for(ann_id)), 1)

    def test_readonly_parent_directory_uses_immutable(self):
        # 父目录 0555 时 SQLite 无法建 -shm；_schema_snapshot 必须用 immutable=1，
        # 否则有效 v2 库会被误判 50。
        ann_id = self._seed()
        os.chmod(self.home, 0o555)
        self.addCleanup(os.chmod, self.home, 0o755)
        self._assert_announcement(ann_id, ann_id)

    def test_readonly_parent_with_pending_wal_is_needs_human(self):
        self._seed()
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"pending-wal")
        os.chmod(self.home, 0o555)
        self.addCleanup(os.chmod, self.home, 0o755)
        payload = self._assert_error(1, 50)
        self.assertIn("WAL", payload["error"])

    def test_announcement_detail_network_does_not_block_concurrent_announcement(self):
        mohurd_id = self._seed("https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2026/art_17339_1.html")
        shzjw_id = self._seed(SHZJW_URL.format(42))
        entered = threading.Event()
        release = threading.Event()

        class _SlowMohurd:
            id = "mohurd"
            api_mode = False

            def detail(self, _url):
                entered.set()
                release.wait(timeout=3)
                return "<html></html>", "正文"

            def attachments(self, _html):
                return []

        class _FastShzjw:
            id = "shzjw"
            api_mode = True

        res_a = {}
        with mock.patch(
            "guiju.sources.registry",
            return_value={"mohurd": _SlowMohurd(), "shzjw": _FastShzjw()},
        ):
            thread_a = threading.Thread(
                target=lambda: res_a.update(
                    _call(self.server, "announcement", {"ref": str(mohurd_id)})
                ),
                daemon=True,
            )
            thread_a.start()
            try:
                self.assertTrue(entered.wait(timeout=2), "线程 A 未进入 detail()")
                # 线程 A 挂起在 detail() 网络 I/O 时，线程 B 的 announcement 不得被 _store_lock 阻塞。
                res_b = _call(self.server, "announcement", {"ref": str(shzjw_id)})
                self.assertFalse(res_b["result"]["isError"], res_b)
                payload_b = json.loads(res_b["result"]["content"][0]["text"])
                self.assertEqual(payload_b["announcement_id"], shzjw_id)
                self.assertEqual(payload_b["attachment_verdict"], "api_mode")
            finally:
                release.set()
                thread_a.join(timeout=3)
        self.assertFalse(thread_a.is_alive())
        self.assertIn("result", res_a)
        payload_a = json.loads(res_a["result"]["content"][0]["text"])
        self.assertEqual(payload_a["announcement_id"], mohurd_id)

if __name__ == "__main__":
    unittest.main()
