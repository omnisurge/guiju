"""which 命令：单次时效查询、地标回落、空结果权威判定与过期/异常告警测试。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from guiju import currency as currency_mod
from guiju import localstatus
from guiju import report as render
from guiju.application import resolve as lookup_mod
from guiju.cli import main
from guiju.cli.commands import which as which_mod
from guiju.currency import Record
from guiju.currency.model import QueryCancelled
from guiju.errors import ComplianceDenied, NeedsHuman, NoCurrencyRecord, SourceUnreachable
from guiju.sources import shzjw as shzjw_source
from guiju.store.sqlite import Store


class WhichSingleLookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(render.configure, as_json=False, quiet=False)
        self.addCleanup(render.set_catalog, None)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/x.html", "2022-01-01",
            "关于发布《某标准》的公告", "某标准", "national",
        )
        self.store.commit()

    def test_which_queries_currency_once(self):
        calls = []

        class FakeCurrency:
            name = "fake"
            from_cache = False
            stale = False
            upstream_error = None
            cache_age_days = None

            def __init__(self, inner=None, store=None):
                pass

            def lookup(self, code):
                calls.append(code)
                if len(calls) > 1:
                    # 第二次查询命中进程内缓存：旧实现会把 from_cache 覆写成 True
                    FakeCurrency.from_cache = True
                return [
                    Record(code="GB 12345-2020", status="current", name="某标准", source="fake")
                ]

        emitted = {}
        with (
            mock.patch.object(currency_mod, "CachedCurrency", FakeCurrency),
            mock.patch.object(which_mod.render, "emit", side_effect=lambda p: emitted.update(p)),
        ):
            rc = which_mod.run(
                self.store, argparse.Namespace(ref="GB 12345-2020", offline=False)
            )
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["GB 12345-2020"], "同一编号只许问一次时效服务")
        self.assertFalse(emitted["currency_from_cache"], "不得因第二次查询谎报服务不可达")
        self.assertTrue(emitted["resolved"])
        self.assertEqual(emitted["code"], "GB 12345-2020")

    def _which_payload(self, currency_cls):
        notes = []
        emitted = {}
        with (
            mock.patch.object(currency_mod, "CachedCurrency", currency_cls),
            mock.patch.object(which_mod.render, "emit", side_effect=lambda p: emitted.update(p)),
            mock.patch.object(which_mod.render, "note", side_effect=notes.append),
        ):
            rc = which_mod.run(
                self.store, argparse.Namespace(ref="GB 12345-2020", offline=False)
            )
        return rc, emitted, notes

    def test_which_reports_incomplete_currency(self):
        # W3/P1-4：which 也要说清"这次没取全"，不能只报 from_cache/stale
        class Incomplete:
            name = "fake"
            from_cache = False
            stale = False
            upstream_error = None
            cache_age_days = None
            complete = False
            incomplete_reason = "无总页数证据"

            def __init__(self, inner=None, store=None):
                pass

            def lookup(self, code):
                return [
                    Record(code="GB 12345-2020", status="current", name="某标准", source="fake")
                ]

        rc, emitted, notes = self._which_payload(Incomplete)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["currency_complete"], False)
        self.assertEqual(emitted["currency_incomplete_reason"], "无总页数证据")
        self.assertTrue(any("时效结果不完整" in n for n in notes), notes)

    def test_which_reports_complete_currency_without_noise(self):
        class Complete:
            name = "fake"
            from_cache = False
            stale = False
            upstream_error = None
            cache_age_days = None
            complete = True
            incomplete_reason = ""

            def __init__(self, inner=None, store=None):
                pass

            def lookup(self, code):
                return [
                    Record(code="GB 12345-2020", status="current", name="某标准", source="fake")
                ]

        rc, emitted, notes = self._which_payload(Complete)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["currency_complete"], True)
        self.assertIsNone(emitted["currency_incomplete_reason"])
        self.assertFalse(any("不完整" in n for n in notes), notes)

    def test_which_keeps_34_for_uncovered_prefix(self):
        buf = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch.object(
                currency_mod.CachedCurrency,
                "lookup",
                side_effect=NoCurrencyRecord("时效侧没有该编号"),
            ),
            redirect_stdout(buf),
            redirect_stderr(io.StringIO()),
        ):
            code = main(
                ["which", "CJ/T 123-2020", "--db", str(Path(self.tmp.name) / "w.db"), "--json"]
            )
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 34)
        self.assertEqual(payload["reason"], "no_currency_record")


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


class _FakeStaleCurrency:
    """which 要读的字段全挂身上：lookup 不进这个桩（用 --offline 路径）。"""

    name = "fake"

    def __init__(self, store=None):
        self.from_cache = False
        self.cache_age_days = None
        self.stale = None
        self.upstream_error = None

    def lookup(self, code):
        return []


class WhichStaleTest(unittest.TestCase):
    def _run_which(self, cur):
        args = argparse.Namespace(ref="GB99999-2099", offline=True)
        out, err = io.StringIO(), io.StringIO()
        render.configure(as_json=True, quiet=False)
        try:
            with mock.patch.object(
                which_mod.currency_mod, "CachedCurrency", return_value=cur
            ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                tempfile.TemporaryDirectory() as home, mock.patch.dict(
                    "os.environ", {"GUIJU_HOME": home}
                ):
                store = Store()
                try:
                    which_mod.run(store, args)
                finally:
                    store.close()
        finally:
            render.configure(as_json=False, quiet=False)
        return out.getvalue(), err.getvalue()

    def test_payload_carries_stale_and_upstream_error(self):
        cur = _FakeStaleCurrency()
        cur.stale = True
        cur.upstream_error = "dial timeout"
        out, err = self._run_which(cur)
        self.assertIn('"currency_stale": true', out)
        self.assertIn('"currency_upstream_error": "dial timeout"', out)
        self.assertIn("时效服务不可达、结论基于旧缓存", err)

    def test_fresh_cache_hit_does_not_warn_stale(self):
        # stale=False：TTL 命中不算服务不可达，不发那条告警。
        cur = _FakeStaleCurrency()
        cur.from_cache = True
        cur.cache_age_days = 0.5
        cur.stale = False
        out, err = self._run_which(cur)
        self.assertIn('"currency_stale": false', out)
        self.assertNotIn("时效服务不可达、结论基于旧缓存", err)
        # 老告警也不该出现：stale 字段存在时以它为准，不再看 from_cache
        self.assertNotIn("时效服务不可达", err)

    def test_legacy_fallback_when_stale_absent(self):
        class Legacy:
            name = "fake"

            def __init__(self):
                self.from_cache = True
                self.cache_age_days = 2.0
                self.inner = self

            def lookup(self, code):
                return []

        out, err = self._run_which(Legacy())
        # 没有 stale 字段 → payload 记 None，告警沿用老口径
        self.assertIn('"currency_stale": null', out)
        self.assertIn("时效服务不可达", err)


if __name__ == "__main__":
    unittest.main()
