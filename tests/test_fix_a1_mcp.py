"""A1：MCP batch 只能读写 GUIJU_HOME/mcp；非 object params 不能打死 stdio。"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.currency.model import Record
from guiju.mcp import McpServer, serve_stdio


class FakeBackend:
    def __init__(self):
        self.rows = {
            "GB50011": [Record("GB 50011-2010", "current", "建筑抗震设计标准", "2010-12-01", "csres")]
        }
        self.calls = []

    def lookup(self, code):
        self.calls.append(code)
        return list(self.rows.get(code, []))


class McpPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.mcp_root = self.home / "mcp"
        self.mcp_root.mkdir()
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict("os.environ", {"GUIJU_HOME": str(self.home)}).start()
        self.server = McpServer(FakeBackend())

    def test_batch_relative_path_and_out_allowed(self):
        (self.mcp_root / "list.txt").write_text("GB50011\n", encoding="utf-8")
        result = self.server._batch({"list": "list.txt", "out": "out.txt"})
        self.assertFalse(result["isError"])
        text = (self.mcp_root / "out.txt").read_text(encoding="utf-8")
        self.assertIn("GB 50011-2010", text)

    def test_batch_rejects_absolute_list_outside_root(self):
        result = self.server._batch({"list": "/etc/hosts"})
        self.assertTrue(result["isError"])
        self.assertIn("必须位于", result["content"][0]["text"])

    def test_batch_rejects_dotdot_escape(self):
        result = self.server._batch({"list": "../outside.txt"})
        self.assertTrue(result["isError"])
        self.assertIn("必须位于", result["content"][0]["text"])

    def test_batch_rejects_symlink_escape(self):
        outside = self.home / "outside-a1.txt"
        outside.write_text("GB50011\n", encoding="utf-8")
        link = self.mcp_root / "link.txt"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("当前平台不支持 symlink")
        result = self.server._batch({"list": str(link)})
        self.assertTrue(result["isError"])
        self.assertIn("必须位于", result["content"][0]["text"])

    def test_batch_rejects_out_outside_root(self):
        (self.mcp_root / "list.txt").write_text("GB50011\n", encoding="utf-8")
        result = self.server._batch({"list": "list.txt", "out": "/tmp/a1-out.txt"})
        self.assertTrue(result["isError"])
        self.assertIn("必须位于", result["content"][0]["text"])

    def test_batch_absolute_paths_inside_root_allowed(self):
        list_file = self.mcp_root / "list.txt"
        list_file.write_text("GB50011\n", encoding="utf-8")
        out_file = self.mcp_root / "out.txt"
        result = self.server._batch({"list": str(list_file), "out": str(out_file)})
        self.assertFalse(result["isError"])
        self.assertTrue(out_file.exists())

    def test_batch_rejects_prefix_collision(self):
        sibling = self.home / "mcp-evil"
        sibling.mkdir()
        result = self.server._batch({"list": str(sibling / "list.txt")})
        self.assertTrue(result["isError"])
        self.assertIn("必须位于", result["content"][0]["text"])

    def test_batch_creates_missing_root(self):
        self.mcp_root.rmdir()
        result = self.server._batch({"list": "list.txt"})
        self.assertTrue(result["isError"])
        self.assertTrue(self.mcp_root.is_dir())

    def test_batch_rejects_symlink_root(self):
        self.mcp_root.rmdir()
        target = self.home / "actual-root"
        target.mkdir()
        try:
            os.symlink(target, self.mcp_root)
        except OSError:
            self.skipTest("当前平台不支持 symlink")
        result = self.server._batch({"list": "list.txt"})
        self.assertTrue(result["isError"])
        self.assertIn("symlink", result["content"][0]["text"])

    def test_batch_rejects_root_file(self):
        self.mcp_root.rmdir()
        self.mcp_root.write_text("not a dir", encoding="utf-8")
        result = self.server._batch({"list": "list.txt"})
        self.assertTrue(result["isError"])
        self.assertIn("不是目录", result["content"][0]["text"])

    def test_batch_rejects_out_symlink_without_writing(self):
        (self.mcp_root / "list.txt").write_text("GB50011\n", encoding="utf-8")
        outside = self.home / "outside.tsv"
        outside.write_text("PRE\n", encoding="utf-8")
        link = self.mcp_root / "out-link.tsv"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("当前平台不支持 symlink")
        result = self.server._batch({"list": "list.txt", "out": str(link)})
        self.assertTrue(result["isError"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "PRE\n")

    def test_batch_validates_out_before_backend_calls(self):
        (self.mcp_root / "list.txt").write_text("GB50011\nNOPE\n", encoding="utf-8")
        result = self.server._batch({"list": "list.txt", "out": "/etc/evil.tsv"})
        self.assertTrue(result["isError"])
        self.assertEqual(self.server.backend.calls, [])


class McpStdioTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(FakeBackend())

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
