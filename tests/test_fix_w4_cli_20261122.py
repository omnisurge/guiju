"""W4 CLI/pull 发版修复回归（1.2.0a4 / task-57）。

每条测试对应计划 W4-1..W4-5，先复现原缺陷：
- W4-1 批量失败体丢 candidates/availability/...；成功项丢 name/status/availability/source_page。
- W4-2 单条 pull 的 30/32/33/resolve 早期失败没有 availability/availability_note。
- W4-3 批量 SQLite locked/corrupt 被兜成 exit 1/internal，而不是 20/50 + stopped=database。
- W4-4 shzjw（api_mode）pull 不落 announcement/mention，已缓存 10 路径也不补；
  cite --offline 因此不可用。
- W4-5 未知 --source、清单 BOM、--json 无子命令、exit 10 形状。
"""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from guiju.application import AppContext, pull
from guiju.application.pull import (
    _batch,
    _codes_for_book,
    _pull_api,
    _pull_one,
    _read_refs,
)
from guiju.cli import main
from guiju.domain import attachment as att_domain
from guiju.domain import availability, naming
from guiju.domain.codes import Codes, Confidence, Mention, Role, normalize_key
from guiju.errors import (
    AmbiguousAttachment,
    AmbiguousCode,
    BadAttachment,
    GuijuError,
    NotIndexed,
)
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


# --------------------------------------------------------------------- W4-1


class BatchFailureHandoffTest(unittest.TestCase):
    def _run_batch(self, side_effect):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        args = _args(list="-", out=tmp.name)
        with (
            mock.patch.object(pull, "_read_refs", return_value=["A-1", "B-2"]),
            mock.patch.object(pull, "_pull_one", side_effect=side_effect),
            mock.patch.object(pull.render, "emit") as emit,
            mock.patch.object(pull.render, "note"),
            mock.patch.object(pull.render, "is_json", return_value=True),
        ):
            rc = _batch(mock.Mock(), args, mock.Mock())
        return rc, emit.call_args.args[0]

    def test_ambiguous_errors_keep_all_handoff_fields(self):
        def side_effect(store, args, ref, source, out_dir):
            if ref == "A-1":
                raise AmbiguousCode(
                    "多个编号候选",
                    hint="请人工选定",
                    candidates=[{"code": "GB 1-2020", "key": "GB1-2020"}],
                    availability="in_window",
                    availability_note="2026 年公告在正文可得窗口内",
                    page_url="https://www.mohurd.gov.cn/x/1",
                    verdict="ambiguous",
                    names=["甲标准"],
                    pick_reason="正文有多个候选",
                )
            raise AmbiguousAttachment(
                "多个附件候选",
                hint="请人工选定附件",
                candidates=[{"label": "甲.pdf", "url": "https://x/a.pdf"}],
                attachments=[{"label": "甲.pdf", "url": "https://x/a.pdf"}],
                availability="pre_window",
                availability_note="2010 年公告通常没有正文",
                page_url="https://www.mohurd.gov.cn/x/2",
                verdict="ambiguous",
                names=["乙标准"],
                pick_reason="附件名无法锁定",
            )

        rc, payload = self._run_batch(side_effect)
        # 32/33 不属于 _SEVERE（1/20/40/50）；批量顶层按既有契约返回 0，逐条看 exit。
        self.assertEqual(rc, 0)
        first, second = payload["results"]
        self.assertEqual(first["exit"], 32)
        self.assertEqual(second["exit"], 33)
        for key in (
            "error",
            "hint",
            "candidates",
            "availability",
            "availability_note",
            "page_url",
            "verdict",
            "names",
            "pick_reason",
        ):
            self.assertIn(key, first, key)
            self.assertIn(key, second, key)
        self.assertEqual(first["availability"], "in_window")
        self.assertEqual(second["availability"], "pre_window")
        self.assertEqual(second["attachments"], [{"label": "甲.pdf", "url": "https://x/a.pdf"}])
        # 失败体不得携带成功字段
        self.assertNotIn("path", first)
        self.assertNotIn("sha256", first)

    def test_no_attachment_keeps_availability(self):
        def side_effect(store, args, ref, source, out_dir):
            raise BadAttachment(
                "附件不是 PDF",
                availability="pre_window",
                availability_note="2010 年公告通常没有正文",
            )

        rc, payload = self._run_batch(side_effect)
        # 30 不属于 _SEVERE；批量顶层按既有契约返回 0，逐条看 exit。
        self.assertEqual(rc, 0)
        result = payload["results"][0]
        self.assertEqual(result["exit"], 30)
        self.assertEqual(result["availability"], "pre_window")
        self.assertEqual(result["availability_note"], "2010 年公告通常没有正文")

    def test_success_item_keeps_name_status_availability_source_page(self):
        def side_effect(store, args, ref, source, out_dir):
            return {
                "ok": True,
                "exit": 0,
                "code": "GB 1-2020",
                "name": "某标准",
                "status": "current",
                "availability": "in_window",
                "source_page": "https://www.mohurd.gov.cn/x/1",
                "path": "/tmp/x.pdf",
                "bytes": 12,
            }

        rc, payload = self._run_batch(side_effect)
        self.assertEqual(rc, 0)
        result = payload["results"][0]
        for key in ("name", "status", "availability", "source_page"):
            self.assertIn(key, result, key)
        self.assertEqual(result["name"], "某标准")
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["availability"], "in_window")
        self.assertEqual(result["source_page"], "https://www.mohurd.gov.cn/x/1")


# --------------------------------------------------------------------- W4-2


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

    def _call(self, source, ref="DG/TJ08-1-2020"):
        with tempfile.TemporaryDirectory() as tmp:
            return _pull_api(mock.Mock(), _args(), ref, source, Path(tmp))

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


# --------------------------------------------------------------------- W4-3


class BatchDatabaseErrorTest(unittest.TestCase):
    def _run(self, error):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        refs = Path(tmp.name) / "refs.txt"
        refs.write_text("A-1\nB-2\nC-3\n", encoding="utf-8")
        calls = []

        def side_effect(store, args, ref, source, out_dir):
            calls.append(ref)
            if ref == "B-2":
                raise error
            return {"exit": 0, "code": ref, "availability": "in_window"}

        args = _args(list=str(refs), out=tmp.name)
        with (
            mock.patch.object(pull, "_pull_one", side_effect=side_effect),
            mock.patch.object(pull.render, "emit") as emit,
            mock.patch.object(pull.render, "note"),
            mock.patch.object(pull.render, "is_json", return_value=True),
        ):
            rc = _batch(mock.Mock(), args, mock.Mock())
        return rc, calls, emit.call_args.args[0]

    def test_locked_maps_to_20_and_stops_batch(self):
        rc, calls, payload = self._run(sqlite3.OperationalError("database is locked"))
        self.assertEqual(rc, 20)
        self.assertEqual(calls, ["A-1", "B-2"])
        self.assertEqual(payload["stopped"], "database")
        self.assertEqual(payload["results"][-1]["exit"], 20)
        self.assertEqual(payload["results"][-1]["availability"], "unknown")
        self.assertEqual(payload["done"], 2)
        self.assertEqual(payload["total"], 3)

    def test_corrupt_maps_to_50_and_stops_batch(self):
        rc, calls, payload = self._run(sqlite3.DatabaseError("file is not a database"))
        self.assertEqual(rc, 50)
        self.assertEqual(calls, ["A-1", "B-2"])
        self.assertEqual(payload["stopped"], "database")
        self.assertEqual(payload["results"][-1]["exit"], 50)
        self.assertEqual(payload["results"][-1]["availability"], "unknown")

    def test_readonly_maps_to_50(self):
        rc, _calls, payload = self._run(
            sqlite3.OperationalError("attempt to write a readonly database")
        )
        self.assertEqual(rc, 50)
        self.assertEqual(payload["results"][-1]["exit"], 50)


# --------------------------------------------------------------------- W4-4


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
        store.close()

        rc, stdout, stderr = self._run_cite()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(json.loads(stdout)["code"], "DG/TJ08-2007-2026")

    def test_withdrawn_status_is_persisted(self):
        rec = _api_rec(status="withdrawn")
        source = _FakeApiSource(rec)
        store = Store(self.db)
        _pull_api(store, _args(force=True), "DG/TJ08-2007-2026", source, self.out)
        row = store.find_by_code(normalize_key(rec["code"]))
        self.assertEqual(row[0]["status"], "withdrawn")
        store.close()


# --------------------------------------------------------------------- W4-5


class ReadRefsBomTest(unittest.TestCase):
    def test_file_bom_stripped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("\ufeffGB 50037\nGB 50038\n")
            path = f.name
        try:
            self.assertEqual(_read_refs(path), ["GB 50037", "GB 50038"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_stdin_bom_stripped(self):
        with mock.patch("sys.stdin", io.StringIO("\ufeffGB 50037\n")):
            self.assertEqual(_read_refs("-"), ["GB 50037"])


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

            # 真实落盘路径：下载成功后必须经唯一 finalize 点改名，不能再出现
            # `_finalize_body` 未定义/双下划线 helper 的 NameError（rev2 P0）。
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


class CliNoCommandJsonTest(unittest.TestCase):
    def test_json_without_subcommand_is_json(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": "1",
                                            "GUIJU_HOME": tmp.name}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = main(["--json", "--db", str(Path(tmp.name) / "c.db")])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIn("commands", payload)


class UnknownSourceTest(unittest.TestCase):
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
