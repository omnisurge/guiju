"""Medium 修复的回归测试（core-review 负责片）：

  M3   mcp.announcement 不得落盘：resolve 必须 allow_network=False + 只读存储门面
  M5   codes.classify 角色过度提升：泛动词跨子句 / 纯 doc_name → weak_primary，
       多个不同族 primary → 歧义（primary_key=None）
  M8   sqlite.stats 不重复统计 announcements，且 NULL 年份不丢数
  M12a which 报告 currency_stale / currency_upstream_error；
       只有真不可达（stale）才告警，TTL 缓存命中不告警
"""

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from unittest import mock

from guiju.domain.codes import Confidence, Role, classify
from guiju.mcp import McpServer, _ReadOnlyStore
from guiju.store.sqlite import Store
from guiju.cli import render
from guiju.cli.commands import which as which_mod


class CodesWeakPrimaryTest(unittest.TestCase):
    def test_publish_verb_in_other_clause_does_not_promote(self):
        # 「实施」和 JGJ147-2004 不同子句：它只是顺带提的旧版，不是本次发布对象。
        codes = classify(
            "现批准《建筑拆除工程安全技术规范》JGJ147-2016，自2027年1月1日起实施。"
            "JGJ147-2004届时不再执行。"
        )
        roles = {m.code_key: m.role for m in codes.mentions}
        self.assertEqual(roles["JGJ147-2016"], Role.PRIMARY)
        # 关键：另一个编号不得被泛动词升成 primary（被 demote 成 superseded 也允许）
        self.assertNotEqual(roles["JGJ147-2004"], Role.PRIMARY)
        self.assertEqual(codes.primary_key, "JGJ147-2016")
        self.assertEqual(codes.confidence, Confidence.PARSED)

    def test_publish_verb_single_code_stays_primary(self):
        codes = classify(
            "《住宅性能评定标准》编号为GB/T50362-2022，自2023年5月1日起实施。"
        )
        self.assertEqual(codes.mentions[0].role, Role.PRIMARY)
        self.assertEqual(codes.confidence, Confidence.PARSED)

    def test_doc_name_only_match_is_weak(self):
        # doc_name 出现在句子里、但没有发布动词：最多是弱证据。
        codes = classify(
            "已列入目录的标准需符合《住宅性能评定标准》GB/T50362-2022相关条款。",
            doc_name="住宅性能评定标准",
        )
        self.assertEqual(codes.mentions[0].role, Role.WEAK_PRIMARY)
        # 弱证据不算决定性：confidence 必须停在 ambiguous
        self.assertEqual(codes.confidence, Confidence.AMBIGUOUS)
        self.assertEqual(codes.primary_key, "GB/T50362-2022")

    def test_weak_primary_yields_to_superseded(self):
        # 同一句里「旧版废止」是强证据：弱新版不能压过它。
        codes = classify(
            "JGJ147-2004经审核现已废止。"
            "相关内容见《拆除工程施工安全技术规程》JGJ147-2016适用范围。",
            doc_name="拆除工程施工安全技术规程",
        )
        roles = {m.code_key: m.role for m in codes.mentions}
        self.assertEqual(roles["JGJ147-2004"], Role.SUPERSEDED_REF)
        self.assertEqual(roles["JGJ147-2016"], Role.WEAK_PRIMARY)
        self.assertEqual(codes.confidence, Confidence.AMBIGUOUS)

    def test_multiple_primaries_different_stem_are_ambiguous(self):
        codes = classify("批准发布GB50495-2019和GB/T51367-2019两项标准。")
        self.assertIsNone(codes.primary_key)
        self.assertEqual(codes.confidence, Confidence.AMBIGUOUS)
        keys = {m.code_key for m in codes.mentions}
        self.assertEqual(keys, {"GB50495-2019", "GB/T51367-2019"})

    def test_multiple_primaries_same_stem_pick_newest(self):
        # 同族（同 code_stem）多 primary 没有歧义：新版就是答案。
        codes = classify(
            "批准《老年人照料设施建筑设计标准》JGJ/T450-2018实施，JGJ/T450-2008同时发布。"
        )
        self.assertEqual(codes.primary_key, "JGJ/T450-2018")
        self.assertEqual(codes.confidence, Confidence.PARSED)


class StatsNoDoubleCountTest(unittest.TestCase):
    def test_stats_counts_null_year_once(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            "os.environ", {"GUIJU_HOME": home}
        ):
            store = Store()
            store.upsert_announcement("https://x/1.html", "2024-01-01", "t", "d", "national")
            store.upsert_announcement("https://x/2.html", "2024-06-01", "t", "d", "national")
            store.upsert_announcement("https://x/3.html", None, "t", "d", "national")
            store.commit()
            s = store.stats()
            self.assertEqual(s["announcements"], 3)
            self.assertEqual(s["by_year"], {"2024": 2})
            store.close()

    def test_stats_does_not_call_count_announcements(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            "os.environ", {"GUIJU_HOME": home}
        ):
            store = Store()
            store.upsert_announcement("https://x/1.html", "2024-01-01", "t", "d", "national")
            with mock.patch.object(
                store, "count_announcements", wraps=store.count_announcements
            ) as spy:
                store.stats()
                self.assertEqual(spy.call_count, 0)
            store.close()


class _FakeBackend:
    name = "fake"

    def lookup(self, code):
        return []


class AnnouncementReadOnlyTest(unittest.TestCase):
    def _server(self):
        return McpServer(_FakeBackend())

    def test_announcement_uses_readonly_offline_resolve(self):
        ann = {
            "id": 7,
            "url": "https://www.mohurd.gov.cn/gov/zhengce/x/7.html",
            "published_on": "2024-01-05",
            "title": "《工程结构通用规范》发布公告",
            "doc_name": "工程结构通用规范",
            "category": "national",
        }
        target = {
            "ann": ann, "code_key": "GB55001-2021", "code_display": "GB 55001-2021",
            "confidence": "parsed", "status": "current",
        }

        class FakeSource:
            id = "mohurd"
            api_mode = False

            def detail(self, url):
                return ("<html>body</html>", "正文")

            def attachments(self, html):
                return []

        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            "os.environ", {"GUIJU_HOME": home}
        ), mock.patch(
            "guiju.cli.lookup.resolve", return_value=target
        ) as resolve_mock, mock.patch(
            "guiju.sources.registry", return_value={"mohurd": FakeSource()}
        ):
            resp = self._server().handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "announcement", "arguments": {"ref": "GB 55001-2021"}},
            })
            self.assertFalse(resp["result"]["isError"])
            _, kwargs = resolve_mock.call_args
            self.assertEqual(kwargs.get("allow_network"), False)
            store_arg = resolve_mock.call_args.args[0]
            self.assertIsInstance(store_arg, _ReadOnlyStore)

    def test_readonly_store_noops_writes(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            "os.environ", {"GUIJU_HOME": home}
        ):
            real = Store()
            real.upsert_announcement("https://x/keep.html", "2024-01-01", "t", "d", "national")
            real.commit()
            ro = _ReadOnlyStore(real)
            # 只读读通：读方法原样转发
            self.assertEqual(ro.count_announcements(), 1)
            # 写路径全部吃掉：upsert_announcement 还要回 (id, inserted) 形状
            rid, inserted = ro.upsert_announcement(
                "https://x/new.html", "2024-01-02", "t", "d", "national")
            self.assertFalse(inserted)
            ro.upsert_edition("GB1-2020", "GB 1-2020", "t", "parsed")
            ro.add_mention(1, "GB1-2020", "primary", "ev")
            ro.mark_scanned(1, 1)
            ro.log_probe("mohurd", "https://x", 200, 1, "ok")
            ro.set_meta("k", "v")
            ro.cache_currency("native", "GB1", "{}", "2024-01-01")
            ro.commit()
            # 底层库纹丝不动
            self.assertEqual(real.count_announcements(), 1)
            self.assertIsNone(real.get_ann_by_url("https://x/new.html"))
            self.assertEqual(real.stats()["editions"], 0)
            real.close()


class _FakeCurrency:
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
            ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with tempfile.TemporaryDirectory() as home, mock.patch.dict(
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
        cur = _FakeCurrency()
        cur.stale = True
        cur.upstream_error = "dial timeout"
        out, err = self._run_which(cur)
        self.assertIn('"currency_stale": true', out)
        self.assertIn('"currency_upstream_error": "dial timeout"', out)
        self.assertIn("时效服务不可达、结论基于旧缓存", err)

    def test_fresh_cache_hit_does_not_warn_stale(self):
        # stale=False：TTL 命中不算服务不可达，不发那条告警。
        cur = _FakeCurrency()
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
