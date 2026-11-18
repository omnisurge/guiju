"""B1/W6 · P1-3：多名称公告不许拿第一个书名代表整条公告。

背景（docs/audit/code-review-2026-11-18.md P1-3）：标题形如
「关于批准发布《A》《B》的通知」时，doc_name 只存了 A；按 B 名称 pull 会拿
A 的附件并 exit 0 —— 抓错比抓不到更糟。

fixture：tests/fixtures/announcements/multi_name_20261118.json
（本地目录 3256 条里真实存在的 7 条多名称公告，只读导出，未联网；
公告页未缓存，附件标签按标题名称构造，不是抓取结果）。
"""

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.cli import render
from guiju.cli.commands.pull import _pull_one
from guiju.domain.announcement import classify_title, extract_names, match_name, names_of
from guiju.domain.attachment import Attachment, Verdict, pick
from guiju.errors import AmbiguousAttachment, AmbiguousCode
from guiju.store.sqlite import Store

from .paths import FIXTURES

_ROWS = json.loads(
    (FIXTURES / "announcements" / "multi_name_20261118.json").read_text(encoding="utf-8")
)["announcements"]

MULTI = next(row for row in _ROWS if row["id"] == 293)
THREE = next(row for row in _ROWS if row["id"] == 3254)
TRIPLE_TITLE = THREE["title"]


def attachments_for(row):
    """按标题名称构造「每个名称一份 PDF」，用于验证「选哪一本」（名称来自真实目录）。"""
    return [
        Attachment(url=f"https://www.mohurd.gov.cn/dl/{row['id']}-{i}.pdf", label=name)
        for i, name in enumerate(row["names"])
    ]


class FixtureTest(unittest.TestCase):
    def test_seven_real_multi_name_titles(self):
        self.assertEqual(len(_ROWS), 7, "本地目录导出 2026-11-18 恰有 7 条多名称公告")
        for row in _ROWS:
            self.assertGreaterEqual(len(extract_names(row["title"])), 2, row["id"])
        self.assertEqual(sum(1 for row in _ROWS if len(row["names"]) == 3), 1)


class ExtractNamesTest(unittest.TestCase):
    def test_every_book_name_is_kept_in_order(self):
        for row in _ROWS:
            self.assertEqual(extract_names(row["title"]), tuple(row["names"]), row["id"])
            # doc_name 只作展示：必须是第一个名称，但不是唯一名称
            self.assertEqual(row["doc_name"], row["names"][0], row["id"])

    def test_three_real_separators_are_covered(self):
        # 三条真实语式都出现过：「》《」「》和《」「》、《」
        titles = {row["id"]: row["title"] for row in _ROWS}
        self.assertIn("》《", titles[293])
        self.assertIn("》和《", titles[2689])
        self.assertIn("》、《", titles[3254])
        for row in _ROWS:
            names = extract_names(row["title"])
            self.assertEqual(len(names), len(set(names)))

    def test_no_book_marks_returns_empty(self):
        self.assertEqual(extract_names("关于发布国家标准的公告"), ())
        self.assertEqual(extract_names(None), ())

    def test_names_of_falls_back_to_doc_name(self):
        # shzjw 这类 api_mode 源没有公告页标题：名称就是落库的 name
        self.assertEqual(names_of("地质灾害危险性评估技术标准", "地质灾害危险性评估技术标准"),
                         ("地质灾害危险性评估技术标准",))
        self.assertEqual(names_of("", ""), ())
        self.assertEqual(names_of(MULTI["title"], MULTI["doc_name"]), tuple(MULTI["names"]))

    def test_classify_title_still_first_name_and_category(self):
        name, category = classify_title(MULTI["title"])
        self.assertEqual(name, MULTI["names"][0])
        self.assertEqual(category, "other")


class MatchNameTest(unittest.TestCase):
    def test_exact_second_name_matches_that_book(self):
        chosen, matched = match_name(tuple(MULTI["names"]), MULTI["names"][1])
        self.assertEqual(chosen, MULTI["names"][1])
        self.assertEqual(matched, (MULTI["names"][1],))

    def test_partial_name_matches_one_book(self):
        chosen, matched = match_name(tuple(MULTI["names"]), "湿地保护")
        self.assertEqual(chosen, "湿地保护工程项目建设标准")
        self.assertEqual(len(matched), 1)

    def test_ambiguous_partial_name_returns_candidates(self):
        names = ("城市给水工程项目建设标准", "城市污水处理工程项目建设标准")
        chosen, matched = match_name(names, "城市")
        self.assertIsNone(chosen, "对不上唯一一本时不能替人挑")
        self.assertEqual(set(matched), set(names))

    def test_unrelated_name_matches_nothing(self):
        chosen, matched = match_name(tuple(MULTI["names"]), "建筑防火通用规范")
        self.assertIsNone(chosen)
        self.assertEqual(matched, ())


class PickMultiNameTest(unittest.TestCase):
    """按名称要哪一本就下哪一本；按编号/ID 说不清是哪一本 → 33。"""

    def test_multi_name_announcement_requires_target_name(self):
        got = pick(attachments_for(MULTI), MULTI["doc_name"], announcement_title=MULTI["title"])
        self.assertIs(got.verdict, Verdict.AMBIGUOUS)
        self.assertIsNone(got.choice)
        self.assertEqual(len(got.candidates), 2, "candidates 要交人挑，不许 doc_name 兜底")
        self.assertEqual(got.book_names, tuple(MULTI["names"]))

    def test_by_requested_second_name_picks_second_book(self):
        got = pick(
            attachments_for(MULTI),
            tuple(MULTI["names"]),
            ref_name=MULTI["names"][1],
            announcement_title=MULTI["title"],
        )
        self.assertIs(got.verdict, Verdict.OK)
        self.assertEqual(got.choice.label, MULTI["names"][1])

    def test_by_requested_first_name_picks_first_book(self):
        got = pick(
            attachments_for(MULTI),
            tuple(MULTI["names"]),
            ref_name=MULTI["names"][0],
            announcement_title=MULTI["title"],
        )
        self.assertIs(got.verdict, Verdict.OK)
        self.assertEqual(got.choice.label, MULTI["names"][0])

    def test_partial_requested_name_picks_only_that_book(self):
        got = pick(
            attachments_for(MULTI),
            tuple(MULTI["names"]),
            ref_name="湿地保护",
            announcement_title=MULTI["title"],
        )
        self.assertIs(got.verdict, Verdict.OK)
        self.assertEqual(got.choice.label, "湿地保护工程项目建设标准")

    def test_three_books_need_a_choice(self):
        got = pick(attachments_for(THREE), THREE["doc_name"], announcement_title=TRIPLE_TITLE)
        self.assertIs(got.verdict, Verdict.AMBIGUOUS)
        self.assertEqual(len(got.candidates), 3)
        self.assertEqual(got.book_names, tuple(THREE["names"]))

    def test_single_book_announcement_keeps_doc_name_matching(self):
        items = [
            Attachment(url="https://www.mohurd.gov.cn/dl/a.doc", label="废止的强制性条文"),
            Attachment(url="https://www.mohurd.gov.cn/dl/b.pdf", label="建筑防火通用规范"),
        ]
        got = pick(items, "建筑防火通用规范", announcement_title="关于发布《建筑防火通用规范》的公告")
        self.assertIs(got.verdict, Verdict.OK)
        self.assertEqual(got.choice.label, "建筑防火通用规范")

    def test_multi_book_with_one_attachment_is_still_ambiguous(self):
        # 只有一份附件也不行：它可能是另一本书的正文（按编号查询不知道要哪本）
        only = [Attachment(url="https://www.mohurd.gov.cn/dl/a.pdf", label="自然保护区规范")]
        got = pick(only, MULTI["doc_name"], announcement_title=MULTI["title"])
        self.assertIs(got.verdict, Verdict.AMBIGUOUS)
        self.assertIsNone(got.choice)

    def test_multi_book_by_name_with_single_matching_attachment_is_ok(self):
        # 用户指明了第二本，且页面上就这一份、标签也对得上 → 可以下
        only = [Attachment(
            url="https://www.mohurd.gov.cn/dl/b.pdf", label="湿地保护工程项目建设标准")]
        got = pick(
            only,
            tuple(MULTI["names"]),
            ref_name=MULTI["names"][1],
            announcement_title=MULTI["title"],
        )
        self.assertIs(got.verdict, Verdict.OK)
        self.assertEqual(got.choice.label, MULTI["names"][1])

    def test_multi_book_by_name_without_label_match_is_ambiguous(self):
        # 指定了第二本，但附件标签被截断/改写：不许退回「唯一 PDF」替人挑
        items = [
            Attachment(url="https://www.mohurd.gov.cn/dl/1.pdf", label="自然保护区工程"),
            Attachment(url="https://www.mohurd.gov.cn/dl/2.doc", label="湿地保护工程"),
        ]
        got = pick(
            items,
            tuple(MULTI["names"]),
            ref_name=MULTI["names"][1],
            announcement_title=MULTI["title"],
        )
        self.assertIs(got.verdict, Verdict.AMBIGUOUS)
        self.assertIsNone(got.choice)

    def test_payload_carries_names_and_candidates(self):
        got = pick(attachments_for(MULTI), MULTI["doc_name"], announcement_title=MULTI["title"])
        payload = got.as_payload()
        self.assertEqual(payload["verdict"], "ambiguous")
        self.assertEqual(payload["names"], list(MULTI["names"]))
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertIn("2 本", payload["pick_reason"])


class _FakeSource:
    api_mode = False
    id = "mohurd"
    display_name = "住房和城乡建设部"

    def __init__(self, atts, text="正文没有编号"):
        self.atts = atts
        self.text = text
        self.downloads = []
        self.detail_calls = []

    def detail(self, url):
        self.detail_calls.append(url)
        return "<html></html>", self.text

    def attachments(self, html):
        return list(self.atts)

    def download_to(self, url, part_path, referer=None):
        self.downloads.append(url)
        return 2048, "pdf", None


class PullMultiNameTest(unittest.TestCase):
    """pull 的入口分流：按名称用该名称，按编号/ID 在标题多本时 33。"""

    def setUp(self):
        render.configure(as_json=True, quiet=True)
        self.addCleanup(render.configure, as_json=False, quiet=False)

    def _target(self, row, via):
        ann = {
            "id": row["id"],
            "url": row["url"],
            "title": row["title"],
            "doc_name": row["doc_name"],
            "published_on": row["published_on"],
            "category": "national",
        }
        return {
            "ann": ann,
            "code_key": None,
            "code_display": None,
            "confidence": "unknown",
            "via": via,
            "status": "",
        }

    def _run(self, row, ref, via, *, cached=None, expect_ambiguous=True, text="正文没有编号"):
        source = _FakeSource(attachments_for(row), text=text)
        store = mock.Mock()
        args = argparse.Namespace(force=False, readable=False, out=None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        find_cached = mock.Mock(return_value=cached)
        with (
            mock.patch("guiju.cli.commands.pull.resolve", return_value=self._target(row, via)),
            mock.patch("guiju.cli.commands.pull.find_cached", find_cached),
            mock.patch("guiju.cli.commands.pull.sha256_file", return_value="0" * 64),
            mock.patch("guiju.cli.commands.pull.sha256_bytes", return_value="key"),
            mock.patch("os.replace"),
            mock.patch("guiju.cli.commands.pull.render.warn"),
        ):
            if expect_ambiguous:
                with self.assertRaises(AmbiguousAttachment) as cm:
                    _pull_one(store, args, ref, source, Path(tmp.name))
                self.assertEqual(cm.exception.exit, 33)
                return cm.exception, source, find_cached
            payload = _pull_one(store, args, ref, source, Path(tmp.name))
        return payload, source, find_cached

    def test_pull_by_second_name_uses_that_name(self):
        payload, source, _ = self._run(
            MULTI, MULTI["names"][1], "name", expect_ambiguous=False,
            text="现批准《湿地保护工程项目建设标准》，编号为GB 2-2020。",
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["name"], MULTI["names"][1])
        self.assertEqual(len(source.downloads), 1)
        self.assertEqual(source.downloads[0], attachments_for(MULTI)[1].url)
        # 文件按用户要的那一本命名，不按目录里先落库的 doc_name
        self.assertIn(MULTI["names"][1], Path(payload["path"]).name)

    def test_pull_by_code_is_ambiguous_33(self):
        exc, source, _ = self._run(MULTI, "GB 1-2020", "code_local")
        self.assertEqual(exc.exit, 33)
        self.assertEqual(len(exc.extra["candidates"]), 2)
        self.assertEqual(source.downloads, [], "33 之后绝不许下载任何一个附件")

    def test_pull_by_ann_id_is_ambiguous_33(self):
        exc, source, _ = self._run(MULTI, str(MULTI["id"]), "ann_id")
        self.assertEqual(exc.exit, 33)
        self.assertEqual(source.downloads, [])

    def test_pull_by_code_does_not_shortcut_to_cached_first_book(self):
        # 幂等快路径不能把「目录里先落库那本」的旧文件当成「已有」返回 10
        cached = (Path("/tmp/自然保护区工程项目建设标准__x.pdf"), "0" * 64)
        exc, _source, find_cached = self._run(MULTI, "GB 1-2020", "code_local", cached=cached)
        self.assertEqual(exc.exit, 33)
        self.assertFalse(find_cached.called, "多本公告按编号 pull 必须先 33，不能命中缓存")


class PullRealStoreTest(unittest.TestCase):
    """P1-A/P1-B：多名称 pull 的编号归属与 33 前不写库，必须用真实 Store 验证。"""

    def setUp(self):
        render.configure(as_json=True, quiet=True)
        self.addCleanup(render.configure, as_json=False, quiet=False)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement(
            MULTI["url"], MULTI["published_on"], MULTI["title"],
            MULTI["doc_name"], "other",
        )
        self.store.commit()

    def _target(self, via, ref):
        ann = dict(self.store.get_ann(1))
        return {
            "ann": ann, "code_key": None, "code_display": None,
            "confidence": "unknown", "via": via, "status": "",
        }

    def _run(self, text, via, ref, expect_error=None):
        source = _FakeSource(attachments_for(MULTI), text=text)
        args = argparse.Namespace(force=False, readable=False, out=None)
        with (
            mock.patch("guiju.cli.commands.pull.resolve", return_value=self._target(via, ref)),
            mock.patch("guiju.cli.commands.pull.find_cached", return_value=None),
            mock.patch("guiju.cli.commands.pull.sha256_file", return_value="0" * 64),
            mock.patch("guiju.cli.commands.pull.sha256_bytes", return_value="key"),
            mock.patch("os.replace"),
            mock.patch("guiju.cli.commands.pull.render.warn"),
        ):
            if expect_error is not None:
                with self.assertRaises(expect_error) as cm:
                    _pull_one(self.store, args, ref, source, Path(self.tmp.name))
                self.assertEqual(cm.exception.exit, expect_error.exit)
                return source, cm.exception
            payload = _pull_one(self.store, args, ref, source, Path(self.tmp.name))
        return source, payload

    def test_second_name_with_first_book_code_is_32(self):
        text = "现批准《自然保护区工程项目建设标准》，编号为GB 1-2020。"
        source, exc = self._run(text, "name", MULTI["names"][1], expect_error=AmbiguousCode)
        self.assertEqual(exc.exit, 32)
        self.assertEqual(source.downloads, [])
        self.assertEqual(self.store.mentions_for(1), [])

    def test_second_name_with_second_code_uses_it(self):
        text = (
            "现批准《自然保护区工程项目建设标准》，编号为GB 1-2020。"
            "现批准《湿地保护工程项目建设标准》，编号为GB 2-2020。"
        )
        source, payload = self._run(text, "name", MULTI["names"][1])
        self.assertEqual(payload["code"], "GB 2-2020")
        self.assertEqual(source.downloads, [attachments_for(MULTI)[1].url])
        rows = self.store.mentions_for(1)
        self.assertEqual([r["code_key"] for r in rows], ["GB2-2020"])

    def test_cite_after_second_name_pull_uses_second_book_and_code(self):
        from guiju import currency as currency_mod
        from guiju.cli.commands import cite

        text = (
            "现批准《自然保护区工程项目建设标准》，编号为GB 1-2020。"
            "现批准《湿地保护工程项目建设标准》，编号为GB 2-2020。"
        )
        _source, payload = self._run(text, "name", MULTI["names"][1])
        self.assertEqual(payload["code"], "GB 2-2020")
        target = self._target("name", MULTI["names"][1])

        class EmptyCurrency:
            name = "fake"
            complete = True

            def __init__(self, inner=None, store=None):
                pass

            def lookup(self, code):
                return []

        emitted = {}
        args = argparse.Namespace(
            ref=MULTI["names"][1], clause="", strict=False, offline=False, format="json"
        )
        with (
            mock.patch.object(cite, "resolve", return_value=target),
            mock.patch.object(currency_mod, "CachedCurrency", EmptyCurrency),
            mock.patch.object(cite.render, "emit", side_effect=lambda p: emitted.update(p)),
        ):
            cite.run(self.store, args)
        self.assertTrue(emitted["cite"].startswith(f"《{MULTI['names'][1]}》"))
        self.assertEqual(emitted["code"], "GB 2-2020")
        self.assertEqual(emitted["confidence"], "parsed")

        # 同一公告按编号 cite 时也要用该编号 evidence 对应的书名，而不是第一本。
        by_code = self._target("code_local", "GB 2-2020")
        by_code["code_key"] = "GB2-2020"
        by_code["code_display"] = "GB 2-2020"
        emitted2 = {}
        args2 = argparse.Namespace(
            ref="GB 2-2020", clause="", strict=False, offline=False, format="json"
        )
        with (
            mock.patch.object(cite, "resolve", return_value=by_code),
            mock.patch.object(currency_mod, "CachedCurrency", EmptyCurrency),
            mock.patch.object(cite.render, "emit", side_effect=lambda p: emitted2.update(p)),
        ):
            cite.run(self.store, args2)
        self.assertTrue(emitted2["cite"].startswith(f"《{MULTI['names'][1]}》"))

    def test_ambiguous_33_does_not_touch_existing_mentions(self):
        self.store.upsert_edition("GB2-2020", "GB 2-2020", None, "parsed")
        self.store.add_mention(1, "GB2-2020", "primary", "已有编号")
        self.store.commit()
        source, exc = self._run("正文没有编号", "ann_id", "1", expect_error=AmbiguousAttachment)
        self.assertEqual(exc.exit, 33)
        self.assertEqual(source.downloads, [])
        self.assertEqual([r["code_key"] for r in self.store.mentions_for(1)], ["GB2-2020"])


class McpAnnouncementSurfaceTest(unittest.TestCase):
    """跨面契约（ADR 0004 D6）：MCP 的 announcement 与 CLI pull 同一套裁定。

    mcp.py 不在本次写范围里，但它传的是 (doc_name, announcement_title=title)；
    pick 从标题里自己认出多本，所以服务面也自动 33，不再给第一个附件的链接。
    """

    def test_mcp_announcement_reports_ambiguous_for_multi_name_title(self):
        from guiju.cli import lookup as lookup_mod
        from guiju.mcp import McpServer

        class Backend:
            name = "fake"

            def lookup(self, code):
                return []

        server = McpServer(Backend())
        self.addCleanup(server.close)
        ann = {
            "id": MULTI["id"],
            "url": MULTI["url"],
            "published_on": MULTI["published_on"],
            "title": MULTI["title"],
            "doc_name": MULTI["doc_name"],
            "category": "other",
        }
        target = {
            "ann": ann, "code_key": None, "code_display": None,
            "confidence": "unknown", "status": "",
        }

        class FakeSource:
            id = "mohurd"
            api_mode = False

            def detail(self, url):
                return "<html></html>", "正文"

            def attachments(self, html):
                return attachments_for(MULTI)

        with (
            tempfile.TemporaryDirectory() as home,
            mock.patch.dict("os.environ", {"GUIJU_HOME": home}),
            mock.patch.object(lookup_mod, "resolve", return_value=target),
            mock.patch("guiju.sources.registry", return_value={"mohurd": FakeSource()}),
        ):
            response = server.handle({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "announcement", "arguments": {"ref": str(MULTI["id"])}},
            })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["attachment_verdict"], "ambiguous")
        self.assertIsNone(payload["attachment_url"], "多本公告不许给第一个附件的链接")
        self.assertEqual(len(payload["attachment_candidates"]), 2)

    def test_mcp_announcement_name_path_uses_second_book(self):
        # P1-3 跨面：按第二本名称查 MCP，也必须用第二本做附件匹配，不能回退 doc_name。
        from guiju.cli import lookup as lookup_mod
        from guiju.mcp import McpServer

        class Backend:
            name = "fake"

            def lookup(self, code):
                return []

        server = McpServer(Backend())
        self.addCleanup(server.close)
        ann = {
            "id": MULTI["id"], "url": MULTI["url"],
            "published_on": MULTI["published_on"], "title": MULTI["title"],
            "doc_name": MULTI["doc_name"], "category": "other",
        }
        target = {
            "ann": ann, "code_key": None, "code_display": None,
            "confidence": "unknown", "status": "", "via": "name",
        }

        class FakeSource:
            id = "mohurd"
            api_mode = False

            def detail(self, url):
                return "<html></html>", "正文"

            def attachments(self, html):
                return attachments_for(MULTI)

        with (
            tempfile.TemporaryDirectory() as home,
            mock.patch.dict("os.environ", {"GUIJU_HOME": home}),
            mock.patch.object(lookup_mod, "resolve", return_value=target),
            mock.patch("guiju.sources.registry", return_value={"mohurd": FakeSource()}),
        ):
            response = server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "announcement",
                    "arguments": {"ref": MULTI["names"][1]},
                },
            })
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["attachment_verdict"], "ok")
        self.assertEqual(payload["attachment_url"], attachments_for(MULTI)[1].url)
        self.assertEqual(payload["name"], MULTI["names"][1])


if __name__ == "__main__":
    unittest.main()
