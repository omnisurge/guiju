"""fix_med_20261112：六个 Medium 项的回归测试。

  M9  pull/resolve 的默认时效缓存拿不到 store（TTL 落库层被架空）
  M4  上海地标/详情页路由写死 mohurd
  M13 lookup 与 cite 各写一份 mine+current 挑选逻辑
  M14 cite 公告无编号时，ref 本身是编号却抛 31
  M6  落库兜底编号的非 parsed 置信度被 or "parsed" 洗白、--strict 漏过
  附加 SourceUnreachable 被 except Exception 吞成 32（应为 20）
"""
import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.cli import lookup
from guiju.cli.lookup import local_records, match_currency, resolve
from guiju.cli.commands import cite, pull
from guiju.currency import Record
from guiju.errors import AmbiguousTarget, SourceUnreachable
from guiju.store.sqlite import Store

_STORES = []


def tearDownModule():
    for s in _STORES:
        s.close()


def make_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "c.db"
    store = Store(tmp)
    _STORES.append(store)
    return store


def two_version_store() -> Store:
    """两条同名公告：原发布（旧） + 局部修订（新）。"""
    store = make_store()
    store.upsert_announcement(
        "https://www.mohurd.gov.cn/a1.html", "2015-05-01",
        "关于发布国家标准《建筑设计防火规范》的公告", "建筑设计防火规范", "national",
    )
    store.upsert_announcement(
        "https://www.mohurd.gov.cn/a2.html", "2019-01-01",
        "关于发布国家标准《建筑设计防火规范》局部修订的公告", "建筑设计防火规范", "partial_revision",
    )
    return store


def _versions(has_a1, has_a2):
    return [
        {"ann_id": 1, "code": "GB 50016-2014", "code_key": "GB50016-2014",
         "published_on": "2015-05-01", "has_attachment": has_a1, "status": "current"},
        {"ann_id": 2, "code": "GB 50016-2014", "code_key": "GB50016-2014",
         "published_on": "2019-01-01", "has_attachment": has_a2, "status": "current"},
    ]


class HasAttachmentTriStateTest(unittest.TestCase):
    """M9 附带修复：has_attachment=None（没核实）≠ False（核实没有）。"""

    def test_none_not_filtered(self):
        """None 行必须保留参选：否则唯二候选被漏成 None。"""
        store = two_version_store()
        cur = mock.Mock()
        cur.lookup.return_value = [Record(code="GB 50016-2014", status="current", name="x")]
        # 让版本构造成功（缓存 mention 给出 code_key）
        store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        store.add_mention(1, "GB50016-2014", "primary", "e1")
        store.add_mention(2, "GB50016-2014", "target_of_revision", "e2")
        # has_attachment=None 由"编号来自缓存、未抓详情页"产生 → 不打 detail
        with mock.patch("guiju.sources.registry", return_value={"mohurd": mock.Mock()}):
            target = resolve(store, "建筑设计防火规范", cur)
        # 两个版本 has_attachment 均为 None（缓存路径没抓详情页）→ win 必须选出来而非 None
        self.assertIsNotNone(target)
        self.assertIn(target["ann"]["url"], ("https://www.mohurd.gov.cn/a1.html",
                                             "https://www.mohurd.gov.cn/a2.html"))

    def test_none_loses_to_true(self):
        """核实有附件(True)的公告优先于没核实(None)的。"""
        store = two_version_store()
        store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        store.add_mention(1, "GB50016-2014", "primary", "e1")
        store.add_mention(2, "GB50016-2014", "target_of_revision", "e2")
        cur = mock.Mock()
        cur.lookup.return_value = [Record(code="GB 50016-2014", status="current", name="x")]
        src = mock.Mock()
        src.detail.return_value = ("<html>a.pdf</html>", "正文")
        src.attachments.return_value = [mock.Mock()]
        with mock.patch("guiju.sources.registry", return_value={"mohurd": src}):
            target = resolve(store, "建筑设计防火规范", cur)
        # 两条都抓到了附件（has_attachment=True）→ 取 published_on 最新的 a2
        self.assertEqual(target["ann"]["url"], "https://www.mohurd.gov.cn/a2.html")


class PullCurrencyStoreTest(unittest.TestCase):
    """M9：pull 必须给 resolve 一个带 store 的 CachedCurrency。"""

    def test_pull_one_passes_store_backed_currency(self):
        store = make_store()
        args = argparse.Namespace(ref="GB 50016-2014", out=None, force=False, readable=False)
        source = mock.Mock(spec=[])  # api_mode 缺失 → 走非 API 分支
        captured = {}

        def fake_resolve(s, ref, currency=None, allow_network=True, fetch_detail=None):
            captured["currency"] = currency
            return {"ann": {"id": 1, "url": "u", "doc_name": "d", "published_on": "2020-01-01"},
                    "versions": [], "code_key": None, "code_display": None}

        with mock.patch.object(pull, "resolve", side_effect=fake_resolve), \
             mock.patch.object(pull, "find_cached", return_value=None), \
             mock.patch.object(pull, "availability"):
            try:
                pull._pull_one(store, args, "GB 50016-2014", source, Path(tempfile.mkdtemp()))
            except Exception:
                pass  # 我们只关心 resolve 入参，后续下载路径允许失败
        from guiju.currency import CachedCurrency
        self.assertIsInstance(captured.get("currency"), CachedCurrency)
        self.assertIs(captured["currency"].store, store)


class ResolveDefaultCurrencyStoreTest(unittest.TestCase):
    """M9：resolve 不传 currency 时，默认 CachedCurrency 也要带 store。"""

    def test_default_cached_currency_has_store(self):
        store = make_store()
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/a1.html", "2015-05-01",
            "关于发布国家标准《建筑设计防火规范》的公告", "建筑设计防火规范", "national",
        )
        seen = {}
        real_cc = lookup.currency_mod.CachedCurrency

        def spy(inner=None, store=None):
            seen["store"] = store
            return mock.Mock(lookup=lambda code: [], name="spy")

        with mock.patch.object(lookup.currency_mod, "CachedCurrency", side_effect=spy):
            try:
                resolve(store, "建筑设计防火规范")
            except Exception:
                pass
        self.assertIs(seen.get("store"), store)


class LocalRecordsRegistryDrivenTest(unittest.TestCase):
    """M4：local_records 按 api_mode+find 挑源，不写死源名。"""

    def test_queries_api_mode_sources_only(self):
        shzjw = mock.Mock()
        shzjw.api_mode = True
        shzjw.id = "shzjw"
        shzjw.find.return_value = [
            {"code": "DG/TJ08-2007-2026", "status": "current", "name": "x", "implement_on": "2026-01-01"}
        ]
        other = mock.Mock()
        other.api_mode = False
        other.id = "mohurd"
        with mock.patch("guiju.sources.registry", return_value={"mohurd": other, "shzjw": shzjw}):
            recs = local_records("DG/TJ 08-2007-2026")
        shzjw.find.assert_called_once()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].code, "DG/TJ08-2007-2026")
        self.assertEqual(recs[0].status, "current")
        self.assertEqual(recs[0].source, "shzjw")

    def test_new_api_mode_source_auto_included(self):
        """将来新增第二个 api_mode 源，不改 local_records 即被询问。"""
        s2 = mock.Mock()
        s2.api_mode = True
        s2.id = "newsrc"
        s2.find.return_value = [{"code": "XX-1", "status": "current"}]
        with mock.patch("guiju.sources.registry", return_value={"newsrc": s2}):
            recs = local_records("XX-1")
        self.assertEqual(len(recs), 1)


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
        # 同族（GB50016-*）现行优先于列表里第一个 current
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
        picked = lookup._pick_record(recs, "GB50016-2014")
        self.assertEqual(picked.code, "GB 50016-2014")


class CiteRefAsCodeFallbackTest(unittest.TestCase):
    """M14：公告正文没有编号，但 ref 本身就是编号 → 出 unknown 引用而非 31。"""

    def _store_no_code(self):
        store = make_store()
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/nocode.html", "2020-01-01",
            "某工作计划公告", "某工作计划", "national",
        )
        return store

    def _target(self, store):
        ann = store.db.execute("SELECT * FROM announcement").fetchone()
        return {"ann": dict(ann), "code_key": None, "code_display": None,
                "confidence": None, "versions": []}

    def test_ref_is_code_gets_citation(self):
        store = self._store_no_code()
        args = argparse.Namespace(ref="GB 50016-2014", clause=None, strict=False,
                                  offline=True, format="json")
        src = mock.Mock()
        src.detail.return_value = ("", "无编号正文")
        with mock.patch.object(cite, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite, "_check_currency", return_value=("unknown", "", [])), \
             mock.patch("guiju.sources.registry", return_value={"mohurd": src}):
            rc = cite.run(store, args)
        self.assertEqual(rc, 0)

    def test_ref_not_code_still_31(self):
        store = self._store_no_code()
        args = argparse.Namespace(ref="不存在的规范名", clause=None, strict=False,
                                  offline=True, format="json")
        from guiju.errors import NotIndexed
        with mock.patch.object(cite, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite, "_check_currency", return_value=("unknown", "", [])), \
             self.assertRaises(NotIndexed):
            cite.run(store, args)


class CiteDbFallbackConfidenceTest(unittest.TestCase):
    """M6：落库兜底编号 confidence 非 parsed → 进 warnings、--strict 硬失败。"""

    def _store_mention(self, confidence):
        store = make_store()
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/nocode.html", "2020-01-01",
            "某公告", "某公告", "national",
        )
        store.upsert_edition("GB50016-2014", "GB 50016-2014", None, confidence)
        store.add_mention(1, "GB50016-2014", "primary", "e1")
        return store

    def _target(self, store):
        ann = store.db.execute("SELECT * FROM announcement").fetchone()
        return {"ann": dict(ann), "code_key": None, "code_display": None,
                "confidence": None, "versions": []}

    def test_ambiguous_fallback_warns_and_strict_fails(self):
        store = self._store_mention("ambiguous")
        args = argparse.Namespace(ref="某公告", clause=None, strict=False,
                                  offline=True, format="json")
        emitted = {}
        with mock.patch.object(cite, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite, "_check_currency", return_value=("unknown", "", [])), \
             mock.patch.object(cite.render, "emit", side_effect=lambda p: emitted.update(p)):
            cite.run(store, args)
        self.assertTrue(emitted["warnings"], "非 parsed 兜底必须产生 warning")
        self.assertEqual(emitted["confidence"], "ambiguous")

        args.strict = True
        args.offline = False  # strict+offline 本身是 BadUsage，先解除
        from guiju.errors import AmbiguousCode
        with mock.patch.object(cite, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite, "_check_currency", return_value=("unknown", "", [])), \
             mock.patch.object(cite.render, "emit"), \
             self.assertRaises(AmbiguousCode):
            cite.run(store, args)

    def test_parsed_fallback_no_extra_warning(self):
        store = self._store_mention("parsed")
        args = argparse.Namespace(ref="某公告", clause=None, strict=False,
                                  offline=True, format="json")
        emitted = {}
        with mock.patch.object(cite, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite, "_check_currency", return_value=("unknown", "", [])), \
             mock.patch.object(cite.render, "emit", side_effect=lambda p: emitted.update(p)):
            cite.run(store, args)
        self.assertEqual(emitted["confidence"], "parsed")
        self.assertEqual(emitted["warnings"], [])


class SourceUnreachableReraiseTest(unittest.TestCase):
    """详情页抓取全部死于 SourceUnreachable → 20 上抛，不得伪装成 32。"""

    def test_pick_version_all_unreachable(self):
        store = two_version_store()
        cur = mock.Mock()
        cur.lookup.return_value = []
        src = mock.Mock()
        src.detail.side_effect = SourceUnreachable("down")
        src.attachments.return_value = []
        with mock.patch("guiju.sources.registry", return_value={"mohurd": src}), \
             self.assertRaises(SourceUnreachable):
            resolve(store, "建筑设计防火规范", cur)

    def test_partial_failure_still_tolerated(self):
        """部分失败部分成功 → 不构成 20，走正常路径。"""
        store = two_version_store()
        store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        store.add_mention(1, "GB50016-2014", "primary", "e1")
        store.add_mention(2, "GB50016-2014", "target_of_revision", "e2")
        cur = mock.Mock()
        cur.lookup.return_value = [Record(code="GB 50016-2014", status="current", name="x")]
        src = mock.Mock()
        # 第一次抓详情页抛 SourceUnreachable，第二次成功
        results = [SourceUnreachable("down"), ("<html/>", "正文")]
        def detail(url):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        src.detail.side_effect = detail
        src.attachments.return_value = []
        with mock.patch("guiju.sources.registry", return_value={"mohurd": src}):
            target = resolve(store, "建筑设计防火规范", cur)
        self.assertIsNotNone(target)


if __name__ == "__main__":
    unittest.main()
