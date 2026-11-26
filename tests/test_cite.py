"""引用串生成、条款规范化、时效 check 状态、置信度推导与兜底行为测试。"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from guiju import currency as currency_mod
from guiju import report as render
from guiju.application import AppContext
from guiju.application import cite as cite_app
from guiju.cli import main
from guiju.currency import NullCurrency, Record
from guiju.domain.cite import build_citation, normalize_clause
from guiju.domain.currency_check import CurrencyCheck, CurrencyState
from guiju.errors import AmbiguousCode, NoCurrencyRecord, NotIndexed, SourceUnreachable
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
    """替身：由入口通过 AppContext 注入给 cite 的时效端口替身。"""

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


class _StaleCurrency(_FakeCurrency):
    """CachedCurrency 真实磁盘缓存路径：吞掉 SourceUnreachable 后返回旧记录。"""

    stale = True
    from_cache = True


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
            # 名称/ID/URL 路径的真实先验值是 unknown，不是 None。
            "confidence": "unknown",
            "versions": [],
        }

    def _run(self, currency_cls, *, strict=False, offline=False, clause="5.2.1"):
        cur = currency_mod.NullCurrency() if offline else currency_cls()
        args = argparse.Namespace(
            ref="某标准",
            clause=clause,
            strict=strict,
            offline=offline,
            format="json",
            app_context=AppContext(
                store=self.store, currency=cur, sources={}, offline=offline
            ),
        )
        emitted = {}
        with (
            mock.patch.object(cite_app, "resolve", return_value=self._target()),
            mock.patch.object(cite_app.render, "emit", side_effect=lambda p: emitted.update(p)),
        ):
            rc = cite_app.run(self.store, args)
        return rc, emitted


class ClauseTest(unittest.TestCase):
    def test_accepted_forms(self):
        for raw in ("5.2.1", "第5.2.1条", "第 5.2.1 条", "5.2.1条", " 5.2.1 "):
            self.assertEqual(normalize_clause(raw), "5.2.1", raw)

    def test_rejects_garbage(self):
        for raw in ("abc", "5.2.1.x", "第条", ""):
            self.assertIsNone(normalize_clause(raw), raw)

    def test_none_passthrough(self):
        self.assertIsNone(normalize_clause(None))


class CitationTest(unittest.TestCase):
    def test_full_form(self):
        text, warnings = build_citation("建筑防火通用规范", "GB 55037-2022", "5.2.1")
        self.assertEqual(text, "《建筑防火通用规范》GB 55037-2022 第5.2.1条")
        self.assertEqual(warnings, [])

    def test_without_clause(self):
        text, _ = build_citation("建筑防火通用规范", "GB 55037-2022")
        self.assertEqual(text, "《建筑防火通用规范》GB 55037-2022")

    def test_missing_code_warns(self):
        text, warnings = build_citation("某项标准", None, "3.1.2")
        self.assertEqual(text, "《某项标准》 第3.1.2条")
        self.assertTrue(any("编号未核实" in w for w in warnings))

    def test_ambiguous_confidence_warns(self):
        _, warnings = build_citation("某标准", "JGJ 147-2016", "1.0.1", confidence="ambiguous")
        self.assertTrue(any("歧义" in w for w in warnings))


class StrictOfflineConflictTest(unittest.TestCase):
    """--strict 与 --offline 相加只能静默降级，必须当场拒掉（exit 2）。"""

    def test_conflict_is_bad_usage(self):
        self.addCleanup(render.configure, as_json=False, quiet=False)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with (
                mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": "1"}),
                redirect_stdout(buf),
            ):
                code = main([
                    "cite", "GB55037-2022", "5.2.1", "--offline", "--strict", "--json",
                    "--db", str(Path(tmp) / "c.db"),
                ])
            payload = json.loads(buf.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["reason"], "bad_usage")
        self.assertIn("--offline", payload["error"])


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
        # P3-7：strict 失败体不输出候选串；人工复核请去掉 --strict
        self.assertNotIn("cite", cm.exception.extra)
        self.assertNotIn("cite", cm.exception.payload())
        self.assertIn("--offline", cm.exception.hint)


class CiteStaleCacheTest(_CiteCase):
    def test_non_strict_warns_on_stale_cache(self):
        rc, emitted = self._run(_StaleCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["currency_check"]["state"], "unreachable")
        self.assertTrue(any("旧缓存" in w for w in emitted["warnings"]))

    def test_strict_stale_cache_does_not_pass_as_verified(self):
        with self.assertRaises(SourceUnreachable) as cm:
            self._run(_StaleCurrency, strict=True)
        self.assertEqual(cm.exception.exit, 20)


class CiteMultiNameTest(_CiteCase):
    def test_name_path_uses_queried_book_not_first_doc_name(self):
        tmp, store = _make_store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        store.db.execute(
            "UPDATE announcement SET title=?, doc_name=? WHERE id=1",
            ("关于发布《第一本》《第二本》的公告", "第一本"),
        )
        store.db.execute(
            "UPDATE mention SET evidence=? WHERE ann_id=1",
            ("《第二本》编号为GB 1-2020",),
        )
        store.commit()
        self.store = store
        target = self._target()
        target["via"] = "name"
        args = argparse.Namespace(
            ref="第二本",
            clause="5.2.1",
            strict=False,
            offline=False,
            format="json",
            app_context=AppContext(
                store=store, currency=_FakeCurrency(), sources={}, offline=False
            ),
        )
        emitted = {}
        with (
            mock.patch.object(cite_app, "resolve", return_value=target),
            mock.patch.object(cite_app.render, "emit", side_effect=lambda p: emitted.update(p)),
        ):
            cite_app.run(store, args)
        self.assertEqual(emitted["name"], "第二本")
        self.assertTrue(emitted["cite"].startswith("《第二本》"))


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
        self.assertNotIn("cite", cm.exception.extra)
        self.assertNotIn("cite", cm.exception.payload())

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
        self.assertNotIn("cite", cm.exception.extra)
        self.assertNotIn("cite", cm.exception.payload())


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
        self.assertNotIn("cite", cm.exception.extra)
        self.assertNotIn("cite", cm.exception.payload())

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

    def test_multi_target_mentions_are_ambiguous_not_first_target(self):
        tmp, store = _make_store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        store.db.execute("DELETE FROM mention WHERE ann_id=1")
        for code in ("GB/T50362-2022", "GB/T50378-2019"):
            store.upsert_edition(code, code.replace("/", "/ ").replace("-", "-"), None, "ambiguous")
            store.add_mention(1, code, "target_of_revision", "编号修改为")
        store.commit()
        self.store = store
        with self.assertRaises(AmbiguousCode) as cm:
            self._run(_FakeCurrency)
        self.assertEqual(cm.exception.exit, 32)
        self.assertEqual(len(cm.exception.extra["candidates"]), 2)

    def test_classified_multi_target_persists_ambiguous_and_cite_32(self):
        from guiju.domain.codes import classify
        from guiju.store.sqlite import persist_codes

        self.store.db.execute("DELETE FROM mention WHERE ann_id=1")
        self.store.db.execute(
            "UPDATE announcement SET title=? WHERE id=1",
            ("关于局部修订《甲标准》《乙标准》的公告",),
        )
        text = (
            "《甲标准》局部修订，编号修改为GB/T 50362-2022；"
            "《乙标准》局部修订，编号修改为GB/T 50378-2019。"
        )
        persist_codes(self.store, 1, classify(text), "甲标准")
        self.store.commit()
        with self.assertRaises(AmbiguousCode) as cm:
            self._run(_FakeCurrency)
        self.assertEqual(cm.exception.exit, 32)

    def test_same_subject_target_and_primary_is_not_ambiguous(self):
        # 真实页面：JGJ450-2018（primary）与 JGJ/T450-2018（target）是同一本；
        # cite 不能因为两条都 parsed 就 32。
        from guiju.domain.codes import classify
        from guiju.store.sqlite import persist_codes

        self.store.db.execute("DELETE FROM mention WHERE ann_id=1")
        text = (
            "现批准《老年人照料设施建筑设计标准》JGJ450-2018局部修订的条文，"
            "自2027年1月1日起实施。标准编号修改为JGJ/T450-2018。"
        )
        persist_codes(self.store, 1, classify(text, "老年人照料设施建筑设计标准"), "老年人照料设施建筑设计标准")
        self.store.commit()
        rc, emitted = self._run(_FakeCurrency)
        self.assertEqual(rc, 0)
        self.assertIn("JGJ/T 450-2018", emitted["cite"])
        self.assertEqual(emitted["confidence"], "parsed")

    def test_ambiguous_mention_stays_ambiguous(self):
        tmp, store = _make_store("ambiguous")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        self.store = store
        rc, emitted = self._run(_FakeCurrency)
        self.assertEqual(rc, 0)
        self.assertEqual(emitted["confidence"], "ambiguous")
        self.assertTrue(emitted["warnings"])


class CiteRefAsCodeFallbackTest(unittest.TestCase):
    """M14：公告正文没有编号，但 ref 本身就是编号 → 出 unknown 引用而非 31。"""

    def _store_no_code(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.db")
        self.addCleanup(store.close)
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
        src = mock.Mock()
        src.detail.return_value = ("", "无编号正文")
        args = argparse.Namespace(
            ref="GB 50016-2014",
            clause=None,
            strict=False,
            offline=True,
            format="json",
            app_context=AppContext(
                store=store, currency=NullCurrency(), sources={"mohurd": src}, offline=True
            ),
        )
        with mock.patch.object(cite_app, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite_app, "check_currency", return_value=CurrencyCheck(CurrencyState.OFFLINE)):
            rc = cite_app.run(store, args)
        self.assertEqual(rc, 0)

    def test_ref_not_code_still_31(self):
        store = self._store_no_code()
        args = argparse.Namespace(
            ref="不存在的规范名",
            clause=None,
            strict=False,
            offline=True,
            format="json",
            app_context=AppContext(
                store=store, currency=NullCurrency(), sources={}, offline=True
            ),
        )
        with mock.patch.object(cite_app, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite_app, "check_currency", return_value=CurrencyCheck(CurrencyState.OFFLINE)), \
             self.assertRaises(NotIndexed):
            cite_app.run(store, args)


class CiteDbFallbackConfidenceTest(unittest.TestCase):
    """M6：落库兜底编号 confidence 非 parsed → 进 warnings、--strict 硬失败。"""

    def _store_mention(self, confidence):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.db")
        self.addCleanup(store.close)
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
        args = argparse.Namespace(
            ref="某公告",
            clause=None,
            strict=False,
            offline=True,
            format="json",
            app_context=AppContext(
                store=store, currency=NullCurrency(), sources={}, offline=True
            ),
        )
        emitted = {}
        with mock.patch.object(cite_app, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite_app, "check_currency", return_value=CurrencyCheck(CurrencyState.OFFLINE)), \
             mock.patch.object(cite_app.render, "emit", side_effect=lambda p: emitted.update(p)):
            cite_app.run(store, args)
        self.assertTrue(emitted["warnings"], "非 parsed 兜底必须产生 warning")
        self.assertEqual(emitted["confidence"], "ambiguous")

        args.strict = True
        args.offline = False  # strict+offline 本身是 BadUsage，先解除
        args.app_context = AppContext(store=store, currency=mock.Mock(), sources={}, offline=False)
        with mock.patch.object(cite_app, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite_app, "check_currency", return_value=CurrencyCheck(CurrencyState.OFFLINE)), \
             mock.patch.object(cite_app.render, "emit"), \
             self.assertRaises(AmbiguousCode):
            cite_app.run(store, args)

    def test_parsed_fallback_no_extra_warning(self):
        store = self._store_mention("parsed")
        args = argparse.Namespace(
            ref="某公告",
            clause=None,
            strict=False,
            offline=True,
            format="json",
            app_context=AppContext(
                store=store, currency=NullCurrency(), sources={}, offline=True
            ),
        )
        emitted = {}
        with mock.patch.object(cite_app, "resolve", return_value=self._target(store)), \
             mock.patch.object(cite_app, "check_currency", return_value=CurrencyCheck(CurrencyState.OFFLINE)), \
             mock.patch.object(cite_app.render, "emit", side_effect=lambda p: emitted.update(p)):
            cite_app.run(store, args)
        self.assertEqual(emitted["confidence"], "parsed")
        self.assertEqual(emitted["warnings"], [])


if __name__ == "__main__":
    unittest.main()
