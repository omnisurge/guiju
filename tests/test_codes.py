"""编号规范化、角色方向判定、多 target/批准措辞/废止语序/年版编号与弱证据边界测试。"""

from __future__ import annotations

import unittest

from guiju.domain import codes
from guiju.domain.codes import Confidence, Role, classify, normalize_key


class NormalizeTest(unittest.TestCase):
    def test_variants_collapse(self):
        for raw in ("GB 55037-2022", "gb55037-2022", "GB55037－2022", "GB55037—2022", " GB 55037 - 2022 "):
            self.assertEqual(normalize_key(raw), "GB55037-2022", raw)


class CodesSingleDigitTest(unittest.TestCase):
    def test_codes_parse_single_digit_serial(self) -> None:
        # 直接打编号正则：1 位序号（实测 CJJ 1-1990）必须命中
        m = codes._CODE_RE.search("CJJ 1-1990")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "1")
        self.assertEqual(m.group(3), "1990")
        self.assertEqual(codes.normalize_key("CJJ 1-1990"), "CJJ1-1990")
        # 回归：常规多位序号不受影响
        m2 = codes._CODE_RE.search("GB 55037-2022")
        self.assertIsNotNone(m2)
        self.assertEqual(m2.group(2), "55037")


class RoleTest(unittest.TestCase):
    def test_revision_sentence_wins_as_primary(self):
        # 真实页面原文（2026-09-23 局部修订公告）
        text = (
            "现批准《老年人照料设施建筑设计标准》JGJ450-2018局部修订的条文，自2027年1月1日起实施。"
            "标准编号修改为JGJ/T450-2018。局部修订后的条文将另行发布。"
        )
        parsed = classify(text, "老年人照料设施建筑设计标准")
        self.assertEqual(parsed.primary_key, "JGJ/T450-2018")
        self.assertEqual(parsed.confidence, Confidence.PARSED)
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertIs(roles["JGJ/T450-2018"], Role.TARGET_OF_REVISION)

    def test_superseded_is_not_primary(self):
        # 真实案例：新标准公告同时写了被代替的旧编号
        text = "现批准《住宅性能评定标准》为国家标准，编号为GB/T50362-2022，自2023年5月1日起实施。原《住宅性能评定标准》GB/T50362-2005同时废止。"
        parsed = classify(text, "住宅性能评定标准")
        self.assertEqual(parsed.primary_key, "GB/T50362-2022")
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertIs(roles["GB/T50362-2005"], Role.SUPERSEDED_REF)
        self.assertIn("GB/T50362-2005", [m.code_key for m in parsed.superseded])

    def test_no_code_returns_none(self):
        parsed = classify("住房城乡建设部关于印发2016年工程建设标准实施指导监督重点研究工作计划的通知。")
        self.assertIsNone(parsed.primary_key)
        self.assertEqual(parsed.confidence, Confidence.NONE)

    def test_two_bare_mentions_are_ambiguous(self):
        # 无任何「代替/修改为/发布」证据时，不允许假装确定
        parsed = classify("《建筑拆除工程安全技术规范》JGJ147-2016与JGJ147-2004均已列入目录。")
        self.assertEqual(parsed.confidence, Confidence.AMBIGUOUS)
        self.assertEqual(len(parsed.candidates), 2)

    def test_html_is_reduced_to_text(self):
        html = "<p>编号为<span>GB</span>&nbsp;55037-2022</p>"
        parsed = classify(html)
        self.assertEqual(parsed.primary_key, "GB55037-2022")


class DirectionTest(unittest.TestCase):
    """「X 代替 Y」的宾语才是旧版 —— 修复前整条方向是反的（把新编号判成了旧版）。"""

    def test_object_of_replace_is_superseded(self):
        parsed = classify("GB 50300-2022代替GB 50300-2013。")
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertIs(roles["GB50300-2022"], Role.PRIMARY)
        self.assertIs(roles["GB50300-2013"], Role.SUPERSEDED_REF)

    def test_object_chain_after_replace_word(self):
        # 「X 代替 Y1、Y2」：两个宾语都是旧版（顿号不是子句边界）
        parsed = classify("GB/T 50001-2020代替GB/T 50001-2010、GB/T 50002-2005。")
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertIs(roles["GB/T50001-2020"], Role.PRIMARY)
        self.assertIs(roles["GB/T50001-2010"], Role.SUPERSEDED_REF)
        self.assertIs(roles["GB/T50002-2005"], Role.SUPERSEDED_REF)

    def test_comma_splits_replacement_scope(self):
        # 「，」必须切断「代替」的宾语链：逗号后新编号不是宾语
        parsed = classify("批准发布GB 50016-2022，原GB 50016-2014同时废止。")
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertIs(roles["GB50016-2022"], Role.PRIMARY)
        self.assertIs(roles["GB50016-2014"], Role.SUPERSEDED_REF)

    def test_passive_replacement_subject_is_primary(self):
        # 「X 废止，由Y代替」：Y 是施动者（新版），不是旧版
        parsed = classify("经批准，JGJ 80-2016废止，由JGJ/T 80-2016代替。")
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertIs(roles["JGJ80-2016"], Role.SUPERSEDED_REF)
        self.assertIs(roles["JGJ/T80-2016"], Role.PRIMARY)

    def test_all_old_gives_no_primary(self):
        # 池子全是旧版 → 无 primary，不能拿最旧的一个凑数
        parsed = classify("本标准代替JGJ 147-2004。")
        self.assertIsNone(parsed.primary_key)

    def test_ambiguous_primary_does_not_demote(self):
        # 无决定性证据时，ambiguous 兜底 primary 不允许把同族旧版降成 superseded_ref
        parsed = classify("《建筑拆除工程安全技术规范》JGJ147-2016与JGJ147-2004均已列入目录。")
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertIs(roles["JGJ147-2004"], Role.MENTIONED)
        self.assertIs(roles["JGJ147-2016"], Role.MENTIONED)
        self.assertEqual(parsed.confidence, Confidence.AMBIGUOUS)


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
        # 同句多编号时保持原有的保守口径 —— 结论必须是 ambiguous，不许伪装确定。
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


class CodesWeakPrimaryTest(unittest.TestCase):
    def test_publish_verb_in_other_clause_does_not_promote(self):
        # 「实施」和 JGJ147-2004 不同子句：它只是顺带提的旧版，不是本次发布对象。
        parsed = classify(
            "现批准《建筑拆除工程安全技术规范》JGJ147-2016，自2027年1月1日起实施。"
            "JGJ147-2004届时不再执行。"
        )
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertEqual(roles["JGJ147-2016"], Role.PRIMARY)
        # 关键：另一个编号不得被泛动词升成 primary（被 demote 成 superseded 也允许）
        self.assertNotEqual(roles["JGJ147-2004"], Role.PRIMARY)
        self.assertEqual(parsed.primary_key, "JGJ147-2016")
        self.assertEqual(parsed.confidence, Confidence.PARSED)

    def test_publish_verb_single_code_stays_primary(self):
        parsed = classify(
            "《住宅性能评定标准》编号为GB/T50362-2022，自2023年5月1日起实施。"
        )
        self.assertEqual(parsed.mentions[0].role, Role.PRIMARY)
        self.assertEqual(parsed.confidence, Confidence.PARSED)

    def test_doc_name_only_match_is_weak(self):
        # doc_name 出现在句子里、但没有发布动词：最多是弱证据。
        parsed = classify(
            "已列入目录的标准需符合《住宅性能评定标准》GB/T50362-2022相关条款。",
            doc_name="住宅性能评定标准",
        )
        self.assertEqual(parsed.mentions[0].role, Role.WEAK_PRIMARY)
        # 弱证据不算决定性：confidence 必须停在 ambiguous
        self.assertEqual(parsed.confidence, Confidence.AMBIGUOUS)
        self.assertEqual(parsed.primary_key, "GB/T50362-2022")

    def test_weak_primary_yields_to_superseded(self):
        # 同一句里「旧版废止」是强证据：弱新版不能压过它。
        parsed = classify(
            "JGJ147-2004经审核现已废止。"
            "相关内容见《拆除工程施工安全技术规程》JGJ147-2016适用范围。",
            doc_name="拆除工程施工安全技术规程",
        )
        roles = {m.code_key: m.role for m in parsed.mentions}
        self.assertEqual(roles["JGJ147-2004"], Role.SUPERSEDED_REF)
        self.assertEqual(roles["JGJ147-2016"], Role.WEAK_PRIMARY)
        self.assertEqual(parsed.confidence, Confidence.AMBIGUOUS)

    def test_multiple_primaries_different_stem_are_ambiguous(self):
        parsed = classify("批准发布GB50495-2019和GB/T51367-2019两项标准。")
        self.assertIsNone(parsed.primary_key)
        self.assertEqual(parsed.confidence, Confidence.AMBIGUOUS)
        keys = {m.code_key for m in parsed.mentions}
        self.assertEqual(keys, {"GB50495-2019", "GB/T51367-2019"})

    def test_multiple_primaries_same_stem_pick_newest(self):
        # 同族（同 code_stem）多 primary 没有歧义：新版就是答案。
        parsed = classify(
            "批准《老年人照料设施建筑设计标准》JGJ/T450-2018实施，JGJ/T450-2008同时发布。"
        )
        self.assertEqual(parsed.primary_key, "JGJ/T450-2018")
        self.assertEqual(parsed.confidence, Confidence.PARSED)


if __name__ == "__main__":
    unittest.main()
