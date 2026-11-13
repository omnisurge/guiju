"""时效适配器的显式配置：要么照办，要么报错，绝不静默换成别的源。

spec-currency 已退役（2026-11-13）：cmd / mcp 不再被识别，一律报「无法识别」。
"""

import os
import unittest
from unittest import mock

from guiju import currency as cm
from guiju.errors import SourceUnreachable


class ExplicitOverrideTest(unittest.TestCase):
    def test_off_is_null(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "off"}):
            self.assertEqual(cm.default_currency().name, "none")

    def test_cmd_is_misconfigured_now_retired(self):
        # spec-currency 退役后 cmd 落入未知分支：报错而不是悄悄找二进制
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "cmd"}):
            cur = cm.default_currency()
        self.assertEqual(cur.name, "cmd(未知)")
        with self.assertRaises(SourceUnreachable) as ctx:
            cur.lookup("GB 55037-2022")
        self.assertIn("无法识别", str(ctx.exception))

    def test_mcp_is_misconfigured_now_retired(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "mcp"}):
            cur = cm.default_currency()
        self.assertEqual(cur.name, "mcp(未知)")
        with self.assertRaises(SourceUnreachable):
            cur.lookup("GB 55037-2022")

    def test_unknown_value_is_reported(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "xyz"}):
            cur = cm.default_currency()
        self.assertEqual(cur.name, "xyz(未知)")
        with self.assertRaises(SourceUnreachable):
            cur.lookup("GB 55037-2022")

    def test_native_is_still_accepted(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "native", "GUIJU_CURRENCY_CACHE": "off"}):
            self.assertEqual(cm.default_currency().name, "native")


if __name__ == "__main__":
    unittest.main()
