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


class StatusDimensionTest(unittest.TestCase):
    """站点自己分「现行/废止 × 标准/设计标准」，我们照抄，不猜。"""

    def test_abolish_time_means_withdrawn(self):
        rec = _record(
            {
                "standardNumber": "DG/TJ08-2021-2007",
                "standardProjectName": "商品砌筑砂浆现场检测技术规程",
                "releaseTime": "2007-01-01 00:00:00",
                "abolishTime": "2026-01-01 00:00:00",
                "standardFileId": "x",
            }
        )
        self.assertEqual(rec["status"], "withdrawn")

    def test_standard_type_1_is_design(self):
        rec = _record(
            {"standardNumber": "DBJT08-144-2026", "standardProjectName": "外墙内保温系统建筑构造",
             "standardType": 1, "standardFileId": "y"}
        )
        self.assertEqual(rec["kind"], "design")

    def test_find_prefers_current_over_withdrawn(self):
        current = _record({"standardNumber": "DG/TJ08-1-2026", "standardProjectName": "甲",
                           "standardFileId": "c"})
        old = _record({"standardNumber": "DG/TJ08-1-1999", "standardProjectName": "甲",
                       "abolishTime": "2026-01-01 00:00:00", "standardFileId": "o"})

        def fake_search(ref, *, abolished=False, standard_type=0, page_size=30):
            return [old] if abolished else [current]

        with mock.patch.object(shzjw, "search", side_effect=fake_search):
            hits = shzjw.find("甲")
        self.assertEqual(hits[0]["status"], "current")

    def test_find_falls_back_to_withdrawn(self):
        old = _record({"standardNumber": "DG/TJ08-9-1999", "standardProjectName": "乙",
                       "abolishTime": "2020-01-01 00:00:00", "standardFileId": "o"})

        def fake_search(ref, *, abolished=False, standard_type=0, page_size=30):
            return [old] if abolished else []

        with mock.patch.object(shzjw, "search", side_effect=fake_search):
            hits = shzjw.find("乙")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["status"], "withdrawn")


class LocalStatusTest(unittest.TestCase):
    def test_recognises_local_codes(self):
        from guiju import localstatus

        for ref in ("DG/TJ08-2007", "DG/TJ 08-2007-2026", "DBJT08-144", "DB31/T 1010"):
            self.assertTrue(localstatus.is_local_code(ref), ref)
        for ref in ("GB 55037-2022", "JGJ/T450-2018", "建筑防火通用规范"):
            self.assertFalse(localstatus.is_local_code(ref), ref)

    def test_records_map_to_currency_shape(self):
        from guiju import localstatus

        hits = [
            {"code": "DG/TJ08-2007-2026", "name": "地质灾害危险性评估技术标准",
             "status": "current", "implement_on": "2027-02-01"},
        ]
        with mock.patch.object(localstatus.shzjw, "find", return_value=hits):
            recs = localstatus.records("DG/TJ08-2007")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].status, "current")
        self.assertEqual(recs[0].source, "shzjw")
        self.assertEqual(recs[0].implement_date, "2027-02-01")


class WhichFallbackTest(unittest.TestCase):
    """时效服务没记录时，地方标准的现行性由源站回答（which 的 currency_source 可证）。"""

    def test_which_uses_shzjw_when_currency_has_nothing(self):
        from guiju import currency as currency_mod

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        buf = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch.object(currency_mod.CachedCurrency, "lookup", return_value=[]),
            mock.patch("guiju.localstatus.records",
                       return_value=[currency_mod.Record(code="DG/TJ08-2007-2026", status="current",
                                                        name="地质灾害危险性评估技术标准",
                                                        source="shzjw")]),
            redirect_stdout(buf),
        ):
            code = main(["which", "DG/TJ08-2007", "--db", str(Path(tmp.name) / "c.db"), "--json"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["currency_source"], "shzjw")
        self.assertEqual(payload["currency"][0]["status"], "current")


if __name__ == "__main__":
    unittest.main()
