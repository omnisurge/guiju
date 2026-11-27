"""guiju MCP 核心协议、崩溃隔离、stdio、纯净化 batch（codes[]）、Outcome 契约与缓存告警测试。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from guiju.currency import MisconfiguredCurrency, NullCurrency
from guiju.currency.model import Record
from guiju.currency.native import Outcome
from guiju.domain import atlas as atlas_domain
from guiju.errors import AmbiguousCode, ComplianceDenied, NeedsHuman, NoCurrencyRecord, SourceUnreachable
from guiju.mcp import McpServer, _query_result, make_http_server, serve_stdio
from guiju.store.atlas import AtlasCatalogStore
from guiju.store.sqlite import Store


def _record(code="GB 50011-2010", status="current", *, source="csres", fallback=""):
    return Record(code, status, "建筑抗震设计标准", "2010-12-01", source, fallback=fallback)


class FakeBackend:
    name = "fake"

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = []

    def query(self, code):
        self.calls.append(code)
        value = self.rows.get(code, ())
        if isinstance(value, Exception):
            raise value
        return Outcome(tuple(value), self.name)

    def lookup(self, code):
        return list(self.query(code).results)


class CrashingBackend:
    """query 直接抛运行时错误（模拟 sqlite3.Error / RuntimeError 等）。"""

    def query(self, code):
        raise RuntimeError("simulated backend crash")


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


def _call(server, name, arguments):
    return server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def _error_payload(result):
    return json.loads(result["content"][0]["text"])


class McpProtocolTest(unittest.TestCase):
    def setUp(self):
        self.rows = {
            "GB50011": [
                Record("GB 50011-2001", "superseded", "建筑抗震设计规范", "2002-01-01", "csres"),
                Record("GB/T 50011-2010", "current", "建筑抗震设计标准", "2010-12-01", "csres"),
            ],
            "DG/TJ08-2007": [Record("DG/TJ08-2007-2026", "current", "地质灾害危险性评估技术标准", "2027-02-01", "shzjw")],
        }
        self.server = McpServer(FakeBackend(self.rows))
        self.addCleanup(self.server.close)

    def call(self, method, params=None, request_id=1, include_id=True):
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if include_id:
            payload["id"] = request_id
        return self.server.handle(payload)

    def test_initialize_and_tools_list(self):
        init = self.call("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(init["result"]["serverInfo"]["name"], "guiju")
        tools = self.call("tools/list")["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["query", "verify", "batch", "announcement", "atlas_search"],
        )
        by_name = {tool["name"]: tool for tool in tools}
        self.assertIn("图集", by_name["query"]["description"])
        self.assertIn("上海地标", by_name["query"]["description"])
        self.assertIn("国标", by_name["query"]["description"])
        self.assertIn("atlas-catalog.json", by_name["atlas_search"]["description"])
        batch_tool = by_name["batch"]
        self.assertEqual(set(batch_tool["inputSchema"]["properties"]), {"codes"})
        self.assertEqual(batch_tool["inputSchema"]["required"], ["codes"])

    def test_query_and_verify_text_contract(self):
        query = self.call("tools/call", {"name": "query", "arguments": {"code": "GB50011"}})
        self.assertFalse(query["result"]["isError"])
        text = query["result"]["content"][0]["text"]
        self.assertIn("GB 50011-2001\tsuperseded\t建筑抗震设计规范\t2002-01-01\tcsres", text)
        self.assertIn("GB/T 50011-2010\tcurrent", text)
        verify = self.call("tools/call", {"name": "verify", "arguments": {"code": "GB50011"}})
        self.assertEqual(
            verify["result"]["content"][0]["text"],
            "GB 50011-2001\tsuperseded\nGB/T 50011-2010\tcurrent\n",
        )
        missing = self.call("tools/call", {"name": "verify", "arguments": {"code": "NOPE"}})
        self.assertEqual(missing["result"]["content"][0]["text"], "unknown")

    def test_tool_schemas_do_not_advertise_unused_cache(self):
        # P3：cache 参数没有实现，不能继续在 inputSchema 里假装支持。
        tools = self.call("tools/list")["result"]["tools"]
        for tool in tools:
            self.assertNotIn("cache", tool["inputSchema"]["properties"], tool["name"])

    def test_missing_arguments_are_tool_errors(self):
        for name in ("query", "verify", "batch", "announcement"):
            response = self.call("tools/call", {"name": name, "arguments": {}})
            self.assertTrue(response["result"]["isError"], name)

    def test_announcement_returns_metadata_without_download(self):
        from guiju.application import resolve as lookup_mod

        ann = {
            "id": 7,
            "url": "https://www.mohurd.gov.cn/gov/zhengce/x/7.html",
            "published_on": "2024-01-05",
            "title": "《工程结构通用规范》发布公告",
            "doc_name": "工程结构通用规范",
            "category": "national",
        }
        target = {
            "ann": ann, "code_key": "GB55001-2021", "code_display": "GB 55001-2021",
            "confidence": "parsed", "status": "current",
        }

        class FakeSource:
            id = "mohurd"
            api_mode = False

            def detail(self, url):
                self.detail_calls = getattr(self, "detail_calls", 0) + 1
                return ("<html>body</html>", "正文")

            def attachments(self, html):
                from guiju.domain.attachment import Attachment

                return [Attachment(url="https://www.mohurd.gov.cn/file/gb55001.pdf", label="正文")]

        source = FakeSource()
        with tempfile.TemporaryDirectory() as home, \
                mock.patch.dict("os.environ", {"GUIJU_HOME": home}), \
                mock.patch.object(lookup_mod, "resolve", return_value=target), \
                mock.patch("guiju.sources.registry", return_value={"mohurd": source}):
            response = self.call(
                "tools/call", {"name": "announcement", "arguments": {"ref": "GB 55001-2021"}})
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertFalse(response["result"]["isError"])
            self.assertEqual(payload["announcement_id"], 7)
            self.assertEqual(payload["code"], "GB 55001-2021")
            self.assertEqual(payload["attachment_url"], "https://www.mohurd.gov.cn/file/gb55001.pdf")
            # 只回元数据与链接：不得下载或落盘文件
            self.assertEqual(
                [f for f in os.listdir(home) if f.endswith(".pdf")], [])

    def test_notifications_and_unknown_methods(self):
        self.assertIsNone(self.call("notifications/initialized", include_id=False))
        self.assertIsNone(self.call("notifications/unknown", include_id=False))
        self.assertIsNone(self.call("unknown/notify", include_id=False))
        unknown = self.call("unknown/method")
        self.assertEqual(unknown["error"]["code"], -32601)
        unknown_zero = self.call("unknown/method", request_id=0)
        self.assertEqual(unknown_zero["id"], 0)
        self.assertEqual(unknown_zero["error"]["code"], -32601)

    def test_http_transport_round_trip(self):
        httpd = make_http_server(self.server, "127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": "query", "arguments": {"code": "GB50011"}}}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/mcp", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode())
            self.assertIn("GB/T 50011-2010", payload["result"]["content"][0]["text"])
            notify_body = json.dumps({"jsonrpc": "2.0", "method": "unknown/notify", "params": {}}).encode()
            notify_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/mcp", data=notify_body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(notify_req, timeout=3) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(response.read(), b"")
            err_body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}).encode()
            err_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/mcp", data=err_body,
                headers={"Content-Type": "application/json"}, method="POST")
            with mock.patch.object(self.server, "handle", side_effect=RuntimeError("boom")), \
                    urllib.request.urlopen(err_req, timeout=3) as response:
                self.assertEqual(response.status, 200)
                err_payload = json.loads(response.read().decode())
            self.assertEqual(err_payload["id"], 9)
            self.assertEqual(err_payload["error"]["code"], -32603)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


class McpCrashIsolationTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(CrashingBackend())
        self.addCleanup(self.server.close)

    def _call(self, name, arguments):
        payload = {"jsonrpc": "2.0", "method": "tools/call", "id": 7,
                   "params": {"name": name, "arguments": arguments}}
        return self.server.handle(payload)

    def test_tool_exception_returns_error_result(self):
        resp = self._call("query", {"code": "GB 50011"})
        self.assertIsNotNone(resp, "handle 不得抛异常/返回 None")
        self.assertNotIn("error", resp, "协议层必须正常响应，错误应在 result.isError")
        result = resp["result"]
        self.assertTrue(result.get("isError"), "工具内部异常必须 isError=true")
        text = result["content"][0]["text"]
        self.assertIn("内部错误", text)

    def test_server_survives_after_crash(self):
        self._call("query", {"code": "X"})
        self._call("ping", {})
        ping = self.server.handle({"jsonrpc": "2.0", "method": "ping", "id": 9})
        self.assertEqual(ping.get("result"), {})

    def test_http_do_post_survives_tool_crash(self):
        httpd = make_http_server(self.server, "127.0.0.1", 0)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            body = json.dumps({
                "jsonrpc": "2.0", "method": "tools/call", "id": 1,
                "params": {"name": "query", "arguments": {"code": "GB"}},
            }).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/mcp", data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(data["result"].get("isError"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=5)


class McpBatchInlineOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict("os.environ", {"GUIJU_HOME": str(self.home)}).start()
        self.server = McpServer(FakeBackend({
            "GB50011": [Record("GB 50011-2010", "current", "建筑抗震设计标准", "2010-12-01", "csres")]
        }))
        self.addCleanup(self.server.close)

    def test_batch_rejects_list_and_out_without_touching_filesystem(self):
        cases = (
            {"list": "list.txt", "out": "out.txt"},
            {"list": "/etc/hosts"},
            {"list": "../outside.txt"},
            {"list": ""},
            {"out": "out.txt"},
            {"out": "/etc/evil.tsv"},
            {"out": ""},
            {"codes": ["GB50011"], "list": "list.txt"},
            {"codes": ["GB50011"], "out": "out.tsv"},
        )
        for args in cases:
            with self.subTest(args=args):
                result = self.server._batch(args)
                self.assertTrue(result["isError"])
                text = result["content"][0]["text"]
                self.assertIn("1.3.0", text)
                self.assertIn("codes[]", text)
        self.assertEqual(self.server.backend.calls, [])
        self.assertFalse((self.home / "mcp").exists())

    def test_batch_inline_codes_succeeds_without_filesystem_side_effects(self):
        result = self.server._batch({"codes": ["GB50011"]})
        self.assertFalse(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("总数 1", text)
        self.assertIn("完成 1", text)
        self.assertNotIn("盘点表", text)
        self.assertEqual(self.server.backend.calls, ["GB50011"])
        self.assertFalse((self.home / "mcp").exists())

    def test_batch_rejects_missing_empty_or_non_string_codes(self):
        for args in (
            {},
            {"codes": None},
            {"codes": []},
            {"codes": ["   ", ""]},
            {"codes": "GB50011"},
            {"codes": [123]},
        ):
            with self.subTest(args=args):
                result = self.server._batch(args)
                self.assertTrue(result["isError"])
        self.assertEqual(self.server.backend.calls, [])


class McpStdioTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(FakeBackend())
        self.addCleanup(self.server.close)

    def test_non_dict_params_are_jsonrpc_errors(self):
        for method in ("initialize", "tools/call"):
            response = self.server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": []})
            self.assertEqual(response["error"]["code"], -32602, method)

    def test_stdio_continues_after_non_dict_params(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        ]
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO("\n".join(lines) + "\n")), \
                mock.patch.object(sys, "stdout", stdout):
            self.assertEqual(serve_stdio(self.server), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32602)
        self.assertEqual(responses[1]["result"], {})

    def test_params_null_and_omitted_are_fine(self):
        initialized = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": None})
        self.assertIn("protocolVersion", initialized["result"])
        pinged = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(pinged["result"], {})

    def test_invalid_params_notification_returns_none(self):
        self.assertIsNone(self.server.handle(
            {"jsonrpc": "2.0", "method": "tools/call", "params": []}))

    def test_stdio_catches_handler_exception(self):
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(self.server, "handle", side_effect=[
                RuntimeError("boom"), {"jsonrpc": "2.0", "id": 2, "result": {}}]), \
                mock.patch.object(sys, "stdin", io.StringIO("\n".join(lines) + "\n")), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(serve_stdio(self.server), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32603)
        self.assertEqual(responses[1]["result"], {})
        self.assertIn("boom", stderr.getvalue())


class BatchComplianceStopTest(unittest.TestCase):
    """batch 命中 40 必须立即整批停止，且 1.3.0 彻底拒收 list/out 文件路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"GUIJU_HOME": str(self.home)}).start()
        self.server = McpServer(FakeBackend())
        self.addCleanup(self.server.close)

    def test_compliance_denied_stops_batch_and_keeps_payload(self):
        self.server.backend = FakeBackend({
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
        self.server.backend = FakeBackend({"A": [_record()], "B": [_record()]})
        rejected = self.server._call("batch", {"codes": ["A", "B"], "out": "codes-out.tsv"})
        self.assertTrue(rejected["isError"])
        self.assertIn("1.3.0", rejected["content"][0]["text"])
        self.assertEqual(self.server.backend.calls, [])
        self.assertFalse((self.home / "mcp").exists())
        # 非法/空 codes 不得静默返回空成功。
        self.assertTrue(self.server._call("batch", {"codes": []})["isError"])
        self.assertTrue(self.server._call("batch", {"codes": "A"})["isError"])

    def test_non_compliance_error_still_counted_and_continues(self):
        self.server.backend = FakeBackend({
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
        self.server.backend = FakeBackend({"A": ComplianceDenied("blocked")})
        response = _call(self.server, "batch", {"codes": ["A"]})
        self.assertTrue(response["result"]["isError"])
        payload = _error_payload(response["result"])
        self.assertEqual(payload["exit"], 40)

    def test_list_or_out_arguments_are_rejected_without_backend_calls(self):
        self.server.backend = FakeBackend({"A": [_record()]})
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


class ErrorPayloadTest(unittest.TestCase):
    """query/verify 的错误必须是完整 exc.payload()，不是仅 message。"""

    def _server(self, error):
        server = McpServer(FakeBackend({"X": error}))
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
        exc = AmbiguousCode(
            "编号存在歧义",
            candidates=[
                {"code": "GB 1-2020", "status": "current"},
                {"code": "GB 1-2010", "status": "superseded"},
            ],
        )
        server = McpServer(FakeBackend({"GB1": exc}))
        self.addCleanup(server.close)
        result = _call(server, "query", {"code": "GB1"})["result"]
        payload = _error_payload(result)
        self.assertEqual(payload["exit"], 32)
        self.assertEqual(payload["reason"], "ambiguous_code")
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertEqual(payload["candidates"][0]["code"], "GB 1-2020")

    def test_query_source_unreachable_payload_has_exit_20(self):
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
        server = McpServer(FakeBackend({"GB50011": [_record()]}))
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


class AtlasMcpSupportTest(unittest.TestCase):
    """标准图集 replaced_by 透传与只读 atlas_search 工具契约。"""

    def test_query_verify_batch_surface_atlas_replaced_by(self):
        atlas_old = Record(
            "16G101-1",
            "superseded",
            "混凝土结构施工图平面整体表示方法制图规则和构造详图",
            "2016-09-01",
            "chinabuilding",
            replaced_by="22G101-1",
        )
        outcome_cached = Outcome((atlas_old,), "chinabuilding", from_cache=True, cache_age=3600)
        server = McpServer(_OutcomeBackend({"16G101-1": outcome_cached}))
        self.addCleanup(server.close)

        q = _call(server, "query", {"code": "16G101-1"})["result"]
        self.assertFalse(q["isError"])
        self.assertIn("16G101-1\tsuperseded\t", q["content"][0]["text"])
        self.assertIn("replaced_by=22G101-1", q["content"][0]["text"])

        # verify 的 content[0] 仍严格只有 code\tstatus，replacements 放进 content[1]。
        v = _call(server, "verify", {"code": "16G101-1"})["result"]
        self.assertEqual(v["content"][0]["text"], "16G101-1\tsuperseded\n")
        meta = json.loads(v["content"][1]["text"])
        self.assertTrue(meta["from_cache"])
        self.assertEqual(meta["replacements"], [{"code": "16G101-1", "replaced_by": "22G101-1"}])

        # 无缓存告警时 verify 同样保留精确 content[0] + content[1].replacements。
        fresh_server = McpServer(_OutcomeBackend({"16G101-1": Outcome((atlas_old,), "chinabuilding")}))
        self.addCleanup(fresh_server.close)
        vf = _call(fresh_server, "verify", {"code": "16G101-1"})["result"]
        self.assertEqual(vf["content"][0]["text"], "16G101-1\tsuperseded\n")
        self.assertEqual(
            json.loads(vf["content"][1]["text"]),
            {"replacements": [{"code": "16G101-1", "replaced_by": "22G101-1"}]},
        )

        # batch 同步透传 replacements。
        b = _call(fresh_server, "batch", {"codes": ["16G101-1"]})["result"]
        self.assertFalse(b["isError"])
        self.assertIn("完成 1", b["content"][0]["text"])
        self.assertEqual(
            json.loads(b["content"][1]["text"]),
            {"replacements": [{"query": "16G101-1", "code": "16G101-1", "replaced_by": "22G101-1"}]},
        )

    def test_atlas_search_readonly_missing_corrupt_and_populated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "home" / "catalog.db"
            atlas_path = root / "home" / "atlas-catalog.json"
            server = McpServer(NullCurrency(), store_path=db_path)
            self.addCleanup(server.close)

            # 1) 缺失目录：绝不建目录或写盘，返回清晰提示
            with mock.patch("socket.create_connection", side_effect=AssertionError("不得联网")), \
                    mock.patch.object(Path, "mkdir", side_effect=AssertionError("atlas_search 不得建目录")):
                missing_res = _call(server, "atlas_search", {"query": "22G101-1"})["result"]
            self.assertFalse(missing_res["isError"])
            missing_payload = json.loads(missing_res["content"][0]["text"])
            self.assertFalse(missing_payload["catalog_ready"])
            self.assertEqual(missing_payload["catalog_state"], "missing")
            self.assertIn("guiju atlas sync", missing_payload["hint"])
            self.assertFalse(root.joinpath("home").exists())

            # 2) 损坏目录：降级提示但不删除原文件
            atlas_path.parent.mkdir(parents=True, exist_ok=True)
            atlas_path.write_text("{corrupt", encoding="utf-8")
            corrupt_res = _call(server, "atlas_search", {"query": "22G101-1"})["result"]
            corrupt_payload = json.loads(corrupt_res["content"][0]["text"])
            self.assertEqual(corrupt_payload["catalog_state"], "corrupt")
            self.assertTrue(atlas_path.exists())

            # 3) 写入完整样本后按关键词/前缀/状态/限制条数检索
            sample_html = (Path(__file__).parent / "fixtures" / "atlas" / "concise_catalogue_sample.html").read_text("utf-8")
            entries = atlas_domain.parse_concise_catalogue(sample_html)
            AtlasCatalogStore(atlas_path, clock=lambda: 1_800_000_000.0).save(entries)

            # 验证在只读目录权限下也可正常工作、不建锁文件
            ok_res = _call(server, "atlas_search", {"query": "16G101-1"})["result"]
            self.assertFalse(ok_res["isError"])
            ok_payload = json.loads(ok_res["content"][0]["text"])
            self.assertTrue(ok_payload["catalog_ready"])
            self.assertEqual(ok_payload["catalog_total"], 21)
            self.assertEqual(ok_payload["results"][0]["code"], "16G101-1")
            self.assertEqual(ok_payload["results"][0]["replaced_by"], "22G101-1")

            prov_res = _call(server, "atlas_search", {"prefix": "粤", "status": "current", "limit": 5})["result"]
            prov_payload = json.loads(prov_res["content"][0]["text"])
            self.assertEqual([r["code"] for r in prov_payload["results"]], ["粤25G/T116"])

            # 非法参数一律 isError=True
            for bad_args in (
                {"query": 123},
                {"prefix": []},
                {"status": "bogus"},
                {"limit": True},
                {"limit": -1},
                {"limit": 201},
            ):
                with self.subTest(bad_args=bad_args):
                    err = _call(server, "atlas_search", bad_args)["result"]
                    self.assertTrue(err["isError"])

    def test_announcement_rejects_atlas_without_false_mohurd_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "catalog.db"
            store = Store(db_path)
            try:
                store.upsert_announcement(
                    "https://www.mohurd.gov.cn/gonggao/399.html",
                    "2016-10-01",
                    "住房城乡建设部关于发布行业标准《住宅排气道应用技术规程》的公告",
                    "住宅排气道应用技术规程",
                    "standard",
                )
                store.commit()
            finally:
                store.close()

            rec = Record("16J916-1", "current", "住宅排气道", "2016-09-01", "chinabuilding")
            server = McpServer(FakeBackend({"16J916-1": (rec,)}), store_path=db_path)
            self.addCleanup(server.close)

            for ref in ("16J916-1", "粤25G/T116"):
                res = _call(server, "announcement", {"ref": ref})["result"]
                self.assertTrue(res["isError"])
                payload = json.loads(res["content"][0]["text"])
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["verdict"], "copyright_atlas")
                self.assertNotIn("mohurd.gov.cn", res["content"][0]["text"])

            with mock.patch.object(atlas_domain, "is_atlas_code", return_value=False):
                bypass = _call(server, "announcement", {"ref": "16J916-1"})["result"]
            self.assertTrue(bypass["isError"])
            bypass_payload = json.loads(bypass["content"][0]["text"])
            self.assertEqual(bypass_payload["verdict"], "copyright_atlas")
            self.assertNotIn("mohurd.gov.cn", bypass["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
