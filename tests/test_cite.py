"""引用串矩阵 —— 最容易被改坏、后果最直接的一段逻辑。"""

import unittest

from guiju.domain.cite import build_citation, normalize_clause


class ClauseTest(unittest.TestCase):
    def test_accepted_forms(self):
        for raw in ("5.2.1", "第5.2.1条", "第 5.2.1 条", "5.2.1条", " 5.2.1 "):
            self.assertEqual(normalize_clause(raw), "5.2.1", raw)

    def test_rejects_garbage(self):
        for raw in ("abc", "5.2.1.x", "第条", ""):
            self.assertIsNone(normalize_clause(raw), raw)

    def test_none_passthrough(self):
        self.assertIsNone(normalize_clause(None))


class CitationTest(unittest.TestCase):
    def test_full_form(self):
        text, warnings = build_citation("建筑防火通用规范", "GB 55037-2022", "5.2.1")
        self.assertEqual(text, "《建筑防火通用规范》GB 55037-2022 第5.2.1条")
        self.assertEqual(warnings, [])

    def test_without_clause(self):
        text, _ = build_citation("建筑防火通用规范", "GB 55037-2022")
        self.assertEqual(text, "《建筑防火通用规范》GB 55037-2022")

    def test_missing_code_warns(self):
        text, warnings = build_citation("某项标准", None, "3.1.2")
        self.assertEqual(text, "《某项标准》 第3.1.2条")
        self.assertTrue(any("编号未核实" in w for w in warnings))

    def test_ambiguous_confidence_warns(self):
        _, warnings = build_citation("某标准", "JGJ 147-2016", "1.0.1", confidence="ambiguous")
        self.assertTrue(any("歧义" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
