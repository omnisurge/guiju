"""M15 回归：pull.py 的 confidence 口径、# 注释、批内兜底。

- confidence：正文解析成 parsed 时覆盖 target 先验值（编号直查给的 unknown）。
- --list 注释：行首 # 与 " #" 行尾注释剥离，URL 里的 #frag 不截断。
- _batch：单条 ref 抛非 GuijuError 不中断整批，该条记 exit=1/internal。
"""

from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.cli.commands.pull import _batch, _pull_one, _read_refs
from guiju.domain.codes import Codes, Confidence, Mention, Role
from guiju.errors import EXIT_OK


class ReadRefsCommentTest(unittest.TestCase):
    def _refs(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return _read_refs(path)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_url_fragment_not_truncated(self):
        refs = self._refs("https://mohurd.gov.cn/x/1#frag" + chr(10) + "GB 50037" + chr(10))
        self.assertEqual(refs, ["https://mohurd.gov.cn/x/1#frag", "GB 50037"])

    def test_trailing_comment_needs_space_before_hash(self):
        text = "GB 50037 # 国标" + chr(10) + "# 整行注释" + chr(10) + "GB 50038" + chr(10)
        refs = self._refs(text)
        self.assertEqual(refs, ["GB 50037", "GB 50038"])


class ConfidenceReportedFromParseTest(unittest.TestCase):
    """target 没先验 confidence 时，正文解析成 parsed 要覆盖 unknown。"""

    def test_parsed_result_overrides_unknown(self):
        ann = {"id": 1, "url": "https://www.mohurd.gov.cn/x/1",
               "doc_name": "某标准", "title": "t", "category": "",
               "published_on": "2026-01-01"}
        # confidence=""（编号直查没给先验）——M15：解析成 parsed 要覆盖
        target = {"ann": ann, "code_display": "", "status": "", "confidence": ""}
        parsed = Codes(
            mentions=(Mention("GB1-2020", "GB 1-2020", Role.PRIMARY, ""),),
            primary_key="GB1-2020", primary_display="GB 1-2020",
            confidence=Confidence.PARSED,
        )
        source = mock.Mock()
        source.api_mode = False
        source.detail.return_value = ("<html></html>", "正文文本")
        source.attachments.return_value = ["att"]
        source.download_to.return_value = (5, "pdf", None)
        choice = mock.Mock(url="https://u/x.pdf", label="正文")
        pick = mock.Mock(ok=True, choice=choice)
        store = mock.Mock()
        args = argparse.Namespace(force=True, readable=False, out=None)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("guiju.cli.commands.pull.resolve", return_value=target),
            mock.patch("guiju.cli.commands.pull.classify", return_value=parsed),
            mock.patch("guiju.cli.commands.pull.att_domain.pick", return_value=pick),
            mock.patch("guiju.cli.commands.pull.find_cached", return_value=None),
            mock.patch("guiju.cli.commands.pull.sha256_file", return_value="d"),
            mock.patch("guiju.cli.commands.pull.sha256_bytes", return_value="k"),
            mock.patch("guiju.cli.commands.pull.naming.library_name", return_value="f.pdf"),
            mock.patch("os.replace"),
        ):
            result = _pull_one(store, args, "GB 1", source, Path(tmp))
        self.assertTrue(result["ok"])
        # M15：正文解析 parsed → 报 parsed（不再是 unknown）
        self.assertEqual(result["confidence"], "parsed")


class BatchNonGuijuErrorTest(unittest.TestCase):
    """单条 ref 抛非 GuijuError（如 OSError）不中断整批。"""

    def test_batch_continues_and_marks_internal(self):
        store = mock.Mock()
        source = mock.Mock()
        args = argparse.Namespace(list="-", force=False, readable=False, out=None)
        refs = ["GB ok", "GB boom", "GB also-ok"]

        def fake_pull(store, args, ref, source, out_dir):
            if ref == "GB boom":
                raise OSError("disk full")
            return {"ok": True, "exit": EXIT_OK, "code": ref, "path": "/p", "bytes": 1}

        with (
            mock.patch("guiju.cli.commands.pull._read_refs", return_value=refs),
            mock.patch("guiju.cli.commands.pull._pull_one", side_effect=fake_pull),
        ):
            rc = _batch(store, args, source)
        # 整批跑完 3 条，boom 那条记 internal/exit1 → severe → rc=1
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
