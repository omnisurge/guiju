"""A1 / S4：MCP batch 彻底移除服务端文件路径（仅内联 codes[]）；非 object params 不打死 stdio。"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.currency.model import Record
from guiju.currency.native import Outcome
from guiju.mcp import McpServer, serve_stdio


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.rows = {
            "GB50011": [Record("GB 50011-2010", "current", "建筑抗震设计标准", "2010-12-01", "csres")]
        }
        self.calls = []

    def query(self, code):
        self.calls.append(code)
        return Outcome(tuple(self.rows.get(code, ())), self.name)

    def lookup(self, code):
        return list(self.query(code).results)


class McpBatchInlineOnlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict("os.environ", {"GUIJU_HOME": str(self.home)}).start()
        self.server = McpServer(FakeBackend())
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


if __name__ == "__main__":
    unittest.main()
