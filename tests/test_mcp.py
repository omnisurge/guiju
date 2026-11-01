"""guiju mcp 契约的离线测试：不访问网络。"""

import json
import threading
import unittest
import urllib.request

from guiju.currency.model import Record
from guiju.mcp import McpServer, make_http_server


class FakeBackend:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def lookup(self, code):
        self.calls.append(code)
        return list(self.rows.get(code, []))


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
        self.assertEqual([tool["name"] for tool in tools], ["query", "verify", "batch"])

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

    def test_missing_arguments_are_tool_errors(self):
        for name in ("query", "verify", "batch"):
            response = self.call("tools/call", {"name": name, "arguments": {}})
            self.assertTrue(response["result"]["isError"], name)

    def test_notifications_and_unknown_methods(self):
        self.assertIsNone(self.call("notifications/initialized", include_id=False))
        unknown = self.call("unknown/method")
        self.assertEqual(unknown["error"]["code"], -32601)

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
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
