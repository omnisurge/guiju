"""B1/W6 · P1-9 / P2-19 / 年版编号：编号角色判定的边界。

- P1-9：多个 target_of_revision 无条件取 max(code_key) 会静默选错编号；
  target 分支必须与 primary 同规则：多 target、target+primary 跨族 → 32。
- P2-19：发布词表缺「批准」，句号分段的「现批准…编号为…。自…实施。」
  被判 weak_primary/ambiguous。
- _CODE_RE 认 DG/TJ08-2007-2026 这类「年版」；「废止GB …」语序判旧版。
"""

import unittest

from guiju.domain import codes
from guiju.domain.codes import Confidence, Role, classify


class MultiTargetTest(unittest.TestCase):
    """真实评审里的两个句式（docs/audit/code-review-2026-11-18.md P1-9）。"""

    def test_multi_target_classification_is_ambiguous(self):
        # 句式 A：两本标准同时局部修订，各自改写编号
        text = (
            "《甲标准》局部修订，编号修改为GB/T 50362-2022；"
            "《乙标准》局部修订，编号修改为GB/T 50378-2019。"
        )
        got = classify(text)
        self.assertIsNone(got.primary_key, "两本各改各的号，不能按 code_key 大小挑一个")
        self.assertEqual(got.confidence, Confidence.AMBIGUOUS)
        roles = {m.code_key: m.role for m in got.mentions}
        self.assertIs(roles["GB/T50362-2022"], Role.TARGET_OF_REVISION)
        self.assertIs(roles["GB/T50378-2019"], Role.TARGET_OF_REVISION)

    def test_publish_plus_foreign_revision_is_ambiguous(self):
        # 句式 B：发布甲（primary）+ 局部修订乙（target）—— 公告发布的是甲，
        # 不能因为 target 优先级更高就把乙的修订号当 primary。
        text = (
            "现批准《甲标准》为国家标准，编号为GB 55037-2022，自2023年1月1日起实施；"
            "《乙标准》局部修订，编号修改为GB/T 50378-2019。"
        )
        got = classify(text)
        self.assertIsNone(got.primary_key)
        self.assertEqual(got.confidence, Confidence.AMBIGUOUS)
        roles = {m.code_key: m.role for m in got.mentions}
        self.assertIs(roles["GB55037-2022"], Role.PRIMARY)
        self.assertIs(roles["GB/T50378-2019"], Role.TARGET_OF_REVISION)

    def test_single_target_is_still_parsed(self):
        # 真实页面（2026-09-23 局部修订公告）：target 与 primary 是同一本规范的两个写法
        text = (
            "现批准《老年人照料设施建筑设计标准》JGJ450-2018局部修订的条文，自2027年1月1日起实施。"
            "标准编号修改为JGJ/T450-2018。"
        )
        got = classify(text, "老年人照料设施建筑设计标准")
        self.assertEqual(got.primary_key, "JGJ/T450-2018")
        self.assertEqual(got.confidence, Confidence.PARSED)

    def test_single_target_plus_unrelated_mention_keeps_parsed(self):
        # 「参考…」只是提到另一本，不是本次发布的竞争证据
        text = "标准编号修改为JGJ/T450-2018。参考GB 50016-2014。"
        got = classify(text)
        self.assertEqual(got.primary_key, "JGJ/T450-2018")
        self.assertEqual(got.confidence, Confidence.PARSED)
        roles = {m.code_key: m.role for m in got.mentions}
        self.assertIs(roles["GB50016-2014"], Role.MENTIONED)

    def test_target_older_than_primary_does_not_win(self):
        # 真实陷阱：批准 2022 版，后文却把局部修订号写成 2021；不能按 target 优先级选旧版。
        text = (
            "现批准《某标准》为国家标准，编号为GB/T 50362-2022，自2023年1月1日起实施；"
            "《某标准》局部修订，编号修改为GB/T 50362-2021。"
        )
        got = classify(text, "某标准")
        self.assertEqual(got.primary_key, "GB/T50362-2022")
        self.assertEqual(got.confidence, Confidence.PARSED)
        roles = {m.code_key: m.role for m in got.mentions}
        self.assertIs(roles["GB/T50362-2021"], Role.SUPERSEDED_REF)

    def test_two_targets_same_subject_are_not_parsed(self):
        # 同一本被写了两遍改号（异常排版）：可以给兜底 primary，但 confidence 不许是 parsed
        text = "标准编号修改为GB/T 50362-2021；标准编号修改为GB/T 50362-2022。"
        got = classify(text)
        self.assertEqual(got.confidence, Confidence.AMBIGUOUS)
        self.assertEqual(got.primary_key, "GB/T50362-2022")
        # 没有决定性证据时不许把另一条降级成 superseded_ref（那会把猜伪装成判）
        roles = {m.code_key: m.role for m in got.mentions}
        self.assertIs(roles["GB/T50362-2021"], Role.TARGET_OF_REVISION)


class PublishWordTest(unittest.TestCase):
    """P2-19：「批准」也是发布措辞；但不能因此溅到同一句里的其他编号。"""

    def test_approval_sentence_split_by_period_is_parsed(self):
        text = (
            "现批准《建筑防火通用规范》为国家标准，编号为GB 55037-2022。"
            "自2023年6月1日起实施。"
        )
        got = classify(text, "建筑防火通用规范")
        self.assertEqual(got.primary_key, "GB55037-2022")
        self.assertEqual(got.confidence, Confidence.PARSED)
        self.assertIs(got.mentions[0].role if len(got.mentions) == 1 else None, Role.PRIMARY)

    def test_approval_does_not_promote_code_in_other_clause(self):
        text = (
            "现批准《建筑防火通用规范》为国家标准，编号为GB 55037-2022，"
            "原GB 50016-2014继续有效。"
        )
        got = classify(text)
        roles = {m.code_key: m.role for m in got.mentions}
        # 同句两个编号：发布动词只喂饱它自己的子句，另一个编号不许被「批准」带成 primary。
        self.assertIsNot(roles["GB50016-2014"], Role.PRIMARY)
        # 同句多编号时保持原有的保守口径（audit P2-19 只要求补「批准」，
        # 不要求把"同句两编号"的句法规则一并放宽）——结论必须是 ambiguous，不许伪装确定。
        self.assertIs(roles["GB55037-2022"], Role.WEAK_PRIMARY)
        self.assertEqual(got.confidence, Confidence.AMBIGUOUS)


class VoidWordOrderTest(unittest.TestCase):
    """「废止GB …」：动词在编号之前，编号是宾语 → 旧版。"""

    def test_void_before_code_marks_superseded(self):
        text = (
            "现批准《建筑防火通用规范》为国家标准，编号为GB 55037-2022，"
            "自2023年6月1日起实施。废止GB 50016-2014。"
        )
        got = classify(text)
        roles = {m.code_key: m.role for m in got.mentions}
        self.assertIs(roles["GB50016-2014"], Role.SUPERSEDED_REF)
        self.assertEqual(got.primary_key, "GB55037-2022")
        self.assertEqual(got.confidence, Confidence.PARSED)

    def test_void_before_quoted_name_marks_superseded(self):
        got = classify("废止《建筑设计防火规范》GB 50016-2014。")
        roles = {m.code_key: m.role for m in got.mentions}
        self.assertIs(roles["GB50016-2014"], Role.SUPERSEDED_REF)


class YearEditionCodeTest(unittest.TestCase):
    """_CODE_RE 必须把 DG/TJ08-2007-2026 整条认成一个编号（年版）。"""

    def test_regex_groups_for_year_edition(self):
        m = codes._CODE_RE.search("DG/TJ08-2007-2026")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "DG/TJ")
        self.assertEqual(m.group(2), "08-2007", "年版编号的基号带一段年份")
        self.assertEqual(m.group(3), "2026")
        self.assertEqual(
            codes.normalize_key(m.group(0)), "DG/TJ08-2007-2026"
        )

    def test_regular_codes_keep_groups(self):
        m = codes._CODE_RE.search("GB 55037-2022")
        self.assertEqual(m.group(2), "55037")
        self.assertEqual(m.group(3), "2022")
        m2 = codes._CODE_RE.search("CJJ 1-1990")
        self.assertEqual(m2.group(2), "1")
        self.assertEqual(m2.group(3), "1990")

    def test_year_edition_is_primary(self):
        text = (
            "现批准《地质灾害危险性评估技术标准》为上海市工程建设规范，"
            "统一编号为DG/TJ08-2007-2026，自2026年7月1日起实施。"
        )
        got = classify(text, "地质灾害危险性评估技术标准")
        self.assertEqual(got.primary_key, "DG/TJ08-2007-2026")
        self.assertEqual(got.primary_display, "DG/TJ 08-2007-2026")
        self.assertEqual(got.confidence, Confidence.PARSED)

    def test_previous_year_edition_is_superseded(self):
        text = (
            "现批准《地质灾害危险性评估技术标准》为上海市工程建设规范，"
            "统一编号为DG/TJ08-2007-2026，自2026年7月1日起实施。"
            "原DG/TJ08-2007-2018同时废止。"
        )
        got = classify(text, "地质灾害危险性评估技术标准")
        roles = {m.code_key: m.role for m in got.mentions}
        self.assertIs(roles["DG/TJ08-2007-2018"], Role.SUPERSEDED_REF)
        self.assertEqual(got.primary_key, "DG/TJ08-2007-2026")


if __name__ == "__main__":
    unittest.main()
