"""GUIJU_CURRENCY / GUIJU_CURRENCY_URL 的每一部分都必须真正生效（不许静默丢弃）。"""

import os
import unittest
from unittest import mock

from guiju import currency as cm


class McpUrlOverrideTest(unittest.TestCase):
    def test_currency_mcp_honours_url_env(self):
        with mock.patch.dict(
            os.environ,
            {"GUIJU_CURRENCY": "mcp", "GUIJU_CURRENCY_URL": "http://127.0.0.1:1/mcp"},
        ):
            cur = cm.default_currency()
        self.assertEqual(cur.name, "mcp")
        self.assertEqual(cur.url, "http://127.0.0.1:1/mcp")

    def test_inline_url_wins_over_env(self):
        with mock.patch.dict(
            os.environ,
            {"GUIJU_CURRENCY": "mcp:http://a/mcp", "GUIJU_CURRENCY_URL": "http://b/mcp"},
        ):
            cur = cm.default_currency()
        self.assertEqual(cur.url, "http://a/mcp")

    def test_plain_mcp_falls_back_to_default(self):
        env = {"GUIJU_CURRENCY": "mcp"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("GUIJU_CURRENCY_URL", None)
            cur = cm.default_currency()
        self.assertEqual(cur.url, cm.DEFAULT_MCP_URL)


class EndpointTest(unittest.TestCase):
    def test_endpoint_reports_what_is_used(self):
        self.assertEqual(cm.endpoint_of(cm.McpCurrency("http://x/mcp")), "http://x/mcp")
        self.assertEqual(cm.endpoint_of(cm.CommandCurrency("/opt/spec-currency")), "/opt/spec-currency")
        self.assertEqual(cm.endpoint_of(cm.NullCurrency()), None)
        broken = cm.MisconfiguredCurrency("cmd(缺失)", "找不到二进制")
        self.assertIn("找不到二进制", cm.endpoint_of(broken))


if __name__ == "__main__":
    unittest.main()
