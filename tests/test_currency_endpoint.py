"""endpoint_of 必须如实报告当前适配器实际用的端点/不可用原因（不许猜）。"""

import unittest

from guiju import currency as cm


class EndpointTest(unittest.TestCase):
    def test_endpoint_reports_what_is_used(self):
        self.assertEqual(cm.endpoint_of(cm.NullCurrency()), None)
        broken = cm.MisconfiguredCurrency("cmd(未知)", "GUIJU_CURRENCY=cmd 无法识别")
        self.assertIn("无法识别", cm.endpoint_of(broken))


if __name__ == "__main__":
    unittest.main()
