"""编号角色判定 —— 全仓风险最高的逻辑，必须钉死真实证据。"""

import unittest

from guiju.domain.codes import Confidence, Role, classify, normalize_key


class NormalizeTest(unittest.TestCase):
    def test_variants_collapse(self):
        for raw in ("GB 55037-2022", "gb55037-2022", "GB55037－2022", "GB55037—2022", " GB 55037 - 2022 "):
            self.assertEqual(normalize_key(raw), "GB55037-2022", raw)


class RoleTest(unittest.TestCase):
    def test_revision_sentence_wins_as_primary(self):
        # 真实页面原文（2026-09-23 局部修订公告）
        text = (
            "现批准《老年人照料设施建筑设计标准》JGJ450-2018局部修订的条文，自2027年1月1日起实施。"
            "标准编号修改为JGJ/T450-2018。局部修订后的条文将另行发布。"
        )
        codes = classify(text, "老年人照料设施建筑设计标准")
        self.assertEqual(codes.primary_key, "JGJ/T450-2018")
        self.assertEqual(codes.confidence, Confidence.PARSED)
        roles = {m.code_key: m.role for m in codes.mentions}
        self.assertIs(roles["JGJ/T450-2018"], Role.TARGET_OF_REVISION)

    def test_superseded_is_not_primary(self):
        # 真实案例：新标准公告同时写了被代替的旧编号
        text = "现批准《住宅性能评定标准》为国家标准，编号为GB/T50362-2022，自2023年5月1日起实施。原《住宅性能评定标准》GB/T50362-2005同时废止。"
        codes = classify(text, "住宅性能评定标准")
        self.assertEqual(codes.primary_key, "GB/T50362-2022")
        roles = {m.code_key: m.role for m in codes.mentions}
        self.assertIs(roles["GB/T50362-2005"], Role.SUPERSEDED_REF)
        self.assertIn("GB/T50362-2005", [m.code_key for m in codes.superseded])

    def test_no_code_returns_none(self):
        codes = classify("住房城乡建设部关于印发2016年工程建设标准实施指导监督重点研究工作计划的通知。")
        self.assertIsNone(codes.primary_key)
        self.assertEqual(codes.confidence, Confidence.NONE)

    def test_two_bare_mentions_are_ambiguous(self):
        # 无任何「代替/修改为/发布」证据时，不允许假装确定
        codes = classify("《建筑拆除工程安全技术规范》JGJ147-2016与JGJ147-2004均已列入目录。")
        self.assertEqual(codes.confidence, Confidence.AMBIGUOUS)
        self.assertEqual(len(codes.candidates), 2)

    def test_html_is_reduced_to_text(self):
        html = "<p>编号为<span>GB</span>&nbsp;55037-2022</p>"
        codes = classify(html)
        self.assertEqual(codes.primary_key, "GB55037-2022")


if __name__ == "__main__":
    unittest.main()
