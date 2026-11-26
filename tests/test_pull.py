"""pull 命令：AppContext 注入、availability 字段传播、api_mode 落库与 cite 闭环测试。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from guiju import report as render
from guiju.application import AppContext, pull
from guiju.application.pull import _codes_for_book, _pull_api, _pull_one
from guiju.cli import main
from guiju.cli.commands import pull as pull_cli
from guiju.currency import CachedCurrency
from guiju.domain import attachment as att_domain
from guiju.domain import availability, naming
from guiju.domain.codes import Codes, Confidence, Mention, Role, normalize_key
from guiju.errors import AmbiguousAttachment, AmbiguousCode, BadAttachment, GuijuError, NotIndexed
from guiju.files import sha256_bytes
from guiju.net.request import ProbeRecord
from guiju.store.sqlite import Store

PDF = b"%PDF-1.7\n" + b"x" * 500
ANN = {
    "id": 1,
    "url": "https://www.mohurd.gov.cn/x/1",
    "doc_name": "某标准",
    "title": "关于发布《某标准》的公告",
    "category": "",
    "published_on": "2026-01-01",
}


def _args(**kwargs):
    base = {
        "list": "-",
        "force": True,
        "readable": False,
        "out": None,
        "source": "mohurd",
    }
    base.update(kwargs)
    base.setdefault(
        "app_context",
        AppContext(
            store=None,
            currency=mock.Mock(),
            out_dir=Path(base["out"]).expanduser() if base["out"] else None,
            sources={},
        ),
    )
    return argparse.Namespace(**base)


def _target(**overrides):
    target = {
        "ann": dict(ANN),
        "code_key": None,
        "code_display": None,
        "status": "",
        "confidence": "unknown",
        "versions": [],
    }
    target.update(overrides)
    return target


class PullCurrencyStoreTest(unittest.TestCase):
    """M9：CLI 入口必须装配带 store 的 CachedCurrency 并经 AppContext 注入 pull。"""

    def test_pull_cli_injects_store_backed_currency(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / "c.db")
        self.addCleanup(store.close)
        args = argparse.Namespace(
            ref="GB 50016-2014", out=tmp.name, force=False, readable=False,
            source="mohurd", list=None,
        )
        source = mock.Mock(spec=[])  # api_mode 缺失 → 走非 API 分支
        captured = {}

        def fake_resolve(s, ref, currency=None, allow_network=True, fetch_detail=None, sources=None):
            captured["currency"] = currency
            return {"ann": {"id": 1, "url": "u", "doc_name": "d", "published_on": "2020-01-01"},
                    "versions": [], "code_key": None, "code_display": None}

        with (
            mock.patch.object(pull, "resolve", side_effect=fake_resolve),
            mock.patch.object(pull, "find_cached", return_value=None),
            mock.patch.object(pull, "availability"),
            contextlib.suppress(Exception),
        ):
            # 我们只关心 CLI 入口装配后传给 resolve 的 currency，后续下载路径允许失败
            pull_cli.run(store, args, sources={"mohurd": source})
        self.assertIsInstance(captured.get("currency"), CachedCurrency)
        self.assertIs(captured["currency"].store, store)

        with self.assertRaises(ValueError):
            _pull_one(
                store,
                argparse.Namespace(force=False, readable=False, out=tmp.name),
                "GB 50016-2014",
                source,
                Path(tmp.name),
            )


class PullOneAvailabilityTest(unittest.TestCase):
    def _raise_from_pull_one(self, source, *, target=None, store=None):
        store = store or mock.Mock()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            pull, "resolve", return_value=target or _target()
        ), self.assertRaises(GuijuError) as ctx:
            _pull_one(store, _args(), "GB 1-2020", source, Path(tmp))
        return ctx.exception

    def test_detail_bad_attachment_has_availability(self):
        source = mock.Mock()
        source.api_mode = False
        source.detail.side_effect = BadAttachment("详情页附件异常")
        exc = self._raise_from_pull_one(source)
        payload = exc.payload()
        self.assertEqual(payload["availability"], "in_window")
        self.assertEqual(
            payload["availability_note"],
            availability.note(availability.Window.IN_WINDOW, ANN["published_on"]),
        )

    def test_attachments_bad_attachment_has_availability(self):
        source = mock.Mock()
        source.api_mode = False
        source.detail.return_value = ("<html></html>", "正文")
        source.attachments.side_effect = BadAttachment("附件列表异常")
        with mock.patch.object(pull, "classify", return_value=Codes()):
            exc = self._raise_from_pull_one(source)
        payload = exc.payload()
        self.assertEqual(payload["availability"], "in_window")
        self.assertIn("availability_note", payload)

    def test_download_bad_attachment_has_availability_and_attachments(self):
        source = mock.Mock()
        source.api_mode = False
        source.detail.return_value = ("<html></html>", "正文")
        source.attachments.return_value = [
            att_domain.Attachment(url="https://x/a.pdf", label="某标准.pdf", kind="pdf")
        ]
        choice = att_domain.Attachment(url="https://x/a.pdf", label="某标准.pdf", kind="pdf")
        pick = att_domain.Pick(
            att_domain.Verdict.OK, choice=choice, candidates=(choice,), reason="唯一附件"
        )
        codes = Codes(
            mentions=(Mention("GB1-2020", "GB 1-2020", Role.PRIMARY, "发布GB 1-2020"),),
            primary_key="GB1-2020",
            primary_display="GB 1-2020",
            confidence=Confidence.PARSED,
        )
        source.download_to.side_effect = BadAttachment("不是 PDF")
        store = mock.Mock()
        with (
            mock.patch.object(pull, "classify", return_value=codes),
            mock.patch.object(pull.att_domain, "pick", return_value=pick),
            mock.patch.object(pull, "find_cached", return_value=None),
            mock.patch.object(pull, "persist_codes"),
        ):
            exc = self._raise_from_pull_one(source, store=store)
        payload = exc.payload()
        self.assertEqual(payload["availability"], "in_window")
        self.assertIn("attachments", payload)

    def test_ambiguous_attachment_has_availability_fields(self):
        source = mock.Mock()
        source.api_mode = False
        source.detail.return_value = ("<html></html>", "正文")
        source.attachments.return_value = []
        candidate = att_domain.Attachment(url="https://x/a.pdf", label="甲.pdf", kind="pdf")
        pick = att_domain.Pick(
            att_domain.Verdict.AMBIGUOUS,
            candidates=(candidate,),
            reason="多个候选无法锁定",
            book_names=("某标准",),
        )
        with (
            mock.patch.object(pull, "classify", return_value=Codes()),
            mock.patch.object(pull.att_domain, "pick", return_value=pick),
        ):
            exc = self._raise_from_pull_one(source)
        self.assertIsInstance(exc, AmbiguousAttachment)
        payload = exc.payload()
        self.assertEqual(payload["availability"], "in_window")
        self.assertIn("availability_note", payload)
        self.assertEqual(payload["verdict"], "ambiguous")
        self.assertEqual(payload["names"], ["某标准"])
        self.assertEqual(payload["pick_reason"], "多个候选无法锁定")

    def test_resolve_early_failure_has_unknown_availability(self):
        source = mock.Mock()
        source.api_mode = False
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            pull, "resolve", side_effect=NotIndexed("本地目录没有", hint="先 sync")
        ), self.assertRaises(NotIndexed) as ctx:
            _pull_one(mock.Mock(), _args(), "GB 404-2020", source, Path(tmp))
        payload = ctx.exception.payload()
        self.assertEqual(payload["availability"], "unknown")
        self.assertIn("availability_note", payload)

    def test_multi_name_mismatch_ambiguous_code_has_availability(self):
        ann = dict(
            ANN,
            title="关于发布《甲标准》《乙标准》的公告",
            doc_name="甲标准",
        )
        target = _target(
            ann=ann,
            via="name",
            code_key="GB1-2020",
            code_display="GB 1-2020",
            confidence="parsed",
            status="current",
        )
        source = mock.Mock()
        source.api_mode = False
        source.detail.return_value = ("<html></html>", "正文")
        codes = Codes(
            mentions=(
                Mention("GB2-2020", "GB 2-2020", Role.PRIMARY, "发布《乙标准》GB 2-2020"),
            ),
            primary_key="GB2-2020",
            primary_display="GB 2-2020",
            confidence=Confidence.PARSED,
        )
        with (
            mock.patch.object(pull, "classify", return_value=codes),
            mock.patch.object(pull, "find_cached", return_value=None),
        ):
            exc = self._raise_from_pull_one(source, target=target)
        self.assertIsInstance(exc, AmbiguousCode)
        payload = exc.payload()
        self.assertEqual(payload["availability"], "in_window")
        self.assertIn("availability_note", payload)
        self.assertIn("candidates", payload)

    def test_codes_for_book_ambiguous_has_availability(self):
        codes = Codes(
            mentions=(Mention("GB2-2020", "GB 2-2020", Role.PRIMARY, "发布《乙》GB 2-2020"),),
            primary_key="GB2-2020",
            primary_display="GB 2-2020",
            confidence=Confidence.PARSED,
        )
        with self.assertRaises(AmbiguousCode) as ctx:
            _codes_for_book(
                codes,
                "甲",
                window=availability.Window.IN_WINDOW,
                published_on="2026-01-01",
            )
        payload = ctx.exception.payload()
        self.assertEqual(payload["availability"], "in_window")
        self.assertIn("availability_note", payload)


class PullApiAvailabilityTest(unittest.TestCase):
    def _source(self, hits, *, download=None):
        source = mock.Mock()
        source.id = "fakeapi"
        source.display_name = "测试地标源"
        source.api_mode = True
        source.find.return_value = hits
        if download is not None:
            source.download_to.side_effect = download
        return source

    def _raise_api(self, source, ref="DG/TJ08-1-2020"):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(GuijuError) as ctx:
            _pull_api(mock.Mock(), _args(), ref, source, Path(tmp))
        return ctx.exception

    def test_multi_hit_32_uses_unknown_not_hits0_date(self):
        hits = [
            {"code": "DG/TJ08-1-2026", "name": "甲", "published_on": "2026-01-01",
             "download_url": "https://x/a", "page_url": "p", "status": "current"},
            {"code": "DG/TJ08-2-2026", "name": "乙", "published_on": "2026-02-02",
             "download_url": "https://x/b", "page_url": "q", "status": "current"},
        ]
        exc = self._raise_api(self._source(hits))
        payload = exc.payload()
        self.assertEqual(payload["exit"], 32)
        self.assertEqual(payload["availability"], "unknown")

    def test_no_hit_31_has_unknown_availability(self):
        exc = self._raise_api(self._source([]))
        payload = exc.payload()
        self.assertEqual(payload["exit"], 31)
        self.assertEqual(payload["availability"], "unknown")

    def test_no_download_url_bad_attachment_has_availability(self):
        rec = {
            "code": "DG/TJ08-1-2010",
            "name": "甲",
            "published_on": "2010-01-01",
            "download_url": None,
            "page_url": "p",
            "status": "current",
        }
        exc = self._raise_api(self._source([rec]))
        payload = exc.payload()
        self.assertEqual(payload["exit"], 30)
        self.assertEqual(payload["availability"], "pre_window")
        self.assertIn("availability_note", payload)

    def test_download_bad_attachment_has_availability(self):
        rec = {
            "code": "DG/TJ08-1-2020",
            "name": "甲",
            "published_on": "2020-01-01",
            "download_url": "https://x/a",
            "page_url": "p",
            "status": "current",
        }
        exc = self._raise_api(
            self._source([rec], download=BadAttachment("上海接口返回的不是 PDF"))
        )
        payload = exc.payload()
        self.assertEqual(payload["exit"], 30)
        self.assertEqual(payload["availability"], "in_window")


class _FakeApiSource:
    id = "fakeapi"
    display_name = "测试地标源"
    api_mode = True

    def __init__(self, rec):
        self.rec = rec
        self.downloaded = []

    def find(self, ref):
        return [self.rec]

    def download_to(self, url, part_path, referer=None, probe_sink=None):
        Path(part_path).parent.mkdir(parents=True, exist_ok=True)
        Path(part_path).write_bytes(PDF)
        self.downloaded.append(url)
        return len(PDF), "pdf", 0


def _api_rec(**overrides):
    rec = {
        "code": "DG/TJ08-2007-2026",
        "name": "地质灾害危险性评估技术标准",
        "published_on": "2026-08-10",
        "implement_on": "2027-02-01",
        "abolish_on": None,
        "download_url": "https://example.invalid/api/files/webDownload?fileId=fid-1",
        "page_url": "https://example.invalid/pc/?keyword=DG%2FTJ08-2007-2026",
        "status": "current",
    }
    rec.update(overrides)
    return rec


class PullApiCiteRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(render.configure, as_json=False, quiet=False)
        self.addCleanup(render.set_catalog, None)
        self.home = Path(self.tmp.name)
        self.db = self.home / "catalog.db"
        self.out = self.home / "library"

    def _run_cite(self):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(
                "os.environ",
                {"GUIJU_NO_AUTO_SYNC": "1", "GUIJU_HOME": str(self.home),
                 "GUIJU_CURRENCY": "off"},
            ),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = main(
                ["cite", "DG/TJ08-2007-2026", "5.2.1", "--offline", "--json",
                 "--db", str(self.db)]
            )
        return rc, out.getvalue(), err.getvalue()

    def test_first_pull_writes_mention_and_cite_offline_works(self):
        rec = _api_rec()
        source = _FakeApiSource(rec)
        store = Store(self.db)
        try:
            with mock.patch.object(store, "commit", wraps=store.commit) as commit:
                payload = _pull_api(store, _args(force=True), "DG/TJ08-2007-2026", source, self.out)
                self.assertGreaterEqual(commit.call_count, 1)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["exit"], 0)
            ann = store.get_ann_by_url(rec["page_url"])
            self.assertIsNotNone(ann)
            mentions = store.mentions_for(int(ann["id"]))
            self.assertEqual(len(mentions), 1)
            self.assertEqual(mentions[0]["code_key"], normalize_key(rec["code"]))
            self.assertEqual(mentions[0]["role"], "primary")
            self.assertEqual(mentions[0]["confidence"], "parsed")
            self.assertEqual(mentions[0]["status"], "current")
        finally:
            store.close()

        rc, stdout, stderr = self._run_cite()
        self.assertEqual(rc, 0, stderr)
        cited = json.loads(stdout)
        self.assertEqual(cited["code"], "DG/TJ08-2007-2026")
        self.assertIn("第5.2.1条", cited["cite"])

    def test_cached_exit10_backfills_mention_for_old_download(self):
        rec = _api_rec()
        source = _FakeApiSource(rec)
        # 模拟 1.2.0a3 已下载的文件：目录里有文件，但库里没有任何公告/mention。
        self.out.mkdir(parents=True, exist_ok=True)
        code_key = normalize_key(rec["code"])
        digest = sha256_bytes(PDF)
        cached_path = self.out / naming.library_name(code_key, rec["name"], digest)
        cached_path.write_bytes(PDF)

        store = Store(self.db)
        try:
            self.assertEqual(store.count_announcements(), 0)
            with mock.patch.object(store, "commit", wraps=store.commit) as commit:
                payload = _pull_api(
                    store, _args(force=False), "DG/TJ08-2007-2026", source, self.out
                )
                self.assertGreaterEqual(commit.call_count, 1)
            self.assertEqual(payload["exit"], 10)
            self.assertTrue(payload["already"])
            for key in ("name", "status", "availability", "source_page", "published_on"):
                self.assertIn(key, payload, key)
            self.assertEqual(payload["availability"], "in_window")
            self.assertEqual(source.downloaded, [])  # 没有重下
            ann = store.get_ann_by_url(rec["page_url"])
            self.assertIsNotNone(ann)
            mentions = store.mentions_for(int(ann["id"]))
            self.assertEqual([m["code_key"] for m in mentions], [code_key])
            self.assertEqual(mentions[0]["status"], "current")
        finally:
            store.close()

        rc, stdout, stderr = self._run_cite()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(json.loads(stdout)["code"], "DG/TJ08-2007-2026")

    def test_withdrawn_status_is_persisted(self):
        rec = _api_rec(status="withdrawn")
        source = _FakeApiSource(rec)
        store = Store(self.db)
        try:
            _pull_api(store, _args(force=True), "DG/TJ08-2007-2026", source, self.out)
            row = store.find_by_code(normalize_key(rec["code"]))
            self.assertEqual(row[0]["status"], "withdrawn")
        finally:
            store.close()


class PullOneCachedShapeTest(unittest.TestCase):
    def test_cached_10_keeps_success_metadata_and_finalize_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            digest = sha256_bytes(PDF)
            cached = out / naming.library_name(None, ANN["doc_name"], digest, ANN["id"])
            cached.write_bytes(PDF)
            source = mock.Mock()
            source.api_mode = False
            with mock.patch.object(pull, "resolve", return_value=_target()):
                payload = _pull_one(mock.Mock(), _args(force=False), "某标准", source, out)

            source.detail.return_value = ("<html></html>", "发布 GB 1-2020")
            source.attachments.return_value = [
                att_domain.Attachment(url="https://x/a.pdf", label="某标准.pdf", kind="pdf")
            ]
            choice = att_domain.Attachment(url="https://x/a.pdf", label="某标准.pdf", kind="pdf")
            pick = att_domain.Pick(
                att_domain.Verdict.OK, choice=choice, candidates=(choice,), reason="唯一附件"
            )
            codes = Codes(
                mentions=(Mention("GB1-2020", "GB 1-2020", Role.PRIMARY, "发布GB 1-2020"),),
                primary_key="GB1-2020",
                primary_display="GB 1-2020",
                confidence=Confidence.PARSED,
            )

            def _download(url, part, referer=None, probe_sink=None):
                Path(part).parent.mkdir(parents=True, exist_ok=True)
                Path(part).write_bytes(PDF)
                if probe_sink is not None:
                    probe_sink(ProbeRecord(url=url, status=200, bytes=len(PDF), outcome="ok"))
                return len(PDF), "pdf", 0

            source.download_to.side_effect = _download
            store = mock.Mock()
            with (
                mock.patch.object(pull, "resolve", return_value=_target()),
                mock.patch.object(pull, "classify", return_value=codes),
                mock.patch.object(pull.att_domain, "pick", return_value=pick),
                mock.patch.object(pull, "find_cached", return_value=None),
                mock.patch.object(pull, "persist_codes"),
            ):
                payload2 = _pull_one(store, _args(force=True), "某标准", source, out)
            self.assertEqual(payload2["exit"], 0)
            store.log_probe.assert_any_call(source.id, "https://x/a.pdf", 200, len(PDF), "ok")
            self.assertEqual(store.log_probe.call_count, 1)
            self.assertTrue(Path(payload2["path"]).exists())
            self.assertEqual(Path(payload2["path"]).read_bytes(), PDF)
            self.assertFalse((out / ".partial").exists() and any((out / ".partial").iterdir()))

            from guiju.application.pull import _probe_sink

            clean_store = mock.Mock()
            sink = _probe_sink(clean_store, "shzjw", url="https://clean/dl")
            sink(ProbeRecord(url="https://clean/dl?receivedToken=tmp", status=200, bytes=len(PDF), outcome="ok"))
            self.assertTrue(sink.ok_emitted)
            clean_store.log_probe.assert_called_once_with("shzjw", "https://clean/dl", 200, len(PDF), "ok")

        self.assertEqual(payload["exit"], 10)
        self.assertTrue(payload["already"])
        for key in (
            "name", "status", "availability", "source_page", "published_on",
            "magic", "confidence",
        ):
            self.assertIn(key, payload, key)
        self.assertEqual(payload["availability"], "in_window")


class UnknownSourceTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(render.configure, as_json=False, quiet=False)
        self.addCleanup(render.set_catalog, None)

    def test_unknown_source_is_bad_usage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": "1",
                                            "GUIJU_HOME": tmp.name}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = main(
                ["pull", "--source", "nope", "GB 50037-2013", "--json",
                 "--db", str(Path(tmp.name) / "c.db")]
            )
        self.assertEqual(rc, 2, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "bad_usage")


if __name__ == "__main__":
    unittest.main()
