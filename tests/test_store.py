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
