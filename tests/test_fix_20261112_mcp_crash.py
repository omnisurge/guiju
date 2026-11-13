"""H2 回归测试：工具内部抛非 GuijuError 时，服务不得崩溃，须返回 JSON-RPC 错误。

背景：此前 _call/do_POST 只 catch GuijuError，任意其他异常会让 stdio serve 循环
整体死掉、HTTP handler 线程断连（无 JSON-RPC 错误体返回给客户端）。
"""

import json
import threading
import unittest
import urllib.request

from guiju.mcp import McpServer, make_http_server


class CrashingBackend:
    """lookup 直接抛运行时错误（模拟 sqlite3.Error / RuntimeError 等）。"""

    def lookup(self, code):
        raise RuntimeError("simulated backend crash")


class McpCrashIsolationTest(unittest.TestCase):
    def setUp(self):
        self.server = McpServer(CrashingBackend())

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
        # 第一次崩溃后 handle 仍然可用（stdio 循环不会因异常终止）
        self._call("query", {"code": "X"})
        self._call("ping", {})
        # ping 是 method 级，直接调用 handle
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


if __name__ == "__main__":
    unittest.main()
