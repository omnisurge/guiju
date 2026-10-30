"""附件裁定 —— 抓不到是空，抓错是事故。"""

import unittest

from guiju.domain.attachment import Attachment, Verdict, pick


def atts(*labels):
    return [
        Attachment(url=f"https://www.mohurd.gov.cn/dl/{i}.pdf", label=label)
        for i, label in enumerate(labels)
    ]


class PickTest(unittest.TestCase):
    def test_name_match_when_two_attachments(self):
        # 官方常见形态：第一个是废止条文 .doc，第二个才是规范
        items = [
            Attachment(url="https://x.gov.cn/a.doc", label="废止的强制性条文"),
            Attachment(url="https://x.gov.cn/b.pdf", label="建筑防火通用规范"),
        ]
        got = pick(items, "建筑防火通用规范", announcement_title="关于发布国家标准的公告")
        self.assertIs(got.verdict, Verdict.OK)
        self.assertEqual(got.choice.label, "建筑防火通用规范")

    def test_english_version_announcement_is_rejected(self):
        # 实测 2022-01-18：4 个附件，旧逻辑兜底选中了无关的《建筑设计防火规范（2018年版）》
        items = atts("建筑设计防火规范（2018年版）", "石油化工钢制设备抗震设计标准", "住宅性能评定标准")
        got = pick(items, "石油化工钢制设备抗震设计标准",
                   announcement_title="关于发布《石油化工钢制设备抗震设计标准》等4项工程建设标准英文版的公告")
        self.assertIs(got.verdict, Verdict.NOT_APPLICABLE)
        self.assertIsNone(got.choice)

    def test_no_fallback_on_ambiguity(self):
        items = atts("甲规范", "乙规范", "丙规范")
        got = pick(items, "", announcement_title="关于发布若干标准的公告")
        self.assertIs(got.verdict, Verdict.AMBIGUOUS)
        self.assertIsNone(got.choice)
        self.assertEqual(len(got.candidates), 3)

    def test_single_attachment_without_name_is_ok(self):
        got = pick(atts("某某标准"), "", announcement_title="公告")
        self.assertIs(got.verdict, Verdict.OK)

    def test_all_deprecated_means_no_attachment(self):
        got = pick(atts("废止的强制性条文", "作废条文"), "某标准", announcement_title="公告")
        self.assertIs(got.verdict, Verdict.NO_ATTACHMENT)

    def test_single_candidate_with_truncated_label_is_ok(self):
        # 实测 2000-02-11：标准名「城市给水工程项目建设标准」，附件名被截断为「城市给水工程项目建设」
        got = pick(atts("城市给水工程项目建设"), "城市给水工程项目建设标准",
                   announcement_title="关于批准发布《城市给水工程项目建设标准》的通知")
        self.assertIs(got.verdict, Verdict.OK)
        self.assertIn("不完全匹配", got.reason)

    def test_empty_is_no_attachment(self):
        self.assertIs(pick([], "某标准").verdict, Verdict.NO_ATTACHMENT)


if __name__ == "__main__":
    unittest.main()
