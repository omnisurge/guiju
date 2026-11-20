"""P3-7：GuijuError 失败载荷字段边界。

不变量：ok=false 的失败体不得携带成功专属字段（cite），也不得被 extra
覆盖基础字段（ok/exit/reason/error；hint 是构造器参数）。成功路径不经过
GuijuError.payload()，因此 cite 只可能在 ok=true 成功体出现。
"""

from __future__ import annotations

import unittest

from guiju.errors import GuijuError


class ErrorPayloadContractTest(unittest.TestCase):
    def test_error_payload_never_exposes_success_only_cite(self):
        exc = GuijuError(
            "内部错误",
            cite="《某标准》GB 1-2020 第5.2.1条",
            ok=True,
            exit=0,
            reason="ok",
            error="伪成功",
        )
        payload = exc.payload()
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["exit"], 1)
        self.assertEqual(payload["reason"], "internal")
        self.assertEqual(payload["error"], "内部错误")
        self.assertNotIn("cite", payload)

    def test_error_payload_keeps_diagnostic_extras(self):
        exc = GuijuError("失败", warnings=["w"], currency_check={"state": "unreachable"})
        payload = exc.payload()
        self.assertEqual(payload["warnings"], ["w"])
        self.assertEqual(payload["currency_check"], {"state": "unreachable"})


if __name__ == "__main__":
    unittest.main()
