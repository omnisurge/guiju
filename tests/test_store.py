"""存储层：展示号与归一化号必须是同一条记录。"""

import tempfile
import unittest
from pathlib import Path

from guiju.store.sqlite import Store


class ArtifactKeyTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "c.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement("https://www.mohurd.gov.cn/x.html", "2021-07-15", "标题", "工程结构通用规范", "national")

    def _record(self, code_key: str):
        self.store.record_artifact(
            ann_id=1, code_key=code_key, source="mohurd", page_url="https://www.mohurd.gov.cn/x.html",
            file_url="https://www.mohurd.gov.cn/f.pdf", label="正文", sha256="a" * 64,
            size=10, magic="pdf", local_path="/tmp/a.pdf",
        )

    def test_display_and_normalized_are_same_row(self):
        self._record("GB 55001-2021")
        self.assertIsNotNone(self.store.artifact_by_code("GB55001-2021"))
        self.assertIsNotNone(self.store.artifact_by_code("gb 55001-2021"))

    def test_normalized_stored_lookup_display(self):
        self._record("GB55001-2021")
        self.assertIsNotNone(self.store.artifact_by_code("GB 55001-2021"))

    def test_missing_returns_none(self):
        self.assertIsNone(self.store.artifact_by_code("GB 99999-2099"))


if __name__ == "__main__":
    unittest.main()
