"""目录解析回归 —— 夹具是 2026-10-30 真实响应片段，解析器改动即报警。"""

import re
import unittest

from guiju.domain.announcement import classify_title
from guiju.sources.mohurd import _COUNT_RE, _DATE_RE, _ITEM_RE

from .paths import FIXTURES


class CatalogParseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fragment = (FIXTURES / "catalog-page-1.html").read_text(encoding="utf-8")

    def test_total_count(self):
        self.assertEqual(int(_COUNT_RE.search(self.fragment).group(1)), 3254)

    def test_item_count(self):
        blocks = re.split(r'<li class="date">', self.fragment)[1:]
        parsed = [b for b in blocks if _ITEM_RE.search(b) and _DATE_RE.search(b)]
        self.assertEqual(len(parsed), 99)  # 单页渲染上限 99，实测非丢数据
        self.assertEqual(len(blocks), 99)

    def test_first_item_shape(self):
        block = re.split(r'<li class="date">', self.fragment)[1]
        link, date = _ITEM_RE.search(block), _DATE_RE.search(block)
        self.assertTrue(link.group(1).startswith("/gongkai/zc/wjk/art/"))
        self.assertRegex(date.group(1), r"^\d{4}-\d{2}-\d{2}$")


class TitleTest(unittest.TestCase):
    def test_extracts_name_and_category(self):
        name, category = classify_title("住房城乡建设部关于发布国家标准《医院洁净手术部建筑技术规范》局部修订条文的公告")
        self.assertEqual(name, "医院洁净手术部建筑技术规范")
        self.assertEqual(category, "partial_revision")

    def test_falls_back_without_book_marks(self):
        name, category = classify_title("住房城乡建设部关于印发2016年工作计划的通知")
        self.assertTrue(name.startswith("住房城乡建设部"))
        self.assertEqual(category, "other")


if __name__ == "__main__":
    unittest.main()
