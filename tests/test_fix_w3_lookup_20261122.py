"""1.2.0a4 W3-4/W3-5/W3-6/W3-8 回归：假 parsed、决定性 confidence、地标回落、主机精确匹配。

这些用例按修复前缺陷编写：在 f98e0d2（1.2.0a3）上应当失败。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from guiju import currency as currency_mod
from guiju import localstatus
from guiju.application import resolve as lookup_mod
from guiju.cli.commands import which as which_mod
from guiju.currency import Record
from guiju.currency.model import QueryCancelled
from guiju.errors import (
    AmbiguousTarget,
    ComplianceDenied,
    NeedsHuman,
    NoCurrencyRecord,
    NotIndexed,
    SourceUnreachable,
)
from guiju.sources import shzjw as shzjw_source
from guiju.store.sqlite import Store

NAME = "某标准"


class FakeCurrency:
    name = "fake"

    def __init__(self, records=None, *, complete=True, empty_authoritative=False, reason="",
                 error=None, insecure_transport=False, truncated=False, has_more=False):
        self.records = list(records or [])
        self.complete = complete
        self.empty_authoritative = empty_authoritative
        self.incomplete_reason = reason
        self.error = error
        self.insecure_transport = insecure_transport
        self.truncated = truncated
        self.has_more = has_more

    def lookup(self, code):
        if self.error is not None:
            raise self.error
        return list(self.records)


def _record(code, status="current", name=NAME):
    return Record(code=code, status=status, name=name, source="fake")


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)

    def _ann(self, suffix: str, published: str) -> int:
        ann_id, _ = self.store.upsert_announcement(
            f"https://www.mohurd.gov.cn/{suffix}.html", published,
            f"关于发布《{NAME}》的公告", NAME, "national",
        )
        return ann_id

    def _mention(self, ann_id: int, key: str, role: str, confidence: str) -> None:
        self.store.upsert_edition(key, key.replace("-", " "), None, "ambiguous")
        self.store.add_mention(ann_id, key, role, f"编号为{key}", confidence=confidence)


class PickCurrentVersionConfidenceTest(_StoreCase):
    """W3-4：cached/详情解析只有 parsed 才能定版；不完整时效结果一律交 32。"""

    def test_cached_ambiguous_versions_are_not_used(self):
        first = self._ann("a1", "2022-01-01")
        second = self._ann("a2", "2015-01-01")
        self._mention(first, "GB50016-2014", "target_of_revision", "ambiguous")
        self._mention(second, "GB50016-2019", "target_of_revision", "ambiguous")
        self.store.commit()
        cur = FakeCurrency([
            _record("GB50016-2014"), _record("GB50016-2019"),
        ])
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def test_sticky_parsed_edition_does_not_mask_ambiguous_mention(self):
        # edition.confidence 是跨公告的全局值且 parsed 不会被降级；按公告的
        # mention.confidence=ambiguous 才是这次公告真正的解析结果。
        first = self._ann("a1", "2022-01-01")
        second = self._ann("a2", "2015-01-01")
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(first, "GB50016-2014", "target_of_revision",
                               "编号为GB 50016-2014", confidence="ambiguous")
        self._mention(second, "GB50016-2019", "target_of_revision", "ambiguous")
        self.store.commit()
        cur = FakeCurrency([_record("GB50016-2014"), _record("GB50016-2019")])
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def test_ambiguous_rescan_does_not_use_old_cached_key(self):
        first = self._ann("a1", "2022-01-01")
        self._ann("a2", "2015-01-01")
        self._mention(first, "GB/T50362-2021", "target_of_revision", "ambiguous")
        self.store.commit()

        source = mock.Mock()
        source.attachments.return_value = []

        def detail(url):
            if "a1" in url:
                return "<html></html>", f"《{NAME}》GB/T 50362-2021与GB/T 50362-2022均已列入目录。"
            return "<html></html>", f"现批准《{NAME}》为国家标准，编号为GB/T 50362-2022，自2023年1月1日起实施。"

        source.detail.side_effect = detail
        statuses = {"GB/T50362-2021": "current", "GB/T50362-2022": "current"}
        cur = FakeCurrency([
            _record(code, status=statuses[code]) for code in statuses
        ])
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(
                self.store,
                NAME,
                currency=cur,
                allow_network=True,
                sources={"mohurd": source},
            )

    def test_incomplete_currency_cannot_pick_current_version(self):
        first = self._ann("a1", "2022-01-01")
        second = self._ann("a2", "2015-01-01")
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.upsert_edition("GB50016-2019", "GB 50016-2019", None, "parsed")
        self.store.add_mention(first, "GB50016-2014", "target_of_revision", "编号为GB 50016-2014", confidence="parsed")
        self.store.add_mention(second, "GB50016-2019", "target_of_revision", "编号为GB 50016-2019", confidence="parsed")
        self.store.commit()
        cur = FakeCurrency([
            _record("GB50016-2014"), _record("GB50016-2019"),
        ], complete=False, reason="无总页数证据")
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def _two_parsed_versions(self):
        first = self._ann("a1", "2022-01-01")
        second = self._ann("a2", "2015-01-01")
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.upsert_edition("GB50016-2019", "GB 50016-2019", None, "parsed")
        self.store.add_mention(first, "GB50016-2019", "target_of_revision", "编号为GB 50016-2019", confidence="parsed")
        self.store.add_mention(second, "GB50016-2014", "target_of_revision", "编号为GB 50016-2014", confidence="parsed")
        self.store.commit()

    def test_plaintext_but_complete_currency_can_pick_current_version(self):
        # csres HTTPS 被阻断时只能走明文；只要返回记录、未截断且 has_more=False，
        # 同名定版仍应可用（不写缓存；_by_code 的 non-empty 明文仍 50 另测）。
        self._two_parsed_versions()
        cur = FakeCurrency(
            [_record("GB50016-2014"), _record("GB50016-2019")],
            complete=False, insecure_transport=True, truncated=False, has_more=False,
            reason="明文回退",
        )
        result = lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)
        self.assertEqual(result["via"], "name_current_version")
        self.assertEqual(result["code_key"], "GB50016-2019")

    def test_plaintext_truncated_currency_still_ambiguous(self):
        self._two_parsed_versions()
        cur = FakeCurrency(
            [_record("GB50016-2014"), _record("GB50016-2019")],
            complete=False, insecure_transport=True, truncated=True, has_more=False,
            reason="翻页截断",
        )
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def test_plaintext_unknown_has_more_still_ambiguous(self):
        self._two_parsed_versions()
        cur = FakeCurrency(
            [_record("GB50016-2014"), _record("GB50016-2019")],
            complete=False, insecure_transport=True, truncated=False, has_more=None,
            reason="缺总页数",
        )
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def test_currency_without_insecure_attribute_does_not_crash(self):
        self._two_parsed_versions()
        class Minimal(FakeCurrency):
            def __init__(self):
                super().__init__([_record("GB50016-2014"), _record("GB50016-2019")], complete=False)
                del self.insecure_transport
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=Minimal(), allow_network=False)


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


class EmptyAuthoritativeByCodeTest(unittest.TestCase):
    """W3-2：complete=False 但源明确空（empty_authoritative）→ 31，不是 50。"""

    class _Store:
        def find_by_code(self, key):
            return []

        def catalog_entries(self):
            return []

    def test_plaintext_zero_hit_is_not_indexed_31(self):
        cur = FakeCurrency([], complete=False, empty_authoritative=True, reason="明文回退")
        with self.assertRaises(NotIndexed) as ctx:
            lookup_mod._by_code(self._Store(), "GB 99999-2099", cur)
        self.assertEqual(ctx.exception.exit, 31)

    def test_plaintext_nonempty_without_target_is_still_needs_human_50(self):
        cur = FakeCurrency([_record("GB 50011-2010")], complete=False, reason="明文回退")
        with self.assertRaises(NeedsHuman) as ctx:
            lookup_mod._by_code(self._Store(), "GB 99999-2099", cur)
        self.assertEqual(ctx.exception.exit, 50)


class WhichLocalFallbackTest(unittest.TestCase):
    """W3-6：地标代码的 20/34 要降级到上海源，其他契约错误照常传播。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)

    def _run(self, ref, *, currency_lookup, local_records=None, resolve_error=None):
        emitted = {}
        notes = []
        patches = [
            mock.patch.dict(os.environ, {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch.object(currency_mod.CachedCurrency, "lookup", side_effect=currency_lookup),
            mock.patch.object(which_mod.render, "emit", side_effect=lambda p: emitted.update(p)),
            mock.patch.object(which_mod.render, "note", side_effect=notes.append),
            mock.patch.object(which_mod.render, "out", lambda *a, **k: None),
        ]
        if local_records is not None:
            patches.append(mock.patch("guiju.localstatus.records", return_value=local_records))
        if resolve_error is not None:
            patches.append(mock.patch.object(which_mod, "resolve", side_effect=resolve_error))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return which_mod.run(self.store, Namespace(ref=ref, offline=False)), emitted, notes

    def test_initial_unreachable_falls_back_to_local(self):
        rc, emitted, _ = self._run(
            "DG/TJ08-2007",
            currency_lookup=SourceUnreachable("currency down"),
            local_records=[Record("DG/TJ08-2007-2026", "current", "地质灾害", source="shzjw")],
            resolve_error=SourceUnreachable("currency down"),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["currency_source"], "shzjw")
        self.assertEqual(emitted["currency"][0]["code"], "DG/TJ08-2007-2026")

    def test_initial_no_currency_record_falls_back_to_local(self):
        rc, emitted, _ = self._run(
            "DG/TJ08-2007",
            currency_lookup=NoCurrencyRecord("not covered"),
            local_records=[Record("DG/TJ08-2007-2026", "current", "地质灾害", source="shzjw")],
            resolve_error=NoCurrencyRecord("not covered"),
        )
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["currency_source"], "shzjw")

    def test_non_local_unreachable_still_raises(self):
        with (
            mock.patch.dict(os.environ, {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch.object(currency_mod.CachedCurrency, "lookup",
                              side_effect=SourceUnreachable("down")),
            self.assertRaises(SourceUnreachable),
        ):
            which_mod.run(self.store, Namespace(ref="GB 50011-2020", offline=False))

    def test_contract_errors_are_not_swallowed(self):
        for error in (NeedsHuman("bad"), ComplianceDenied("blocked"), QueryCancelled("cancelled")):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                self._run(
                    "DG/TJ08-2007",
                    currency_lookup=lambda code: [],
                    local_records=[],
                    resolve_error=error,
                )

    def test_resolve_20_and_34_degrade_to_local(self):
        for error in (SourceUnreachable("down"), NoCurrencyRecord("not covered")):
            with self.subTest(error=type(error).__name__):
                rc, emitted, _ = self._run(
                    "DG/TJ08-2007",
                    currency_lookup=lambda code: [],
                    local_records=[Record("DG/TJ08-2007-2026", "current", "地质灾害", source="shzjw")],
                    resolve_error=error,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(emitted["currency"][0]["status"], "current")

    def test_is_local_code_matches_shanghai_only(self):
        for ref in ("DG/TJ08-2007", "DG/TJ 08-2007-2026", "DBJT08-144", "DB31/T 1010", "DBJ08-214-2021"):
            self.assertTrue(localstatus.is_local_code(ref), ref)
        for ref in ("DBJ50-023-2020", "DGJ11-01-2020", "DB31/373", "DB32/T 1-2020", "DB11/T 1-2020", "GB 55037-2022"):
            self.assertFalse(localstatus.is_local_code(ref), ref)

    def test_source_find_accepts_prefer_exact(self):
        with mock.patch.object(shzjw_source.core, "find", return_value=[]) as core_find:
            shzjw_source.find("DG/TJ08-2007", prefer_exact=False)
        core_find.assert_called_once_with(
            "DG/TJ08-2007", searcher=shzjw_source.search, prefer_exact=False
        )

    def test_localstatus_uses_prefer_exact_false_for_base_and_true_for_version(self):
        with mock.patch.object(localstatus.shzjw, "find", return_value=[]) as find:
            localstatus.records("DG/TJ08-2007")
        find.assert_called_once_with("DG/TJ08-2007", prefer_exact=False)
        with mock.patch.object(localstatus.shzjw, "find", return_value=[]) as find:
            localstatus.records("DG/TJ08-2007-2026")
        find.assert_called_once_with("DG/TJ08-2007-2026", prefer_exact=True)

    def test_local_records_uses_prefer_exact_flag(self):
        source = mock.Mock()
        source.api_mode = True
        source.id = "shzjw"
        source.find.return_value = []
        lookup_mod.local_records("DG/TJ08-2007", sources={"shzjw": source})
        source.find.assert_called_once_with("DG/TJ08-2007", prefer_exact=False)
        source.find.reset_mock()
        lookup_mod.local_records("DG/TJ08-2007-2026", sources={"shzjw": source})
        source.find.assert_called_once_with("DG/TJ08-2007-2026", prefer_exact=True)

    def test_base_code_keeps_current_year_version(self):
        current = shzjw_source._record({
            "standardNumber": "DG/TJ08-2007-2026", "standardProjectName": "地质灾害",
            "releaseTime": "2026-08-10", "implementationTime": "2027-02-01",
        })
        bare = shzjw_source._record({
            "standardNumber": "DG/TJ08-2007", "standardProjectName": "地质灾害",
            "abolishTime": "2020-01-01",
        })

        def fake_search(ref, *, abolished=False, standard_type=0, page_size=30):
            if abolished:
                return [bare] if standard_type == 0 else []
            return [current] if standard_type == 0 else []

        with mock.patch.object(shzjw_source, "search", side_effect=fake_search):
            hits = shzjw_source.find("DG/TJ08-2007", prefer_exact=False)
        codes = [h["code"] for h in hits]
        self.assertIn("DG/TJ08-2007-2026", codes)
        self.assertIn("DG/TJ08-2007", codes)
        self.assertEqual(hits[0]["status"], "current")


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


class WhichEmptyAuthoritativeTest(unittest.TestCase):
    """W3-2：which 的零命中不能被 plaintext 空结果抬成 50。"""

    def test_which_zero_hit_plaintext_empty_stays_calm(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.db")
        self.addCleanup(store.close)
        emitted = {}

        class EmptyCurrency:
            name = "csres"
            complete = False
            empty_authoritative = True
            incomplete_reason = "明文回退"
            from_cache = False
            stale = False
            upstream_error = None
            cache_age_days = None

            def __init__(self, inner=None, store=None):
                pass

            def lookup(self, code):
                return []

        with (
            mock.patch.dict(os.environ, {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch.object(currency_mod, "CachedCurrency", EmptyCurrency),
            mock.patch.object(which_mod.render, "emit", side_effect=lambda p: emitted.update(p)),
            mock.patch.object(which_mod.render, "note", lambda *a, **k: None),
            mock.patch.object(which_mod.render, "out", lambda *a, **k: None),
        ):
            rc = which_mod.run(store, Namespace(ref="GB 99999-2099", offline=False))
        self.assertEqual(rc, 0)
        self.assertFalse(emitted["resolved"])
        self.assertTrue(emitted["currency_empty_authoritative"])
        self.assertFalse(emitted["currency_complete"])
        self.assertTrue(emitted["hint"], "未定位的原因要留给 agent（31 不是故障）")
