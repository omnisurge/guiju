"""批量清单解析（行尾注释/BOM）、40 合规熔断、失败移交字段与数据库异常映射测试。"""

from __future__ import annotations

import argparse
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.application import AppContext, pull
from guiju.application.pull import _batch, _read_refs
from guiju.errors import (
    AmbiguousAttachment,
    AmbiguousCode,
    BadAttachment,
    ComplianceDenied,
    NeedsHuman,
    SourceUnreachable,
)

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


class ReadRefsTest(unittest.TestCase):
    def _file(self, text: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "list.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_strips_comments_and_blanks(self):
        path = self._file("# 头部注释\nGB 55001-2021\n\nGB 55002-2021  # 抗震\n")
        self.assertEqual(_read_refs(path), ["GB 55001-2021", "GB 55002-2021"])

    def test_inline_comment_with_names(self):
        path = self._file("GB 55037-2022    # 建筑防火通用规范\n")
        self.assertEqual(_read_refs(path), ["GB 55037-2022"])

    def test_keeps_names_with_book_marks(self):
        path = self._file("《建筑防火通用规范》\n")
        self.assertEqual(_read_refs(path), ["《建筑防火通用规范》"])

    def test_stdin(self):
        old = sys.stdin
        sys.stdin = io.StringIO("GB 55001-2021\n# skip\n")
        try:
            self.assertEqual(_read_refs("-"), ["GB 55001-2021"])
        finally:
            sys.stdin = old


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


class BatchStopTest(unittest.TestCase):
    def _run(self, side_effect):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        refs = Path(tmp.name) / "refs.txt"
        refs.write_text("A-1\nB-2\nC-3\n", encoding="utf-8")
        args = argparse.Namespace(list=str(refs), out=tmp.name, readable=False)
        with mock.patch.object(pull, "_pull_one", side_effect=side_effect) as one, \
                mock.patch.object(pull.render, "emit") as emit, \
                mock.patch.object(pull.render, "note"), \
                mock.patch.object(pull.render, "is_json", return_value=True):
            code = pull._batch(mock.Mock(), args, mock.Mock())
        payload = emit.call_args.args[0]
        return code, one.call_args_list, payload

    def test_compliance_error_stops_immediately(self):
        calls = []

        def side_effect(store, args, ref, source, out_dir):
            calls.append(ref)
            if ref == "B-2":
                raise ComplianceDenied("blocked")
            return {"exit": 0, "code": ref}

        code, one_calls, payload = self._run(side_effect)
        self.assertEqual(code, 40)
        self.assertEqual(calls, ["A-1", "B-2"])
        self.assertEqual(len(one_calls), 2)
        self.assertEqual(payload["done"], 2)
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["stopped"], "compliance")

    def test_compliance_result_dict_stops_immediately(self):
        calls = []

        def side_effect(store, args, ref, source, out_dir):
            calls.append(ref)
            return {"exit": 40, "code": ref} if ref == "B-2" else {"exit": 0, "code": ref}

        code, one_calls, payload = self._run(side_effect)
        self.assertEqual(code, 40)
        self.assertEqual(calls, ["A-1", "B-2"])
        self.assertEqual(len(one_calls), 2)
        self.assertEqual(payload["stopped"], "compliance")
        self.assertEqual(payload["results"][1]["outcome"], "compliance")

    def test_earlier_fatal_codes_do_not_mask_40(self):
        cases = [
            (SourceUnreachable("unreachable"), 20),
            (NeedsHuman("needs human"), 50),
            (OSError("disk full"), 1),
        ]
        for first, expected in cases:
            with self.subTest(first=type(first).__name__):
                calls = []

                def side_effect(store, args, ref, source, out_dir,
                                _calls=calls, _first=first):
                    _calls.append(ref)
                    if ref == "A-1":
                        raise _first
                    if ref == "B-2":
                        raise ComplianceDenied("blocked")
                    return {"exit": 0, "code": ref}

                code, one_calls, payload = self._run(side_effect)
                self.assertEqual(code, 40)
                self.assertEqual(calls, ["A-1", "B-2"])
                self.assertEqual(len(one_calls), 2)
                self.assertEqual(payload["counts"][str(expected)], 1)
                self.assertEqual(payload["counts"]["40"], 1)
                self.assertEqual(payload["stopped"], "compliance")

    def test_non_40_fatal_code_keeps_batch_going(self):
        calls = []

        def side_effect(store, args, ref, source, out_dir):
            calls.append(ref)
            if ref == "A-1":
                raise SourceUnreachable("unreachable")
            return {"exit": 0, "code": ref}

        code, one_calls, payload = self._run(side_effect)
        self.assertEqual(code, 20)
        self.assertEqual(calls, ["A-1", "B-2", "C-3"])
        self.assertEqual(len(one_calls), 3)
        self.assertIsNone(payload["stopped"])


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


if __name__ == "__main__":
    unittest.main()
