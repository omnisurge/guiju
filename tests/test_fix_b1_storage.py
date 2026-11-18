"""B1 P2-5/6/7：公告更新、mention 替换、同族降级与 confidence 证据。"""

import tempfile
import unittest
from pathlib import Path

from guiju.domain.codes import Codes, Confidence, Mention, Role
from guiju.store.sqlite import Store, persist_codes


class AnnouncementUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)

    def test_update_keeps_id_and_first_fetched_at(self):
        ann_id, fresh = self.store.upsert_announcement(
            "https://x/1", "2020-01-01", "旧标题", "旧名称", "other"
        )
        self.assertTrue(fresh)
        old_fetched = self.store.get_ann(ann_id)["fetched_at"]
        same_id, fresh2 = self.store.upsert_announcement(
            "https://x/1", "2026-02-02", "新标题", "新名称", "standard"
        )
        row = self.store.get_ann(ann_id)
        self.assertFalse(fresh2)
        self.assertEqual(same_id, ann_id)
        self.assertEqual(row["title"], "新标题")
        self.assertEqual(row["doc_name"], "新名称")
        self.assertEqual(row["published_on"], "2026-02-02")
        self.assertEqual(row["category"], "standard")
        self.assertEqual(row["fetched_at"], old_fetched)


class PersistCodesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)
        self.ann_id, _ = self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "标题", "名称", "other"
        )

    def _codes(self, primary, mentions):
        return Codes(
            mentions=tuple(mentions),
            primary_key=primary,
            primary_display=primary,
            confidence=Confidence.PARSED,
        )

    def test_rescan_replaces_mentions(self):
        first = self._codes(
            "GB50016-2014",
            [Mention("GB50016-2014", "GB 50016-2014", Role.PRIMARY, "发布")],
        )
        persist_codes(self.store, self.ann_id, first, "名称")
        second = self._codes(
            "GB50011-2010",
            [Mention("GB50011-2010", "GB 50011-2010", Role.PRIMARY, "发布")],
        )
        persist_codes(self.store, self.ann_id, second, "名称")
        keys = [r["code_key"] for r in self.store.mentions_for(self.ann_id)]
        self.assertEqual(keys, ["GB50011-2010"])

    def test_unrelated_mention_is_not_demoted(self):
        codes = self._codes(
            "GB55037-2022",
            [
                Mention("GB55037-2022", "GB 55037-2022", Role.PRIMARY, "发布"),
                Mention("GB50016-2014", "GB 50016-2014", Role.MENTIONED, "参考"),
            ],
        )
        persist_codes(self.store, self.ann_id, codes, "名称")
        rows = {r["code_key"]: r for r in self.store.mentions_for(self.ann_id)}
        self.assertEqual(rows["GB50016-2014"]["role"], "mentioned")
        self.assertEqual(rows["GB55037-2022"]["role"], "primary")

    def test_same_family_older_mention_is_demoted(self):
        codes = self._codes(
            "GB/T50362-2022",
            [
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.PRIMARY, "发布"),
                Mention("GB/T50362-2005", "GB/T 50362-2005", Role.MENTIONED, "同页出现"),
            ],
        )
        persist_codes(self.store, self.ann_id, codes, "名称")
        rows = {r["code_key"]: r for r in self.store.mentions_for(self.ann_id)}
        self.assertEqual(rows["GB/T50362-2005"]["role"], "superseded_ref")

    def test_ambiguous_target_is_not_persisted_as_parsed(self):
        codes = Codes(
            mentions=(
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.TARGET_OF_REVISION, "修改为"),
                Mention("GB/T50378-2019", "GB/T 50378-2019", Role.TARGET_OF_REVISION, "修改为"),
            ),
            confidence=Confidence.AMBIGUOUS,
        )
        persist_codes(self.store, self.ann_id, codes, "名称")
        rows = {r["code_key"]: r for r in self.store.mentions_for(self.ann_id)}
        self.assertEqual(rows["GB/T50362-2022"]["role"], "target_of_revision")
        self.assertEqual(rows["GB/T50378-2019"]["role"], "target_of_revision")
        conf = {
            r["code_key"]: r["confidence"]
            for r in self.store.db.execute("SELECT code_key,confidence FROM edition")
        }
        self.assertEqual(conf["GB/T50362-2022"], "ambiguous")
        self.assertEqual(conf["GB/T50378-2019"], "ambiguous")

    def test_non_primary_confidence_follows_evidence(self):
        codes = self._codes(
            "GB55037-2022",
            [
                Mention("GB55037-2022", "GB 55037-2022", Role.PRIMARY, "发布"),
                Mention("GB50016-2014", "GB 50016-2014", Role.MENTIONED, "参考"),
                Mention("JGJ147-2016", "JGJ 147-2016", Role.WEAK_PRIMARY, "提及"),
                Mention("CJ/T133-2007", "CJ/T 133-2007", Role.SUPERSEDED_REF, "废止"),
            ],
        )
        persist_codes(self.store, self.ann_id, codes, "名称")
        rows = {
            r["code_key"]: r
            for r in self.store.db.execute("SELECT code_key,confidence FROM edition")
        }
        self.assertEqual(rows["GB55037-2022"]["confidence"], "parsed")
        self.assertEqual(rows["GB50016-2014"]["confidence"], "ambiguous")
        self.assertEqual(rows["JGJ147-2016"]["confidence"], "ambiguous")
        self.assertEqual(rows["CJ/T133-2007"]["confidence"], "parsed")


if __name__ == "__main__":
    unittest.main()
