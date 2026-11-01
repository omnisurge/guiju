"""M4 只读对比逻辑的离线测试；不联网、不访问 NAS。"""

import unittest

from guiju.currency.m4diff import compare_rows
from guiju.currency.model import Record


def mcp(code="GB 1-2020", status="current", name="甲", date="2020-01-01"):
    return Record(code=code, status=status, name=name, implement_date=date, source="csres")


class CompareRowsTest(unittest.TestCase):
    def test_exact_three_fields_match(self):
        result = compare_rows([mcp()], [mcp()])
        self.assertTrue(result["match"])
        self.assertEqual(result["differences"], [])
        self.assertEqual(result["mcp_count"], 1)

    def test_status_name_and_date_are_each_compared(self):
        for field, value in (("status", "withdrawn"), ("name", "乙"), ("implement_date", "2021-02-02")):
            native = mcp()
            object.__setattr__(native, field, value)
            with self.subTest(field=field):
                result = compare_rows([mcp()], [native])
                self.assertFalse(result["match"])
                self.assertEqual(result["differences"][0]["field"], field)

    def test_empty_both_sides_is_a_match_but_one_side_empty_is_not(self):
        empty = compare_rows([], [])
        self.assertTrue(empty["match"])
        self.assertTrue(empty["both_empty"])
        missing = compare_rows([], [mcp()])
        self.assertFalse(missing["match"])
        self.assertEqual(missing["differences"][0]["kind"], "missing_mcp")

    def test_extra_version_on_either_side_is_not_hidden(self):
        extra = mcp(code="GB 1-2019", status="superseded")
        result = compare_rows([mcp()], [mcp(), extra])
        self.assertFalse(result["match"])
        self.assertEqual(result["differences"][0]["kind"], "missing_mcp")
        self.assertEqual(result["differences"][0]["code"], "GB1-2019")

    def test_code_key_uses_shared_normalizer_so_spacing_is_aligned(self):
        result = compare_rows([mcp(code="GB/T 1-2020")], [mcp(code="GB/T1-2020")])
        self.assertTrue(result["match"])

    def test_duplicate_normalized_code_is_reported(self):
        result = compare_rows([mcp(), mcp(name="重复")], [mcp()])
        self.assertFalse(result["match"])
        self.assertTrue(any(diff["kind"] == "duplicate_code" for diff in result["differences"]))


if __name__ == "__main__":
    unittest.main()
