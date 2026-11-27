"""解析契约与回归：契约错误上抛、显式依赖注入、不完整结果定版限制、权威空与多源驱动。"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.application import cite as cite_app
from guiju.application import pull as pull_app
from guiju.application import resolve as lookup_mod
from guiju.application.resolve import local_records, resolve
from guiju.cli.commands import cite as cite_cli
from guiju.currency import CachedCurrency, Record
from guiju.currency.model import QueryCancelled
from guiju.errors import AmbiguousTarget, ComplianceDenied, NeedsHuman, NotIndexed, SourceUnreachable
from guiju.store.sqlite import Store

NAME = "某标准"
_CONTRACT_ERRORS = (NeedsHuman, ComplianceDenied, QueryCancelled)


class _ContractCurrency:
    name = "fake"

    def __init__(self, records=(), *, complete=True, empty_authoritative=False, reason="",
                 error=None, insecure_transport=False, truncated=False, has_more=False):
        self.records = list(records)
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


def _sources(error):
    source = mock.Mock()
    source.detail.side_effect = error
    source.attachments.return_value = []
    return {"mohurd": source}


class _TwoVersionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/a1.html", "2022-01-01",
            "住房和城乡建设部关于发布国家标准《建筑防火通用规范》的公告", "建筑防火通用规范", "national",
        )
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/a2.html", "2015-05-01",
            "关于发布国家标准《建筑设计防火规范》的公告", "建筑设计防火规范", "national",
        )
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/a3.html", "2019-01-01",
            "关于发布国家标准《建筑设计防火规范》局部修订的公告", "建筑设计防火规范", "partial_revision",
        )


class PickVersionDetailFetchTest(_TwoVersionCase):
    """_pick_current_version 的详情页抓取：契约错误不得被 continue 吞成 32。"""

    def test_contract_errors_propagate(self):
        for err_cls in _CONTRACT_ERRORS:
            with self.subTest(error=err_cls.__name__), self.assertRaises(err_cls):
                resolve(
                    self.store,
                    "建筑设计防火规范",
                    _ContractCurrency(),
                    sources=_sources(err_cls("x")),
                )

    def test_generic_error_still_drops_candidates(self):
        with self.assertRaises(AmbiguousTarget):
            resolve(
                self.store,
                "建筑设计防火规范",
                _ContractCurrency(),
                sources=_sources(ValueError("boom")),
            )


class PickVersionCurrencyLoopTest(_TwoVersionCase):
    """_pick_current_version 的时效查询：契约错误不得被 return None 吞成 32。"""

    def setUp(self):
        super().setUp()
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(2, "GB50016-2014", "primary", "编号为GB 50016-2014")
        self.store.add_mention(3, "GB50016-2014", "target_of_revision", "局部修订为GB 50016-2014")

    def test_contract_errors_propagate(self):
        for err_cls in _CONTRACT_ERRORS:
            with self.subTest(error=err_cls.__name__):
                cur = _ContractCurrency(error=err_cls("x"))
                with self.assertRaises(err_cls):
                    resolve(self.store, "建筑设计防火规范", cur)

    def test_generic_error_still_ambiguous(self):
        cur = _ContractCurrency(error=ValueError("boom"))
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "建筑设计防火规范", cur)


class VerifyByPrimaryCodeTest(_TwoVersionCase):
    """_verify_by_primary_code 的 fetch_detail：契约错误不得被 continue 吞掉。"""

    def setUp(self):
        super().setUp()
        self.cur = _ContractCurrency(
            [Record(code="GB 50016-2014", status="current", name="建筑设计防火规范")]
        )

    def test_contract_errors_propagate(self):
        for err_cls in _CONTRACT_ERRORS:
            with self.subTest(error=err_cls.__name__):
                def fake_detail(url, _cls=err_cls):
                    raise _cls("x")

                with self.assertRaises(err_cls):
                    resolve(self.store, "GB 50016-2014", self.cur, fetch_detail=fake_detail)

    def test_generic_error_still_ambiguous(self):
        def fake_detail(url):
            raise ValueError("boom")

        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "GB 50016-2014", self.cur, fetch_detail=fake_detail)


class _EmptyStoreDouble:
    def find_by_code(self, key):
        return []

    def catalog_entries(self):
        return []


class ByCodeCompletenessTest(unittest.TestCase):
    """B3-W3-8：_by_code 部分空结果不能伪装成 NotIndexed(31)。"""

    def _run(self, currency):
        return lookup_mod._by_code(_EmptyStoreDouble(), "GB 9999-2020", currency)

    def test_incomplete_empty_is_needs_human_50(self):
        with self.assertRaises(NeedsHuman) as ctx:
            self._run(_ContractCurrency([], complete=False, reason="翻页截断"))
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("不完整", str(ctx.exception))
        self.assertEqual(ctx.exception.extra["currency_complete"], False)
        self.assertEqual(ctx.exception.extra["currency_incomplete_reason"], "翻页截断")

    def test_incomplete_nonempty_without_target_is_needs_human_50(self):
        records = [Record(code="GB 9999-2020", status="current", name="抗震规范")]
        with self.assertRaises(NeedsHuman) as ctx:
            self._run(_ContractCurrency(records, complete=False, reason="无总页数"))
        self.assertEqual(ctx.exception.exit, 50)
        self.assertEqual(ctx.exception.extra["resolved_name"], "抗震规范")

    def test_incomplete_mismatched_edition_is_needs_human_50_without_wrong_name(self):
        records = [Record(code="GB 9999-2010", status="current", name="旧版抗震规范")]
        with self.assertRaises(NeedsHuman) as ctx:
            self._run(_ContractCurrency(records, complete=False, reason="无总页数"))
        self.assertEqual(ctx.exception.exit, 50)
        self.assertNotIn("resolved_name", ctx.exception.extra)

    def test_complete_empty_remains_not_indexed_31(self):
        with self.assertRaises(NotIndexed) as ctx:
            self._run(_ContractCurrency([], complete=True))
        self.assertEqual(ctx.exception.exit, 31)

    def test_unknown_completeness_remains_not_indexed_31(self):
        with self.assertRaises(NotIndexed) as ctx:
            self._run(_ContractCurrency([], complete=None))
        self.assertEqual(ctx.exception.exit, 31)


class PickCurrentVersionConfidenceTest(unittest.TestCase):
    """W3-4：cached/详情解析只有 parsed 才能定版；不完整时效结果一律交 32。"""

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

    def _two_parsed_versions(self):
        first = self._ann("a1", "2022-01-01")
        second = self._ann("a2", "2015-01-01")
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.upsert_edition("GB50016-2019", "GB 50016-2019", None, "parsed")
        self.store.add_mention(first, "GB50016-2019", "target_of_revision", "编号为GB 50016-2019", confidence="parsed")
        self.store.add_mention(second, "GB50016-2014", "target_of_revision", "编号为GB 50016-2014", confidence="parsed")
        self.store.commit()

    def test_cached_ambiguous_versions_are_not_used(self):
        first = self._ann("a1", "2022-01-01")
        second = self._ann("a2", "2015-01-01")
        self._mention(first, "GB50016-2014", "target_of_revision", "ambiguous")
        self._mention(second, "GB50016-2019", "target_of_revision", "ambiguous")
        self.store.commit()
        cur = _ContractCurrency([
            _record("GB50016-2014"), _record("GB50016-2019"),
        ])
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def test_sticky_parsed_edition_does_not_mask_ambiguous_mention(self):
        first = self._ann("a1", "2022-01-01")
        second = self._ann("a2", "2015-01-01")
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(first, "GB50016-2014", "target_of_revision",
                               "编号为GB 50016-2014", confidence="ambiguous")
        self._mention(second, "GB50016-2019", "target_of_revision", "ambiguous")
        self.store.commit()
        cur = _ContractCurrency([_record("GB50016-2014"), _record("GB50016-2019")])
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
        cur = _ContractCurrency([
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
        cur = _ContractCurrency([
            _record("GB50016-2014"), _record("GB50016-2019"),
        ], complete=False, reason="无总页数证据")
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def test_plaintext_but_complete_currency_can_pick_current_version(self):
        self._two_parsed_versions()
        cur = _ContractCurrency(
            [_record("GB50016-2014"), _record("GB50016-2019")],
            complete=False, insecure_transport=True, truncated=False, has_more=False,
            reason="明文回退",
        )
        result = lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)
        self.assertEqual(result["via"], "name_current_version")
        self.assertEqual(result["code_key"], "GB50016-2019")

    def test_plaintext_truncated_currency_still_ambiguous(self):
        self._two_parsed_versions()
        cur = _ContractCurrency(
            [_record("GB50016-2014"), _record("GB50016-2019")],
            complete=False, insecure_transport=True, truncated=True, has_more=False,
            reason="翻页截断",
        )
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def test_plaintext_unknown_has_more_still_ambiguous(self):
        self._two_parsed_versions()
        cur = _ContractCurrency(
            [_record("GB50016-2014"), _record("GB50016-2019")],
            complete=False, insecure_transport=True, truncated=False, has_more=None,
            reason="缺总页数",
        )
        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=cur, allow_network=False)

    def test_currency_without_insecure_attribute_does_not_crash(self):
        self._two_parsed_versions()

        class Minimal(_ContractCurrency):
            def __init__(self):
                super().__init__([_record("GB50016-2014"), _record("GB50016-2019")], complete=False)
                del self.insecure_transport

        with self.assertRaises(AmbiguousTarget):
            lookup_mod.resolve(self.store, NAME, currency=Minimal(), allow_network=False)


class EmptyAuthoritativeByCodeTest(unittest.TestCase):
    """W3-2：complete=False 但源明确空（empty_authoritative）→ 31，不是 50。"""

    def test_plaintext_zero_hit_is_not_indexed_31(self):
        cur = _ContractCurrency([], complete=False, empty_authoritative=True, reason="明文回退")
        with self.assertRaises(NotIndexed) as ctx:
            lookup_mod._by_code(_EmptyStoreDouble(), "GB 99999-2099", cur)
        self.assertEqual(ctx.exception.exit, 31)

    def test_plaintext_nonempty_without_target_is_still_needs_human_50(self):
        cur = _ContractCurrency([_record("GB 50011-2010")], complete=False, reason="明文回退")
        with self.assertRaises(NeedsHuman) as ctx:
            lookup_mod._by_code(_EmptyStoreDouble(), "GB 99999-2099", cur)
        self.assertEqual(ctx.exception.exit, 50)


class ApplicationExplicitInjectionContractTest(unittest.TestCase):
    """S3c：application 不得在缺参时隐式构造 CachedCurrency/registry()/out_dir。"""

    def test_missing_explicit_dependencies_raise_value_error(self):
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
        non_api_src = mock.Mock(spec=[])
        empty_cur = mock.Mock()
        empty_cur.lookup.return_value = []

        # 1. resolve 按编号缺 currency
        with self.assertRaises(ValueError):
            resolve(store, "GB 50016-2014")
        # 2. resolve 同名多版缺 currency
        with self.assertRaises(ValueError):
            resolve(store, "建筑设计防火规范")
        # 3. resolve 同名多版需抓详情页但缺 sources
        with self.assertRaises(ValueError):
            resolve(store, "建筑设计防火规范", currency=empty_cur, allow_network=True)
        # 4. local_records 缺 sources
        with self.assertRaises(ValueError):
            local_records("DG/TJ08-2007")
        # 5. cite_app.check_currency 在线缺 currency
        with self.assertRaises(ValueError):
            cite_app.check_currency("GB 1-2020", offline=False)
        # 6. cite_app.run 在线缺 AppContext.currency
        with self.assertRaises(ValueError):
            cite_app.run(
                store,
                argparse.Namespace(ref="GB 1-2020", clause=None, strict=False, offline=False),
            )
        # 7. pull_app.run 缺 sources
        with self.assertRaises(ValueError):
            pull_app.run(
                store,
                argparse.Namespace(
                    ref="GB 1-2020", source="mohurd", force=False, readable=False, out=tmp.name
                ),
            )
        # 8. pull_app._pull_one 缺 AppContext.currency
        with self.assertRaises(ValueError):
            pull_app._pull_one(
                store,
                argparse.Namespace(force=False, readable=False, out=tmp.name),
                "GB 1-2020",
                non_api_src,
                Path(tmp.name),
            )
        # 9. pull_app._out_dir 缺 out_dir 与 out
        with self.assertRaises(ValueError):
            pull_app._out_dir(argparse.Namespace(out=None))

    def test_cite_cli_entry_injects_store_backed_currency_and_sources(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.db")
        self.addCleanup(store.close)
        args = argparse.Namespace(ref="GB 50016-2014", clause=None, strict=False, offline=False)
        with mock.patch.object(cite_app, "run", return_value=0) as app_run:
            rc = cite_cli.run(store, args)
        self.assertEqual(rc, 0)
        app_run.assert_called_once_with(store, args)
        ctx = args.app_context
        self.assertIsInstance(ctx.currency, CachedCurrency)
        self.assertIs(ctx.currency.store, store)
        self.assertIn("mohurd", ctx.sources)


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
        recs = local_records("DG/TJ 08-2007-2026", sources={"mohurd": other, "shzjw": shzjw})
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
        recs = local_records("XX-1", sources={"newsrc": s2})
        self.assertEqual(len(recs), 1)


class SourceUnreachableReraiseTest(unittest.TestCase):
    """详情页抓取全部死于 SourceUnreachable → 20 上抛，不得伪装成 32。"""

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

    def test_pick_version_all_unreachable(self):
        store = self._two_version_store()
        cur = mock.Mock()
        cur.lookup.return_value = []
        src = mock.Mock()
        src.detail.side_effect = SourceUnreachable("down")
        src.attachments.return_value = []
        with self.assertRaises(SourceUnreachable):
            resolve(store, "建筑设计防火规范", cur, sources={"mohurd": src})

    def test_partial_failure_still_tolerated(self):
        store = self._two_version_store()
        store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        store.add_mention(1, "GB50016-2014", "primary", "e1")
        store.add_mention(2, "GB50016-2014", "target_of_revision", "e2")
        cur = mock.Mock()
        cur.lookup.return_value = [Record(code="GB 50016-2014", status="current", name="x")]
        src = mock.Mock()
        results = [SourceUnreachable("down"), ("<html/>", "正文")]

        def detail(url):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        src.detail.side_effect = detail
        src.attachments.return_value = []
        target = resolve(store, "建筑设计防火规范", cur, sources={"mohurd": src})
        self.assertIsNotNone(target)


if __name__ == "__main__":
    unittest.main()
