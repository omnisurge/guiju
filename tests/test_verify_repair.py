"""verify --repair：文件挪动后按内容指纹重新对位 local_path。"""

import argparse
import tempfile
import unittest
from pathlib import Path

from guiju.cli.commands import verify
from guiju.files import sha256_file
from guiju.store.sqlite import Store


class RepairTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.lib = self.home / "library"
        self.lib.mkdir()
        self.store = Store(self.home / "catalog.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/x.html", "2021-07-15", "标题", "某标准", "national"
        )

    def _write(self, name: str, body: bytes = b"%PDF-fake") -> Path:
        p = self.lib / name
        p.write_bytes(body)
        return p

    def _record(self, path: Path) -> str:
        digest = sha256_file(path)
        self.store.record_artifact(
            ann_id=1, code_key="GB1-2021", source="mohurd",
            page_url="https://www.mohurd.gov.cn/x.html",
            file_url="https://www.mohurd.gov.cn/f.pdf", label="正文",
            sha256=digest, size=path.stat().st_size, magic="pdf",
            local_path=str(path),
        )
        self.store.commit()
        return digest

    def _args(self, repair: bool, dir_: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(repair=repair, dir=dir_)

    def test_missing_without_repair_reports_missing(self):
        f = self._write("a__deadbeef.pdf")
        self._record(f)
        f.rename(self.lib / "moved.pdf")
        self.assertEqual(verify.run(self.store, self._args(False)), 50)

    def test_repair_repoints_local_path(self):
        f = self._write("a__deadbeef.pdf")
        digest = self._record(f)
        moved = self.lib / "sub" / "moved.pdf"
        moved.parent.mkdir()
        f.rename(moved)

        self.assertEqual(verify.run(self.store, self._args(True)), 0)
        rec = self.store.artifact_by_sha(digest)
        self.assertEqual(rec["local_path"], str(moved))

    def test_repair_leaves_modified_and_missing(self):
        good = self._write("ok.pdf")
        self._record(good)
        orphan = self._write("stray.pdf", b"untracked bytes")
        self.assertEqual(verify.run(self.store, self._args(True)), 0)
        rec = self.store.artifact_by_sha(sha256_file(good))
        self.assertEqual(rec["local_path"], str(good))
        self.assertTrue(orphan.exists())

    def test_partial_dir_skipped(self):
        f = self._write("x.pdf")
        self._record(f)
        part_dir = self.lib / ".partial"
        part_dir.mkdir()
        (part_dir / "ab.part").write_bytes(sha256_file(f).encode())
        self.assertEqual(verify.run(self.store, self._args(True)), 0)


if __name__ == "__main__":
    unittest.main()
