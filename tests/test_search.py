"""检索评分 —— 用户最常用的入口，行为必须可预期。"""

import unittest

from guiju.domain.search import bigrams, normalize, rank, tokens_of

ENTRIES = [
    {"id": 1, "name": "建筑防火通用规范", "title": "关于发布国家标准《建筑防火通用规范》的公告",
     "codes": ["GB 55037-2022"], "published_on": "2022-01-01"},
    {"id": 2, "name": "建筑设计防火规范", "title": "关于发布国家标准《建筑设计防火规范》的公告",
     "codes": ["GB 50016-2014"], "published_on": "2014-08-01"},
    {"id": 3, "name": "煤化工工程设计防火标准", "title": "关于发布国家标准《煤化工工程设计防火标准》的公告",
     "codes": [], "published_on": "2023-05-01"},
    {"id": 4, "name": "住宅性能评定标准", "title": "关于发布国家标准《住宅性能评定标准》的公告",
     "codes": ["GB/T 50362-2022"], "published_on": "2023-01-19"},
    {"id": 5, "name": "外墙外保温用防火分隔条", "title": "关于发布行业产品标准《外墙外保温用防火分隔条》的公告",
     "codes": [], "published_on": "2024-03-01"},
    {"id": 6, "name": "建筑给水排水设计规范", "title": "关于发布国家标准《建筑给水排水设计规范》的公告",
     "codes": ["GB 50015-2019"], "published_on": "2019-06-01"},
]


class NormalizeTest(unittest.TestCase):
    def test_book_marks_are_stripped(self):
        self.assertEqual(normalize("《防火》"), "防火")
        self.assertEqual(tokens_of("《防火》"), ["防火"])

    def test_mixed_separators(self):
        self.assertEqual(tokens_of("防火, 规范、设计"), ["防火", "规范", "设计"])

    def test_fullwidth_and_spaces(self):
        self.assertEqual(normalize("ＧＢ ５５０３７－２０２２"), "GB55037-2022")


class BigramTest(unittest.TestCase):
    def test_short_token_is_itself(self):
        self.assertEqual(bigrams("防火"), ["防火"])

    def test_three_char_token(self):
        self.assertEqual(bigrams("防火规"), ["防火", "火规"])


class RankTest(unittest.TestCase):
    def test_bookmark_query_finds_all(self):
        hits, mode = rank(ENTRIES, tokens_of("《防火》"))
        self.assertEqual(mode, "and")
        self.assertEqual({h.entry["id"] for h in hits}, {1, 2, 3, 5})

    def test_compound_word_merges_fuzzy_recall(self):
        """「防火规范」在《建筑防火通用规范》里并不连续：必须靠模糊补召回。

        （旧实现只要有一条严格命中就丢掉模糊结果 —— 26 条变 21 条，正是用户抱怨的问题。）
        """
        hits, mode = rank(ENTRIES, tokens_of("防火规范"))
        self.assertEqual(mode, "and+fuzzy")
        ids = [h.entry["id"] for h in hits]
        self.assertIn(1, ids)   # 建筑防火通用规范（模糊补回）
        self.assertIn(2, ids)   # 建筑设计防火规范（严格命中）
        self.assertNotIn(4, ids)

    def test_fuzzy_excludes_unrelated(self):
        hits, _ = rank(ENTRIES, tokens_of("防火规范"))
        self.assertNotIn(4, hits and [h.entry["id"] for h in hits])

    def test_multi_token_is_and(self):
        hits, mode = rank(ENTRIES, tokens_of("防火 设计"))
        self.assertIn(mode, ("and", "and+fuzzy"))
        ids = {h.entry["id"] for h in hits}
        self.assertTrue({2, 3} <= ids)          # 两个都必须命中
        self.assertNotIn(4, ids)                 # 无关条目不出现

    def test_code_query(self):
        hits, _ = rank(ENTRIES, tokens_of("55037"))
        self.assertEqual([h.entry["id"] for h in hits], [1])
        self.assertEqual(hits[0].match, "contains_code")

    def test_relevance_beats_date(self):
        """名称完全匹配必须排在更新的公告前面。"""
        hits, _ = rank(ENTRIES, tokens_of("建筑设计防火规范"))
        self.assertEqual(hits[0].entry["id"], 2)

    def test_char_coverage_handles_inserted_characters(self):
        """「给排水」↔《建筑给水排水设计规范》：中文标准名常见的插字变形。"""
        hits, mode = rank(ENTRIES, tokens_of("给排水"))
        self.assertEqual(mode, "chars")
        self.assertEqual([h.entry["id"] for h in hits], [6])

    def test_no_match(self):
        hits, _ = rank(ENTRIES, tokens_of("钢结构"))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
