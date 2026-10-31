"""上海地标源：API 检索 + 直连下载（不打公告页），以及 pull 的 API 模式分支。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from guiju.cli import main
from guiju.sources import shzjw
from guiju.sources.shzjw import _record, file_id_of

PDF = b"%PDF-1.7\n" + b"x" * 500


class RecordTest(unittest.TestCase):
    def test_record_normalizes_fields(self):
        row = {
            "standardNumber": "DG/TJ08-2007-2026",
            "standardProjectName": "地质灾害危险性评估技术标准",
            "releaseTime": "2026-08-10 00:00:00",
            "implementationTime": "2027-02-01 00:00:00",
            "standardFileId": "abc-123",
        }
        rec = _record(row)
        self.assertEqual(rec["code_key"], "DG/TJ08-2007-2026")
        self.assertEqual(rec["published_on"], "2026-08-10")
        self.assertEqual(rec["implement_on"], "2027-02-01")
        self.assertIn("fileId=abc-123", rec["download_url"])
        self.assertIn("keyword=", rec["page_url"])

    def test_file_id_parsed_from_url(self):
        self.assertEqual(file_id_of("https://x/api/files/webDownload?fileId=abc-123"), "abc-123")

    def test_find_prefers_exact_code_over_fuzzy(self):
        rows = [
            _record({"standardNumber": "DG/TJ08-1-2020", "standardProjectName": "甲"}),
            _record({"standardNumber": "DG/TJ08-2007-2026", "standardProjectName": "乙"}),
        ]
        with mock.patch.object(shzjw, "search", return_value=rows):
            hits = shzjw.find("DG/TJ08-2007-2026")
        self.assertEqual([h["code"] for h in hits], ["DG/TJ08-2007-2026"])


class PullApiModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def _run(self, *argv):
        # 关掉进程内目录自保鲜：这里测的是 pull 的 API 分支，不该顺带去抓住建部目录
        buf = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": "1"}),
            redirect_stdout(buf),
        ):
            code = main([*argv, "--json"])
        return code, (json.loads(buf.getvalue()) if buf.getvalue().strip() else {})

    def _fake_download(self, part_path):
        def download_to(_url, path, referer=None):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(PDF)
            return len(PDF), "pdf", 0

        return download_to

    def test_single_hit_downloads_and_archives(self):
        rec = _record(
            {
                "standardNumber": "DG/TJ08-2007-2026",
                "standardProjectName": "地质灾害危险性评估技术标准",
                "releaseTime": "2026-08-10 00:00:00",
                "standardFileId": "fid-1",
            }
        )
        with (
            mock.patch.object(shzjw.SOURCE, "find", return_value=[rec]),
            mock.patch.object(shzjw.SOURCE, "download_to", side_effect=self._fake_download(None)),
        ):
            code, payload = self._run("pull", "--source", "shzjw", "DG/TJ08-2007", "--db",
                                     str(self.home / "catalog.db"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["bytes"], len(PDF))
        self.assertEqual(payload["magic"], "pdf")
        self.assertTrue(Path(payload["path"]).exists())
        self.assertEqual(payload["source"], "shzjw")

        # 幂等：第二条命令不再下载
        with (
            mock.patch.object(shzjw.SOURCE, "find", return_value=[rec]),
            mock.patch.object(shzjw.SOURCE, "download_to", side_effect=AssertionError("不该重下")),
        ):
            code2, payload2 = self._run("pull", "--source", "shzjw", "DG/TJ08-2007",
                                        "--db", str(self.home / "catalog.db"))
        self.assertEqual(code2, 10)
        self.assertTrue(payload2.get("already"))

    def test_no_hit_is_31(self):
        with mock.patch.object(shzjw.SOURCE, "find", return_value=[]):
            code, payload = self._run("pull", "--source", "shzjw", "DG/TJ08-9999",
                                      "--db", str(self.home / "catalog.db"))
        self.assertEqual(code, 31)
        self.assertEqual(payload["reason"], "not_indexed")

    def test_multiple_hits_is_32_with_candidates(self):
        rows = [
            _record({"standardNumber": "DG/TJ08-1-2020", "standardProjectName": "甲",
                     "standardFileId": "a"}),
            _record({"standardNumber": "DG/TJ08-2-2020", "standardProjectName": "乙",
                     "standardFileId": "b"}),
        ]
        with mock.patch.object(shzjw.SOURCE, "find", return_value=rows):
            code, payload = self._run("pull", "--source", "shzjw", "DG/TJ08",
                                      "--db", str(self.home / "catalog.db"))
        self.assertEqual(code, 32)
        self.assertEqual(len(payload["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
