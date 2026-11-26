"""解析链：编号 → 权威名称 → 本地公告、三小修、附件三态、currency 精确/同族匹配、主机匹配与置信度。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.application import resolve as lookup_mod
from guiju.application.resolve import match_currency, resolve, source_of
from guiju.currency import Record
from guiju.errors import AmbiguousTarget, GuijuError, NotIndexed
from guiju.store.sqlite import Store


class FakeCurrency:
    name = "fake"

    def __init__(self, records=(), *, complete=True, empty_authoritative=False, reason=""):
        self.records = list(records)
        self.complete = complete
        self.empty_authoritative = empty_authoritative
        self.incomplete_reason = reason

    def lookup(self, code):
        return list(self.records)


class _LookupCase(unittest.TestCase):
    def _make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.db")
        self.addCleanup(store.close)
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/a1.html", "2022-01-01",
            "住房和城乡建设部关于发布国家标准《建筑防火通用规范》的公告", "建筑防火通用规范", "national",
        )
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/a2.html", "2015-05-01",
            "关于发布国家标准《建筑设计防火规范》的公告", "建筑设计防火规范", "national",
        )
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/a3.html", "2019-01-01",
            "关于发布国家标准《建筑设计防火规范》局部修订的公告", "建筑设计防火规范", "partial_revision",
        )
        return store


class ByRefTest(_LookupCase):
    def setUp(self):
        self.store = self._make_store()

    def test_by_url(self):
        got = resolve(self.store, "https://www.mohurd.gov.cn/a1.html", FakeCurrency([]))
        self.assertEqual(got["ann"]["doc_name"], "建筑防火通用规范")
        self.assertEqual(got["via"], "url")

    def test_by_id(self):
        got = resolve(self.store, "1", FakeCurrency([]))
        self.assertEqual(got["via"], "ann_id")

    def test_by_name_unique(self):
        got = resolve(self.store, "建筑防火通用规范", FakeCurrency([]))
        self.assertEqual(got["ann"]["id"], 1)
        self.assertEqual(got["via"], "name")

    def test_by_name_ambiguous(self):
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "建筑设计防火规范", FakeCurrency([]), allow_network=False)

    def test_by_name_not_found(self):
        with self.assertRaises(NotIndexed):
            resolve(self.store, "钢结构设计标准", FakeCurrency([]))


class ByCodeWithoutIndexTest(_LookupCase):
    """核心用例：本地从未索引过任何编号，仍能靠权威名称定位公告。"""

    def setUp(self):
        self.store = self._make_store()

    def test_code_resolved_via_currency_name(self):
        cur = FakeCurrency([Record(code="GB 55037-2022", status="current", name="建筑防火通用规范")])
        got = resolve(self.store, "GB55037-2022", cur, allow_network=False)
        self.assertEqual(got["ann"]["id"], 1)
        self.assertEqual(got["via"], "code_via_currency")
        self.assertEqual(got["status"], "current")

    def test_superseded_status_is_surfaced(self):
        cur = FakeCurrency([Record(code="GB 55037-2017", status="superseded", name="建筑防火通用规范")])
        got = resolve(self.store, "GB55037-2017", cur, allow_network=False)
        self.assertEqual(got["status"], "superseded")

    def test_currency_name_ambiguous(self):
        cur = FakeCurrency([Record(code="X-1", status="current", name="建筑设计防火规范")])
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "X-1", cur, allow_network=False)

    def test_currency_has_no_name(self):
        with self.assertRaises(NotIndexed):
            resolve(self.store, "GB99999-2099", FakeCurrency([]))

    def test_currency_name_without_local_announcement(self):
        cur = FakeCurrency([Record(code="Y-1", status="current", name="钢结构设计标准")])
        with self.assertRaises(GuijuError):
            resolve(self.store, "Y-1", cur)


class RoleAndVerificationTest(_LookupCase):
    """两个会"拿错书"的坑：被别的公告提到、以及同名公告的核实。"""

    def setUp(self):
        self.store = self._make_store()

    def test_mentioned_code_is_not_the_publisher(self):
        """GB 50016-2014 只在《建筑防火通用规范》里被"代替"，不能因此定位到那本。"""
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(1, "GB50016-2014", "superseded_ref", "代替GB 50016-2014")
        cur = FakeCurrency([
            Record(code="GB 50016-2014", status="current", name="建筑设计防火规范",
                   implement_date="2015-05-01"),
        ])
        got = resolve(self.store, "GB 50016-2014", cur, allow_network=False)
        self.assertEqual(got["ann"]["doc_name"], "建筑设计防火规范")
        self.assertNotEqual(got["ann"]["id"], 1)

    def test_verification_picks_announcement_whose_text_has_the_code(self):
        cur = FakeCurrency([Record(code="GB 50016-2014", status="current", name="建筑设计防火规范")])

        def fake_detail(url):
            if url.endswith("a3.html"):
                return "现批准《建筑设计防火规范》为国家标准，编号为GB 50016-2014。"
            return "现批准《建筑设计防火规范》为国家标准，编号为GB 50099-2011。"

        got = resolve(self.store, "GB 50016-2014", cur, fetch_detail=fake_detail)
        self.assertEqual(got["ann"]["id"], 3)

    def test_name_mismatch_without_verification_evidence_is_refused(self):
        cur = FakeCurrency([Record(code="X-9", status="current", name="建筑抗震设计规范")])
        with self.assertRaises(NotIndexed):
            resolve(self.store, "X-9", cur, fetch_detail=lambda url: "编号为GB 50099-2011")


class SourceLabelTest(unittest.TestCase):
    """候选里必须能看出这条来自哪个源（同名跨源是真实情况）。"""

    def test_source_of(self):
        self.assertEqual(
            source_of("https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2024/art_1.html")["id"], "mohurd"
        )
        self.assertEqual(
            source_of("https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWeb/pc/#/standardQuery?keyword=x")["id"],
            "shzjw",
        )
        self.assertEqual(source_of("")["id"], "unknown")


class LookupFixesTest(unittest.TestCase):
    def test_dedupe_keeps_distinct_empty_key_rows(self) -> None:
        from guiju.application.resolve import _dedupe

        rows = [
            {"code_key": "", "id": 1},
            {"code_key": "", "id": 2},
            {"code_key": "", "url": "u3"},
            {"code_key": "GB55037-2022", "id": 4},
            {"code_key": "GB55037-2022", "id": 5},
        ]
        out = _dedupe(rows)
        self.assertEqual([r.get("id") for r in out], [1, 2, None, 4])

    def test_version_window_keeps_undated_hits(self) -> None:
        from guiju.application.resolve import _filter_by_version_window

        class H:
            def __init__(self, d):
                self.entry = {"published_on": d}

        hits = [H("2015-01-01"), H("2026-01-01"), H(None), H("")]
        out = _filter_by_version_window(hits, "2018-10-01")
        dates = [h.entry["published_on"] for h in out]
        self.assertNotIn("2026-01-01", dates)            # 晚于实施日：有证据，滤掉
        self.assertIn("2015-01-01", dates)
        self.assertEqual(dates.count(None) + dates.count(""), 2)  # 无日期：无证据，保留

    def test_name_match_level_tiers(self) -> None:
        from guiju.application.resolve import _best_name_hits, _name_match_level

        self.assertEqual(_name_match_level("建筑设计防火规范", "建筑设计防火规范"), 3)
        self.assertEqual(_name_match_level("《建筑设计防火规范》", "建筑设计防火规范"), 2)
        self.assertEqual(_name_match_level("建筑设计防火规范修订", "建筑设计防火规范"), 1)
        self.assertEqual(_name_match_level("城市给水工程", "建筑设计防火规范"), 0)

        class H:
            def __init__(self, name):
                self.entry = {"name": name}

        hits = [H("建筑设计防火规范修订"), H("建筑设计防火规范"), H("完全不相关")]
        best = _best_name_hits(hits, "建筑设计防火规范")
        self.assertEqual([h.entry["name"] for h in best], ["建筑设计防火规范"])

        # 没有精确命中时降级用"仅互含"，保持旧行为
        hits2 = [H("建筑设计防火规范修订"), H("完全不相关")]
        best2 = _best_name_hits(hits2, "建筑设计防火规范")
        self.assertEqual([h.entry["name"] for h in best2], ["建筑设计防火规范修订"])


class HasAttachmentTriStateTest(unittest.TestCase):
    """M9 附带修复：has_attachment=None（没核实）≠ False（核实没有）。"""

    def _two_version_store(self) -> Store:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.db")
        self.addCleanup(store.close)
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/a1.html", "2015-05-01",
            "关于发布国家标准《建筑设计防火规范》的公告", "建筑设计防火规范", "national",
        )
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/a2.html", "2019-01-01",
            "关于发布国家标准《建筑设计防火规范》局部修订的公告", "建筑设计防火规范", "partial_revision",
        )
        return store

    def test_none_not_filtered(self):
        """None 行必须保留参选：否则唯二候选被漏成 None。"""
        store = self._two_version_store()
        cur = mock.Mock()
        cur.lookup.return_value = [Record(code="GB 50016-2014", status="current", name="x")]
        store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        store.add_mention(1, "GB50016-2014", "primary", "e1")
        store.add_mention(2, "GB50016-2014", "target_of_revision", "e2")
        target = resolve(store, "建筑设计防火规范", cur, sources={"mohurd": mock.Mock()})
        self.assertIsNotNone(target)
        self.assertIn(target["ann"]["url"], ("https://www.mohurd.gov.cn/a1.html",
                                             "https://www.mohurd.gov.cn/a2.html"))

    def test_none_loses_to_true(self):
        """核实有附件(True)的公告优先于没核实(None)的。"""
        store = self._two_version_store()
        store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        store.add_mention(1, "GB50016-2014", "primary", "e1")
        store.add_mention(2, "GB50016-2014", "target_of_revision", "e2")
        cur = mock.Mock()
        cur.lookup.return_value = [Record(code="GB 50016-2014", status="current", name="x")]
        src = mock.Mock()
        src.detail.return_value = ("<html>a.pdf</html>", "正文")
        src.attachments.return_value = [mock.Mock()]
        target = resolve(store, "建筑设计防火规范", cur, sources={"mohurd": src})
        self.assertEqual(target["ann"]["url"], "https://www.mohurd.gov.cn/a2.html")


class MatchCurrencyTest(unittest.TestCase):
    """M13：mine 精确匹配 + current 同族优先。"""

    def test_exact_mine_and_same_family_current(self):
        recs = [
            Record(code="GB 50016-2014", status="superseded", name="old"),
            Record(code="GB 50016-2018", status="current", name="new"),
            Record(code="GB 50017-2018", status="current", name="other-family"),
        ]
        mine, current = match_currency(recs, "GB50016-2014")
        self.assertIs(mine, recs[0])
        self.assertIs(current, recs[1])

    def test_mine_none_current_first(self):
        recs = [Record(code="JGJ 1-2020", status="current", name="a")]
        mine, current = match_currency(recs, "GB9999-2099")
        self.assertIsNone(mine)
        self.assertIs(current, recs[0])

    def test_pick_record_english_excluded(self):
        recs = [
            Record(code="GB 50016-2014E", status="current", name="Code for fire protection (English)"),
            Record(code="GB 50016-2014", status="superseded", name="建筑设计防火规范"),
        ]
        picked = lookup_mod._pick_record(recs, "GB50016-2014")
        self.assertEqual(picked.code, "GB 50016-2014")


class SourceOfHostTest(unittest.TestCase):
    """W3-8：主机匹配必须精确，不能被 mohurd.gov.cn.evil.com / evil-mohurd.gov.cn 骗过。"""

    def test_malicious_hosts_are_unknown(self):
        for url in (
            "https://mohurd.gov.cn.evil.com/x.html",
            "https://evil-mohurd.gov.cn/x.html",
            "https://evil.example/mohurd.gov.cn",
        ):
            with self.subTest(url=url):
                self.assertEqual(lookup_mod.source_of(url)["id"], "unknown")

    def test_real_subdomains_still_match(self):
        self.assertEqual(lookup_mod.source_of("https://www.mohurd.gov.cn/x.html")["id"], "mohurd")
        self.assertEqual(lookup_mod.source_of("https://bzjg.zjw.sh.gov.cn/x")["id"], "shzjw")


class ByCodeDecisiveConfidenceTest(unittest.TestCase):
    """W3-5：_by_code 只把 confidence=parsed 的 decisive 当作本地确定结果。"""

    class _Store:
        def __init__(self, rows):
            self.rows = rows

        def find_by_code(self, key):
            return list(self.rows)

        def get_ann(self, ann_id):
            return {"id": ann_id, "doc_name": "某标准", "published_on": "2022-01-01",
                    "title": "关于发布《某标准》的公告", "url": "https://www.mohurd.gov.cn/x.html"}

        def catalog_entries(self):
            return []

    def test_single_ambiguous_target_is_not_code_local(self):
        rows = [{
            "id": 1, "code_key": "GB50016-2014", "code_display": "GB 50016-2014",
            "role": "target_of_revision", "confidence": "ambiguous",
            "evidence": "局部修订为GB 50016-2014", "published_on": "2022-01-01",
        }]
        cur = FakeCurrency([], complete=True)
        with self.assertRaises(NotIndexed) as ctx:
            lookup_mod._by_code(self._Store(rows), "GB 50016-2014", cur)
        self.assertEqual(ctx.exception.exit, 31)

    def test_parsed_target_is_code_local(self):
        rows = [{
            "id": 1, "code_key": "GB50016-2014", "code_display": "GB 50016-2014",
            "role": "target_of_revision", "confidence": "parsed",
            "evidence": "局部修订为GB 50016-2014", "published_on": "2022-01-01",
        }]
        result = lookup_mod._by_code(self._Store(rows), "GB 50016-2014", FakeCurrency([], complete=True))
        self.assertEqual(result["via"], "code_local")
        self.assertEqual(result["code_key"], "GB50016-2014")


if __name__ == "__main__":
    unittest.main()
