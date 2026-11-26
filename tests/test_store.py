"""存储层：公告写入与目录新鲜度元数据。"""

import tempfile
import unittest
from pathlib import Path

from guiju.store.sqlite import Store


class CatalogMetaTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "c.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/x.html", "2021-07-15",
            "标题", "工程结构通用规范", "national",
        )

    def test_upsert_announcement_counts(self):
        self.assertEqual(self.store.count_announcements(), 1)

        import sqlite3
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "c.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("CREATE TABLE old_stuff(x TEXT)")
                conn.commit()
            finally:
                conn.close()
            existing_backup = Path(tmp) / "c.db.legacy-2026-11-26-120000"
            existing_backup.write_text("keep-me", encoding="utf-8")
            with mock.patch("guiju.store.sqlite.now", return_value="2026-11-26 12:00:00"):
                migrated = Store(db_path)
            try:
                self.assertEqual(migrated.legacy_backup, Path(tmp) / "c.db.legacy-2026-11-26-120000-1")
                self.assertEqual(existing_backup.read_text(encoding="utf-8"), "keep-me")
                self.assertTrue(migrated.legacy_backup.exists())
                migrated.upsert_announcement(
                    "https://www.mohurd.gov.cn/v2.html", "2026-01-01",
                    "标题", "标准", "national",
                )
                migrated.commit()
            finally:
                migrated.close()

            reopened = Store(db_path)
            try:
                self.assertEqual(reopened.count_announcements(), 1)
            finally:
                reopened.close()

    def test_catalog_age_days(self):
        from datetime import datetime, timedelta

        self.assertIsNone(self.store.catalog_age_days())  # 从未同步
        old = datetime.now() - timedelta(days=9)
        self.store.set_meta("synced_at", old.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertAlmostEqual(self.store.catalog_age_days(), 9.0, delta=0.01)
        self.store.set_meta("synced_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.assertLess(self.store.catalog_age_days(), 0.01)


if __name__ == "__main__":
    unittest.main()
