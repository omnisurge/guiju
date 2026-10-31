"""时效适配器的显式配置：要么照办，要么报错，绝不静默换成别的源。"""

import os
import unittest
from unittest import mock

from guiju import currency as cm
from guiju.errors import SourceUnreachable


class ExplicitOverrideTest(unittest.TestCase):
    def test_off_is_null(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "off"}):
            self.assertEqual(cm.default_currency().name, "none")

    def test_cmd_without_binary_is_misconfigured_not_mcp(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "cmd"}):
            cur = cm.default_currency()
        self.assertEqual(cur.name, "cmd(缺失)")
        with self.assertRaises(SourceUnreachable) as ctx:
            cur.lookup("GB 55037-2022")
        self.assertIn("spec-currency", str(ctx.exception))

    def test_unknown_value_is_reported(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "xyz"}):
            cur = cm.default_currency()
        self.assertEqual(cur.name, "xyz(未知)")
        with self.assertRaises(SourceUnreachable):
            cur.lookup("GB 55037-2022")

    def test_cmd_with_explicit_path_is_honoured(self):
        with mock.patch.dict(os.environ, {"GUIJU_CURRENCY": "cmd:/opt/spec-currency"}):
            cur = cm.default_currency()
        self.assertEqual(cur.name, "cmd")
        self.assertEqual(cur.binary, "/opt/spec-currency")


if __name__ == "__main__":
    unittest.main()
