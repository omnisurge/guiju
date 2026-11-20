"""A1：损坏数据库必须返回可解释的 50，而不是裸 traceback。"""

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.cli import main
from guiju.cli.commands import sync as sync_mod
from guiju.errors import GuijuError, NeedsHuman, SourceUnreachable
from guiju.store.sqlite import Store


class CorruptStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "catalog.db"
        self.path.write_bytes(b"this is not a sqlite database\x00" * 4)

    def test_store_raises_needs_human(self):
        with self.assertRaises(NeedsHuman) as ctx:
            Store(self.path)
        self.assertEqual(ctx.exception.exit, 50)
        self.assertIn("数据库无法打开或已损坏", str(ctx.exception))

    def test_cli_returns_json_50(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--json", "--db", str(self.path), "status"])
        self.assertEqual(code, 50)
        payload = json.loads(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit"], 50)
        self.assertEqual(payload["reason"], "needs_human")

    def test_oserror_path_maps_to_50(self):
        blocked = Path(self.tmp.name) / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(NeedsHuman):
            Store(blocked / "catalog.db")
        with self.assertRaises(NeedsHuman):
            Store(Path(self.tmp.name))

    def test_transient_lock_maps_to_retryable_20(self):
        with mock.patch(
            "guiju.store.sqlite.sqlite3.connect",
            side_effect=sqlite3.OperationalError("database is locked"),
        ), self.assertRaises(SourceUnreachable) as ctx:
            Store(self.path)
        self.assertEqual(ctx.exception.exit, 20)

    def test_existing_readonly_v2_db_can_be_read(self):
        db_path = Path(self.tmp.name) / "readonly.db"
        writer = Store(db_path)
        writer.upsert_announcement("https://x/1", "2026-01-01", "标题", "名称", "other")
        writer.commit()
        writer.close()
        db_path.chmod(0o444)
        self.addCleanup(db_path.chmod, 0o644)
        reader = Store(db_path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.count_announcements(), 1)

    def test_readonly_directory_v2_db_can_be_read(self):
        db_dir = Path(self.tmp.name) / "ro-dir"
        db_dir.mkdir()
        db_path = db_dir / "catalog.db"
        writer = Store(db_path)
        writer.upsert_announcement("https://x/1", "2026-01-01", "标题", "名称", "other")
        writer.commit()
        writer.close()
        db_path.chmod(0o444)
        db_dir.chmod(0o555)
        self.addCleanup(db_dir.chmod, 0o755)
        self.addCleanup(db_path.chmod, 0o644)
        reader = Store(db_path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.count_announcements(), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--json", "--db", str(db_path), "status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["announcements"], 1)

    def test_readonly_v2_without_cache_table_can_be_read(self):
        db_dir = Path(self.tmp.name) / "ro-no-cache"
        db_dir.mkdir()
        db_path = db_dir / "catalog.db"
        writer = Store(db_path)
        writer.upsert_announcement("https://x/1", "2026-01-01", "标题", "名称", "other")
        writer.db.execute("DROP TABLE currency_cache")
        writer.commit()
        writer.close()
        db_path.chmod(0o444)
        db_dir.chmod(0o555)
        self.addCleanup(db_dir.chmod, 0o755)
        self.addCleanup(db_path.chmod, 0o644)
        reader = Store(db_path)
        self.addCleanup(reader.close)
        self.assertEqual(reader.count_announcements(), 1)

    def test_readonly_dir_with_pending_wal_is_not_silently_read(self):
        db_dir = Path(self.tmp.name) / "ro-pending-wal"
        db_dir.mkdir()
        db_path = db_dir / "catalog.db"
        Store(db_path).close()
        db_path.with_name(db_path.name + "-wal").write_bytes(b"not-empty-wal")
        db_path.chmod(0o444)
        db_dir.chmod(0o555)
        self.addCleanup(db_dir.chmod, 0o755)
        self.addCleanup(db_path.chmod, 0o644)
        with self.assertRaises(NeedsHuman) as ctx:
            Store(db_path)
        self.assertIn("未合并的 WAL", str(ctx.exception))

    def test_sync_lock_file_open_failure_maps_50(self):
        db_dir = Path(self.tmp.name) / "ro-lock"
        db_dir.mkdir()
        db_path = db_dir / "catalog.db"
        Store(db_path).close()
        db_path.chmod(0o444)
        db_dir.chmod(0o555)
        self.addCleanup(db_dir.chmod, 0o755)
        self.addCleanup(db_path.chmod, 0o644)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--json", "--db", str(db_path), "sync"])
        self.assertEqual(code, 50)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["exit"], 50)
        self.assertIn("同步锁文件", payload["error"])

    def test_readonly_write_maps_to_json_50_not_internal(self):
        db_path = Path(self.tmp.name) / "readonly-write.db"
        Store(db_path).close()
        db_path.chmod(0o444)
        self.addCleanup(db_path.chmod, 0o644)
        out = io.StringIO()
        with mock.patch.object(
            sync_mod, "crawl",
            side_effect=sqlite3.OperationalError("attempt to write a readonly database"),
        ), contextlib.redirect_stdout(out):
            code = main(["--json", "--db", str(db_path), "sync"])
        self.assertEqual(code, 50)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["exit"], 50)
        self.assertEqual(payload["reason"], "needs_human")

    def test_future_schema_closes_connection(self):
        future = Path(self.tmp.name) / "future.db"
        db = sqlite3.connect(future)
        db.execute("PRAGMA user_version = 99")
        db.commit()
        db.close()
        real_connect = sqlite3.connect
        created = []

        def tracking(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            created.append(conn)
            return conn

        with mock.patch("guiju.store.sqlite.sqlite3.connect", side_effect=tracking), \
                self.assertRaises(GuijuError):
            Store(future)
        self.assertEqual(len(created), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            created[0].execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
