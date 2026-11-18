"""B1/W6 · P1-6 / P1-7 / P2-11 + which 的单次查询。

契约（ADR 0004 D4）：
  · cite 的时效检查是类型化结果 ok/unreachable/unsupported/no_record/offline；
  · 非 strict：不可达/未覆盖/无记录都输出引用串 + warning，status=unknown；
  · strict：不可达 → 20；未覆盖/无记录 → 34；其他 warning 维持 32（不静默通过）；
  · confidence 从 primary mention 推导，name/ID/URL 路径不再恒为 unknown；
  · which 只做一次时效查询（第二次命中的 TTL 缓存会把 from_cache 谎报成 true）。
"""

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from guiju import currency as currency_mod
from guiju.cli import main, render
from guiju.cli.commands import cite
from guiju.cli.commands import which as which_mod
from guiju.currency import Record
from guiju.errors import AmbiguousCode, NoCurrencyRecord, SourceUnreachable
from guiju.store.sqlite import Store


def _make_store(confidence="parsed"):
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "c.db")
    store.upsert_announcement(
        "https://www.mohurd.gov.cn/x.html", "2022-01-01",
        "关于发布《某标准》的公告", "某标准", "national",
    )
    store.upsert_edition("GB1-2020", "GB 1-2020", None, confidence)
    store.add_mention(1, "GB1-2020", "primary", "编号为GB 1-2020")
    store.commit()
    return tmp, store


class _FakeCurrency:
    """替身：cite.run 与 _check_currency 都会构造 CachedCurrency(store=...)。"""

    name = "fake"

    def __init__(self, inner=None, store=None):
        pass

    def lookup(self, code):
        return [Record(code="GB 1-2020", status="current", name="某标准", source="fake")]


class _UnreachableCurrency(_FakeCurrency):
    def lookup(self, code):
        raise SourceUnreachable("时效服务不可达（测试替身）")


class _UnsupportedCurrency(_FakeCurrency):
    def lookup(self, code):
        raise NoCurrencyRecord("时效侧没有该编号（原生路由未覆盖，测试替身）")


class _EmptyCurrency(_FakeCurrency):
    def lookup(self, code):
        return []


class _SupersededCurrency(_FakeCurrency):
    def lookup(self, code):
        return [
            Record(code="GB 1-2020", status="superseded", name="某标准", source="fake",
                   replaces="GB 1-2010"),
            Record(code="GB 1-2022", status="current", name="某标准", source="fake"),
        ]


class _IncompleteCurrency(_FakeCurrency):
    """W3/P1-4：服务通了但没取全（翻页截断/无总页数证据/明文回退）。"""

    complete = False
    incomplete_reason = "翻页上限截断，可能缺少记录"


class _IncompleteEmptyCurrency(_IncompleteCurrency):
    def lookup(self, code):
        return []


class _CompleteCurrency(_FakeCurrency):
    complete = True


class _CiteCase(unittest.TestCase):
    confidence = "parsed"

    def setUp(self):
        self.tmp, self.store = _make_store(self.confidence)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)
        # 引用串的人类可读输出只在非 --json 时出现；测试里关掉，保持输出干净
        render.configure(as_json=True, quiet=True)
        self.addCleanup(render.configure, as_json=False, quiet=False)

    def _target(self):
        return {
            "ann": dict(self.store.get_ann(1)),
            "code_key": None,
            "code_display": None,
            "confidence": None,
            "versions": [],
        }

    def _run(self, currency_cls, *, strict=False, offline=False, clause="5.2.1"):
        args = argparse.Namespace(
            ref="某标准", clause=clause, strict=strict, offline=offline, format="json"
        )
        emitted = {}
        with (
            mock.patch.object(cite, "resolve", return_value=self._target()),
            mock.patch.object(cite.render, "emit", side_effect=lambda p: emitted.update(p)),
            mock.patch.object(currency_mod, "CachedCurrency", currency_cls),
        ):
            rc = cite.run(self.store, args)
        return rc, emitted


class CiteUnreachableTest(_CiteCase):
    def test_non_strict_outputs_cite_with_warning(self):
        rc, emitted = self._run(_UnreachableCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["cite"], "《某标准》GB 1-2020 第5.2.1条")
        self.assertEqual(emitted["status"], "unknown")
        self.assertTrue(any("时效服务不可达" in w for w in emitted["warnings"]))
        self.assertEqual(emitted["currency_check"]["state"], "unreachable")

    def test_cite_strict_fails_when_currency_unreachable(self):
        with self.assertRaises(SourceUnreachable) as cm:
            self._run(_UnreachableCurrency, strict=True)
        self.assertEqual(cm.exception.exit, 20)
        # 失败也要带上已生成好的引用串，便于人工复核后使用
        self.assertEqual(cm.exception.extra["cite"], "《某标准》GB 1-2020 第5.2.1条")
        self.assertIn("--offline", cm.exception.hint)


class CiteNoCurrencyRecordTest(_CiteCase):
    def test_cite_survives_no_currency_record_with_warning(self):
        # P1-7：34 只说明时效路由没覆盖，不该把本地已核实的引用串整条丢掉
        rc, emitted = self._run(_UnsupportedCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["cite"], "《某标准》GB 1-2020 第5.2.1条")
        self.assertEqual(emitted["confidence"], "parsed")
        self.assertEqual(emitted["status"], "unknown")
        self.assertTrue(any("未覆盖" in w for w in emitted["warnings"]))
        self.assertEqual(emitted["currency_check"]["state"], "unsupported")

    def test_strict_no_currency_record_exits_34(self):
        with self.assertRaises(NoCurrencyRecord) as cm:
            self._run(_UnsupportedCurrency, strict=True)
        self.assertEqual(cm.exception.exit, 34)
        self.assertEqual(cm.exception.extra["cite"], "《某标准》GB 1-2020 第5.2.1条")

    def test_empty_records_is_no_record_not_verified(self):
        rc, emitted = self._run(_EmptyCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["status"], "unknown")
        self.assertEqual(emitted["currency_check"]["state"], "no_record")
        self.assertTrue(any("没有该编号的记录" in w for w in emitted["warnings"]))

    def test_strict_empty_records_exits_34(self):
        with self.assertRaises(NoCurrencyRecord) as cm:
            self._run(_EmptyCurrency, strict=True)
        self.assertEqual(cm.exception.exit, 34)


class CiteOfflineTest(_CiteCase):
    def test_offline_is_its_own_state_and_adds_no_warning(self):
        rc, emitted = self._run(_UnreachableCurrency, offline=True)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["status"], "unknown")
        self.assertEqual(emitted["warnings"], [], "离线是用户显式选择，不靠 warning 兜底")
        self.assertEqual(emitted["currency_check"]["state"], "offline")


class CiteStatusWarningsTest(_CiteCase):
    def test_superseded_status_still_warns(self):
        rc, emitted = self._run(_SupersededCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["status"], "superseded")
        self.assertTrue(any("superseded" in w for w in emitted["warnings"]))
        self.assertIn("GB 1-2022", " ".join(emitted["warnings"]))

    def test_strict_other_warning_is_still_32(self):
        with self.assertRaises(AmbiguousCode) as cm:
            self._run(_SupersededCurrency, strict=True)
        self.assertEqual(cm.exception.exit, 32)
        self.assertEqual(cm.exception.extra["cite"], "《某标准》GB 1-2020 第5.2.1条")


class CiteCompletenessTest(_CiteCase):
    """W3/P1-4：时效结果不完整时，cite 必须告警且不能冒充权威否定结论。"""

    def test_incomplete_result_warns_and_is_not_verified(self):
        rc, emitted = self._run(_IncompleteCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["status"], "current")
        self.assertEqual(emitted["currency_check"]["complete"], False)
        self.assertFalse(emitted["currency_check"]["verified"])
        self.assertIn("翻页上限截断", emitted["currency_check"]["incomplete_reason"])
        self.assertTrue(any("时效结果不完整" in w for w in emitted["warnings"]))

    def test_strict_incomplete_result_is_32_not_34(self):
        # 不完整 ≠ 路由未覆盖：严格模式按"未核实"处理（32），不是 34
        with self.assertRaises(AmbiguousCode) as cm:
            self._run(_IncompleteCurrency, strict=True)
        self.assertEqual(cm.exception.exit, 32)
        self.assertEqual(cm.exception.extra["cite"], "《某标准》GB 1-2020 第5.2.1条")

    def test_incomplete_empty_result_is_not_authoritative_no_record(self):
        # P1-4 的核心：不完整的空结果不能当"服务说没有"（那会变成 34 + 假阴性）
        rc, emitted = self._run(_IncompleteEmptyCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["status"], "unknown")
        self.assertEqual(emitted["currency_check"]["state"], "ok")
        self.assertFalse(emitted["currency_check"]["verified"])
        self.assertTrue(any("时效结果不完整" in w for w in emitted["warnings"]))
        self.assertFalse(any("没有该编号的记录" in w for w in emitted["warnings"]))
        with self.assertRaises(AmbiguousCode) as cm:
            self._run(_IncompleteEmptyCurrency, strict=True)
        self.assertEqual(cm.exception.exit, 32)

    def test_complete_result_is_verified_without_completeness_warning(self):
        rc, emitted = self._run(_CompleteCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["currency_check"]["complete"], True)
        self.assertTrue(emitted["currency_check"]["verified"])
        self.assertEqual(emitted["warnings"], [])

    def test_old_adapter_without_contract_is_treated_as_complete(self):
        # complete=None（老适配器没有完整性契约）：按 W3 的约定视作完整，不额外告警
        rc, emitted = self._run(_FakeCurrency)
        self.assertEqual(rc, 0)
        self.assertNotIn("complete", emitted["currency_check"])
        self.assertTrue(emitted["currency_check"]["verified"])
        self.assertEqual(emitted["warnings"], [])


class CiteConfidenceTest(_CiteCase):
    def test_confidence_follows_primary_mention(self):
        # P2-11：名称路径 target.confidence 仍是 unknown，但编号来自 parsed primary
        rc, emitted = self._run(_FakeCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["confidence"], "parsed")
        self.assertEqual(emitted["warnings"], [])

    def test_ambiguous_mention_stays_ambiguous(self):
        # edition.confidence 一旦 parsed 不会被降级（upsert_edition 的既有约定），
        # 所以这里另建一个"正文解析就是 ambiguous"的库。
        tmp, store = _make_store("ambiguous")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        self.store = store
        rc, emitted = self._run(_FakeCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["confidence"], "ambiguous")
        self.assertTrue(emitted["warnings"])


class WhichSingleLookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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


if __name__ == "__main__":
    unittest.main()
