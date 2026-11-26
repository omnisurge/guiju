"""标准图集纯领域模块离线测试（使用 S0 真实脱敏黄金夹具）。"""

from __future__ import annotations

import unittest
from pathlib import Path

from guiju.domain import atlas
from guiju.errors import NeedsHuman

FIXTURES = Path(__file__).parent / "fixtures" / "atlas"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class AtlasCodeClassificationTest(unittest.TestCase):
    def test_positive_atlas_families_recognized(self) -> None:
        positives = (
            # 国标图集
            "22G101-1", "16G101-1", "11G101-1", "09MR603", "06SS127",
            "24CS01-1", "13CD701-4", "11SJ937-1（1）", "15J401", "23G101-11",
            "B16G101-1", "B 16G101-1", "11G101-1 英文版",
            # 中南标与省标（含汉字省前缀）
            "21ZJ111", "粤25G/T116", "京05G69", "京02SSB1", "冀00J201", "冀00ZJ03",
            # 合订本与无年份基础号
            "G101-1", "G101", "J401", "ZJ111", "S901～902", "D101-1～7", "D101-1～7(新)",
            "D500~D502", "DX003～004", "S1~S3", "FD01",
            # 明确带图集后缀的非上海地方 DBJ
            "DBJ/T02-81-2013-12D1",
        )
        for code in positives:
            with self.subTest(code=code):
                self.assertTrue(atlas.is_atlas_code(code), f"漏判图集号: {code}")

    def test_standards_shanghai_and_noise_never_misclassified(self) -> None:
        negatives = (
            # 国标/行标/通用地标/团标
            "GB 55037-2022", "GB/T 50378-2019", "JGJ 147-2016", "JGJ/T 450-2018",
            "CJJ 1-1990", "CJJ/T 100-2010", "CJ/T 133-2012", "JTG D60-2015",
            "DB11/T 123-2020", "DBJ/T 15-123-2020", "T/CECS 100-2020",
            "GB 55037", "JGJ 147", "YB 9082", "DL 5009",
            # 上海全家族（必须留给 shzjw 路由）
            "DG/TJ08-2007-2026", "DGJ08-2139-2021", "DBJT08-144-2026",
            "DBTJ08-102-2016", "DBJ08-101-2020", "DB31/T 792-2014", "DB3108-2020",
            # 非编号输入与非法控制字符
            "建筑防火通用规范", "混凝土结构施工图", "153493",
            "CECS1", "GBX1", "XX 999", "UNKNOWN", "NFPA 13", "M1",
            "https://ebook.chinabuilding.com.cn/zbooklib/book/detail/show?SiteID=1&bookID=153493",
            "2022-05-01", "", "   ", "22G101-1\u200b", "22G101-1\x00",
        )
        for code in negatives:
            with self.subTest(code=code):
                self.assertFalse(atlas.is_atlas_code(code), f"误判为图集号: {code}")

    def test_anti_confusion_and_family_matching(self) -> None:
        # 1) B 双语陷阱与不同年份严格隔离
        self.assertTrue(atlas.matches("16G101-1", "16G101-1"))
        self.assertTrue(atlas.matches("16G101－1", "16G101-1"))
        self.assertFalse(atlas.matches("16G101-1", "B16G101-1"))
        self.assertFalse(atlas.matches("16G101-1", "B 16G101-1"))
        self.assertFalse(atlas.matches("16G101-1", "22G101-1"))
        self.assertTrue(atlas.matches("B16G101-1", "B 16G101-1"))

        # 2) 英文版后缀不混入中文正本
        self.assertTrue(atlas.matches("11G101-1", "11G101-1"))
        self.assertFalse(atlas.matches("11G101-1", "11G101-1 英文版"))
        self.assertTrue(atlas.matches("11G101-1 英文版", "11G101-1英文版"))

        # 3) 无年份基础号匹配全中文代际，但拒收双语/英文版及 23G101-11 兄弟子册
        for hit in ("22G101-1", "16G101-1", "11G101-1", "03G101-1"):
            self.assertTrue(atlas.matches("G101-1", hit), f"G101-1 应命中 {hit}")
        for reject in ("23G101-11", "17G101-11", "B16G101-1", "11G101-1 英文版"):
            self.assertFalse(atlas.matches("G101-1", reject), f"G101-1 不应命中 {reject}")

        # 4) 仅专业+系列号 & 全角括号等价 & 控制字符防御
        self.assertTrue(atlas.matches("G101", "22G101-1"))
        self.assertFalse(atlas.matches("G101", "22G1011-1"))
        self.assertTrue(atlas.matches("11SJ937-1(1)", "11SJ937-1（1）"))
        self.assertFalse(atlas.matches("22G101-1\u200b", "22G101-1"))
        self.assertFalse(atlas.has_edition_year("G101-1"))
        self.assertTrue(atlas.has_edition_year("22G101-1"))
        self.assertTrue(atlas.has_edition_year("B16G101-1"))
        self.assertTrue(atlas.has_edition_year("S901～902"))


class AtlasSearchParserTest(unittest.TestCase):
    def test_parse_exact_current_22g101_1(self) -> None:
        page = atlas.parse_search_page(_read("search_22g101_1.html"))
        self.assertEqual(page.total, 1)
        self.assertFalse(page.empty_authoritative)
        self.assertFalse(page.has_more)
        self.assertEqual(len(page.items), 1)
        item = page.items[0]
        self.assertEqual(item.book_id, "153493")
        self.assertEqual(item.code, "22G101-1")
        self.assertIn("混凝土结构施工图", item.name)
        self.assertEqual(item.status, "current")
        self.assertEqual(item.publish_date, "2022-05-01")
        self.assertEqual(item.implement_date, "2022-05-01")
        self.assertEqual(item.abolish_date, "")
        self.assertEqual(item.url, atlas.detail_url("153493"))

    def test_parse_16g101_1_filters_out_bilingual_trap(self) -> None:
        page = atlas.parse_search_page(_read("search_16g101_1.html"))
        self.assertEqual(page.total, 2)
        self.assertEqual([i.book_id for i in page.items], ["70663", "61268"])
        self.assertEqual([i.status for i in page.items], ["current", "withdrawn"])
        self.assertEqual(page.items[1].abolish_date, "2022-05-01")

        exact = [i for i in page.items if atlas.matches("16G101-1", i.code)]
        self.assertEqual(len(exact), 1)
        self.assertEqual(exact[0].book_id, "61268")
        self.assertEqual(exact[0].status, "withdrawn")

    def test_parse_base_g101_1_keeps_only_four_chinese_editions(self) -> None:
        page = atlas.parse_search_page(_read("search_g101_1.html"))
        self.assertEqual(page.total, 9)
        self.assertEqual(len(page.items), 9)
        filtered = [i for i in page.items if atlas.matches("G101-1", i.code)]
        self.assertEqual([i.code for i in filtered], ["22G101-1", "16G101-1", "11G101-1", "03G101-1"])

    def test_parse_authoritative_empty(self) -> None:
        page = atlas.parse_search_page(_read("search_empty_99z999.html"))
        self.assertEqual(page.items, ())
        self.assertEqual(page.total, 0)
        self.assertTrue(page.empty_authoritative)
        self.assertFalse(page.has_more)

    def test_parse_pagination_has_more(self) -> None:
        base_html = _read("search_22g101_1.html")
        # 构造总数为 25、本页 20 张卡的第 1 页，验证 has_more=True；第 2 页 5 张时 False。
        card_m = base_html.find('<h4 class="search-tit')
        footer_idx = base_html.find('<div id="pageEx"')
        single_card = base_html[card_m:footer_idx]
        cards_20 = "\n".join(single_card.replace("153493", str(153493 + idx)) for idx in range(20))
        page1_html = (
            base_html[:card_m]
            .replace('"count":1', '"count":25')
            + cards_20
            + base_html[footer_idx:]
        )
        p1 = atlas.parse_search_page(page1_html, page=1)
        self.assertEqual(p1.total, 25)
        self.assertEqual(len(p1.items), 20)
        self.assertTrue(p1.has_more)

        cards_5 = "\n".join(single_card.replace("153493", str(153600 + idx)) for idx in range(5))
        page2_html = base_html[:card_m].replace('"count":1', '"count":25') + cards_5 + base_html[footer_idx:]
        p2 = atlas.parse_search_page(page2_html, page=2)
        self.assertEqual(len(p2.items), 5)
        self.assertFalse(p2.has_more)

    def test_search_fail_closed_on_malformed_or_contradictory_html(self) -> None:
        valid = _read("search_22g101_1.html")
        with self.assertRaises(NeedsHuman):
            atlas.parse_search_page("")
        with self.assertRaises(NeedsHuman):
            atlas.parse_search_page("<html><body>no data var</body></html>")
        with self.assertRaises(NeedsHuman):
            atlas.parse_search_page("<script>var data = {broken;</script>")
        with self.assertRaises(NeedsHuman):
            atlas.parse_search_page(valid.replace('"count":1', '"count":5'))
        with self.assertRaises(NeedsHuman):
            atlas.parse_search_page(valid.replace('"count":1', '"count":0'))
        with self.assertRaises(NeedsHuman):
            atlas.parse_search_page(valid, page=2)
        with self.assertRaises(NeedsHuman):
            atlas.parse_search_page(valid.replace('bookID=153493', 'bookX=153493'))
        with self.assertRaises(NeedsHuman):
            atlas.parse_search_page(valid.replace('<span class="active">现行</span>', ''))


class AtlasDetailParserTest(unittest.TestCase):
    def test_parse_withdrawn_detail_extracts_replacement(self) -> None:
        detail = atlas.parse_detail_page(_read("detail_61268_16g101_1.html"), book_id="61268")
        self.assertEqual(detail.book_id, "61268")
        self.assertEqual(detail.code, "16G101-1")
        self.assertEqual(detail.status, "superseded")
        self.assertEqual(detail.replaced_by, "22G101-1")
        self.assertEqual(detail.replaces, "")
        self.assertEqual(detail.resource_type, "国标图集")
        self.assertEqual(detail.publish_date, "2016-09-01")
        self.assertEqual(detail.implement_date, "2016-09-01")
        self.assertEqual(detail.abolish_date, "2022-05-01")
        self.assertIn("混凝土结构施工图", detail.name)

    def test_parse_current_detail_extracts_replaces_and_editor(self) -> None:
        detail = atlas.parse_detail_page(_read("detail_153493_22g101_1.html"), book_id="153493")
        self.assertEqual(detail.book_id, "153493")
        self.assertEqual(detail.code, "22G101-1")
        self.assertEqual(detail.status, "current")
        self.assertEqual(detail.replaces, "16G101-1")
        self.assertEqual(detail.replaced_by, "")
        self.assertEqual(detail.editor, "中国建筑标准设计研究院有限公司")

    def test_withdrawn_without_replacement_stays_withdrawn(self) -> None:
        raw = _read("detail_61268_16g101_1.html").replace(
            '<a style="font-weight: bold;" href="https://ebook.chinabuilding.com.cn/zbooklib/book/detail/show?SiteID=1&bookID=153493">22G101-1</a>',
            "-",
        )
        detail = atlas.parse_detail_page(raw, book_id="61268")
        self.assertEqual(detail.status, "withdrawn")
        self.assertEqual(detail.replaced_by, "")

    def test_detail_fail_closed_on_missing_markers(self) -> None:
        with self.assertRaises(NeedsHuman):
            atlas.parse_detail_page("<html><body>broken</body></html>", book_id="1")


class AtlasConciseCatalogueParserTest(unittest.TestCase):
    def test_parse_concise_catalogue_sample_dedup_and_fields(self) -> None:
        entries = atlas.parse_concise_catalogue(_read("concise_catalogue_sample.html"))
        # 22 行含 1 条重复 bookID=55832 (11G101-1)，去重后应正好 21 条唯一图集。
        self.assertEqual(len(entries), 21)
        by_code = {e.code: e for e in entries}

        # title 属性反截断 & 实体解码验证
        dbj = by_code["DBJ/T02-81-2013-12D1"]
        self.assertEqual(dbj.book_id, "76171")
        self.assertEqual(dbj.status, "current")

        j22 = by_code["22J403-1"]
        self.assertEqual(j22.name, "楼梯 栏杆 栏板（一）")
        self.assertEqual(j22.replaces, "15J403-1")

        # 替代与被替代关系
        g22 = by_code["22G101-1"]
        self.assertEqual(g22.status, "current")
        self.assertEqual(g22.replaces, "16G101-1")
        self.assertEqual(g22.replaced_by, "")

        g16 = by_code["16G101-1"]
        self.assertEqual(g16.status, "superseded")
        self.assertEqual(g16.replaced_by, "22G101-1")

        ss06 = by_code["06SS127"]
        self.assertEqual(ss06.status, "superseded")
        self.assertEqual(ss06.replaced_by, "25S127")

        dx = by_code["DX003～004"]
        self.assertEqual(dx.status, "superseded")
        self.assertEqual(dx.replaced_by, "25DX003")

        g11 = by_code["11G101-1"]
        self.assertEqual(g11.status, "withdrawn")
        self.assertEqual(g11.replaced_by, "")

        # 省标与中南标
        self.assertEqual(by_code["粤25G/T116"].status, "current")
        self.assertEqual(by_code["21ZJ111"].status, "current")

    def test_parse_concise_catalogue_fail_closed(self) -> None:
        with self.assertRaises(NeedsHuman):
            atlas.parse_concise_catalogue("<html><body>not concise</body></html>")
        with self.assertRaises(NeedsHuman):
            atlas.parse_concise_catalogue('<table class="menuTd"><tbody></tbody></table>')


if __name__ == "__main__":
    unittest.main()
