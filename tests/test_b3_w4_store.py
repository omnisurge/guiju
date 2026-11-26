"""B3 W4：原子 upsert、LIKE 转义与数据库错误映射。"""

import io
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from guiju import cli
from guiju.domain.search import rank, tokens_of
from guiju.store.sqlite import Store


class StoreW4Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "catalog.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def test_concurrent_same_url_upsert_is_atomic(self):
        errors = []
        barrier = threading.Barrier(4)

        def worker(n):
            try:
                store = Store(self.path)
                try:
                    for round_no in range(20):
                        barrier.wait()
                        store.upsert_announcement(
                            "https://x/same",
                            "2026-01-01",
                            f"标题{n}-{round_no}",
                            "名称",
                            "other",
                        )
                        store.commit()
                finally:
                    store.close()
            except Exception as exc:  # 捕捉以便断言零异常
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.store.count_announcements(), 1)

    def test_upsert_keeps_id_and_first_fetched_at(self):
        ann_id, fresh = self.store.upsert_announcement(
            "https://x/1", "2020-01-01", "旧", "旧", "other"
        )
        self.assertTrue(fresh)
        fetched = self.store.get_ann(ann_id)["fetched_at"]
        same, fresh2 = self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "新", "新", "standard"
        )
        self.assertEqual(same, ann_id)
        self.assertFalse(fresh2)
        self.assertEqual(self.store.get_ann(ann_id)["fetched_at"], fetched)
        self.assertEqual(self.store.get_ann(ann_id)["title"], "新")

    def test_find_by_code_wildcard_is_literal(self):
        ann_id, _ = self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "标题", "名称", "standard"
        )
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(ann_id, "GB50016-2014", "primary", "发布")
        self.store.commit()
        self.assertEqual(self.store.find_by_code("GB%"), [])
        self.assertEqual(self.store.find_by_code("GB50016_2014"), [])
        self.assertEqual(len(self.store.find_by_code("GB50016")), 1)

    def test_search_wildcard_is_literal(self):
        self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "防火规范", "建筑设计防火规范", "standard"
        )
        self.store.commit()
        self.assertFalse(hasattr(self.store, "search"))
        entries = self.store.catalog_entries()
        self.assertEqual(rank(entries, tokens_of("%"))[0], [])
        self.assertEqual(len(rank(entries, tokens_of("防火"))[0]), 1)


class CliW4Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "catalog.db"
        Store(self.path).close()

    def _main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_unknown_schema_is_needs_human_50(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA user_version = 999")
        db.commit()
        db.close()
        rc, out = self._main(["--json", "--quiet", "--db", str(self.path), "status"])
        self.assertEqual(rc, 50)
        payload = json.loads(out)
        self.assertEqual(payload["exit"], 50)
        self.assertEqual(payload["reason"], "needs_human")
        self.assertIn("schema", payload["error"])

    def test_integrity_error_maps_to_50(self):
        with mock.patch(
            "guiju.cli.sync.run", side_effect=sqlite3.IntegrityError("boom")
        ):
            rc, out = self._main(["--json", "--quiet", "--db", str(self.path), "sync"])
        self.assertEqual(rc, 50)
        payload = json.loads(out)
        self.assertEqual(payload["reason"], "needs_human")
        self.assertEqual(payload["exit"], 50)

    def test_locked_error_maps_to_20(self):
        with mock.patch(
            "guiju.cli.sync.run", side_effect=sqlite3.OperationalError("database is locked")
        ):
            rc, out = self._main(["--json", "--quiet", "--db", str(self.path), "sync"])
        self.assertEqual(rc, 20)
        payload = json.loads(out)
        self.assertEqual(payload["reason"], "unreachable")
        self.assertEqual(payload["exit"], 20)


if __name__ == "__main__":
    unittest.main()
