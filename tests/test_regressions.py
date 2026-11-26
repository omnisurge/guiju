"""高危 bug 与杂项小修定向回归 —— 覆盖退出码、hint、naming、CLI 入口等跨模块保护。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from guiju import files
from guiju import report as render
from guiju.cli import main
from guiju.domain import naming
from guiju.domain.codes import classify
from guiju.domain.search import _year_of
from guiju.errors import BadUsage, NeedsHuman, NotIndexed
from guiju.store.sqlite import Store, persist_codes


class YearOfTest(unittest.TestCase):
    """published_on='待定' 曾让排序键抛 ValueError（internal 1）。"""

    def test_non_numeric_year_is_zero(self):
        self.assertEqual(_year_of("待定"), 0)
        self.assertEqual(_year_of(None), 0)
        self.assertEqual(_year_of(""), 0)
        self.assertEqual(_year_of("2026-10-31"), 2026)


class RefIsCodeTest(unittest.TestCase):
    """which/resolve 入口分流：名称形态必须先走本地检索，不能去问时效服务。"""

    def test_cjk_name_is_not_code(self):
        from guiju.application.resolve import ref_is_code

        self.assertFalse(ref_is_code("绿色建筑评价标准"))
        self.assertFalse(ref_is_code("《绿色建筑评价标准》"))
        self.assertTrue(ref_is_code("GB/T 50378-2019"))
        self.assertFalse(ref_is_code("12345"))
        self.assertFalse(ref_is_code("https://www.mohurd.gov.cn/x.html"))
        self.assertFalse(ref_is_code(""))


class ResolveExitTest(unittest.TestCase):
    """裸 GuijuError 让「没收录」变成 internal(1)：分流到对应退出码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)

    def test_empty_ref_is_bad_usage(self):
        from guiju.application.resolve import resolve

        with self.assertRaises(BadUsage):
            resolve(self.store, "  ")

    def test_unknown_url_is_not_indexed(self):
        from guiju.application.resolve import resolve

        with self.assertRaises(NotIndexed):
            resolve(self.store, "https://www.mohurd.gov.cn/not-in-db.html")

    def test_unknown_ann_id_is_not_indexed(self):
        from guiju.application.resolve import resolve

        with self.assertRaises(NotIndexed):
            resolve(self.store, "999999")


class PersistCodesTest(unittest.TestCase):
    """ambiguous 兜底 primary 没资格把同族编号写成 superseded_ref（之前一律改写）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/x.html", "2021-07-15", "标题", "建筑拆除工程安全技术规范", "national"
        )

    def test_ambiguous_primary_does_not_demote(self):
        codes = classify("《建筑拆除工程安全技术规范》JGJ147-2016与JGJ147-2004均已列入目录。")
        persist_codes(self.store, 1, codes, "建筑拆除工程安全技术规范")
        roles = {r["code_key"]: r["role"] for r in self.store.mentions_for(1)}
        self.assertEqual(roles["JGJ147-2004"], "mentioned")
        self.assertEqual(roles["JGJ147-2016"], "mentioned")

    def test_decisive_primary_still_demotes(self):
        codes = classify("现批准《住宅性能评定标准》为国家标准，编号为GB/T50362-2022，自2023年5月1日起实施。原《住宅性能评定标准》GB/T50362-2005同时废止。")
        persist_codes(self.store, 1, codes, "住宅性能评定标准")
        roles = {r["code_key"]: r["role"] for r in self.store.mentions_for(1)}
        self.assertEqual(roles["GB/T50362-2005"], "superseded_ref")
        self.assertEqual(roles["GB/T50362-2022"], "primary")


class NeedsHumanHintTest(unittest.TestCase):
    def test_needs_human_hint_not_mohurd_only(self) -> None:
        err = NeedsHuman("boom")
        self.assertTrue("mohurd.py" not in err.hint or "shzjw.py" in err.hint)
        self.assertIn("sources/", err.hint)


class MohurdDeduplicateTest(unittest.TestCase):
    def test_mohurd_uses_domain_classify_title(self) -> None:
        from guiju.domain import announcement
        from guiju.sources import mohurd

        self.assertIs(mohurd.classify_title, announcement.classify_title)
        self.assertFalse(hasattr(mohurd, "_CATEGORY_RULES"))
        self.assertFalse(hasattr(mohurd, "_NAME_RE"))


class UntitledStemTest(unittest.TestCase):
    def test_untitled_stem_includes_ann_id(self) -> None:
        self.assertNotEqual(naming.library_stem(None, "", 1), naming.library_stem(None, "", 2))
        self.assertEqual(naming.library_stem(None, ""), "untitled")  # 无 ann_id 时维持旧名

    def test_find_cached_untitled_uses_ann_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            content = b"pdf-body-for-ann-7"
            digest = files.sha256_bytes(content)
            name = naming.library_name(None, "", digest, ann_id=7)
            (tmp_path / name).write_bytes(content)
            # 正确 ann_id 命中；别的 ann_id / 裸 untitled 都不得误中
            self.assertIsNotNone(files.find_cached(tmp_path, None, "", ann_id=7))
            self.assertIsNone(files.find_cached(tmp_path, None, "", ann_id=8))
            self.assertIsNone(files.find_cached(tmp_path, None, ""))


class CliNoCommandJsonTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(render.configure, as_json=False, quiet=False)
        self.addCleanup(render.set_catalog, None)

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


if __name__ == "__main__":
    unittest.main()
