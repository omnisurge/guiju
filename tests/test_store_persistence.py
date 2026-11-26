"""只读 Store、损坏数据库/权限错误映射（50/20）、CLI schema 错误与 stats 统计测试。"""

from __future__ import annotations

import contextlib
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
from guiju import report as render
from guiju.cli import main
from guiju.cli.commands import sync as sync_mod
from guiju.errors import GuijuError, NeedsHuman, SourceUnreachable
from guiju.store.sqlite import Store


class ReadOnlyStoreTest(unittest.TestCase):
    """W2-4：显式处处只读 + 自动只读兼容；缺库/旧库绝不偷偷建/迁移。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "catalog.db"

    def _make_db(self, n: int = 1) -> None:
        store = Store(self.path)
        try:
            for i in range(n):
                store.upsert_announcement(
                    f"https://x/{i}", "2026-01-01", f"标题{i}", f"名称{i}", "other"
                )
            store.commit()
        finally:
            store.close()

    def test_explicit_read_only_missing_file_fails_without_creating(self):
        with self.assertRaises(NeedsHuman) as ctx:
            Store(self.path, read_only=True)
        self.assertEqual(ctx.exception.exit, 50)
        self.assertFalse(self.path.exists())

    def test_explicit_read_only_missing_parent_does_not_mkdir(self):
        missing = self.dir / "missing" / "catalog.db"
        with self.assertRaises(NeedsHuman):
            Store(missing, read_only=True)
        self.assertFalse(missing.parent.exists())

    def test_explicit_read_only_zero_byte_file_fails(self):
        self.path.write_bytes(b"")
        with self.assertRaises(NeedsHuman):
            Store(self.path, read_only=True)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_writable_path_still_creates_and_writes(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        self.assertFalse(store.read_only)
        store.upsert_announcement("https://x/1", "2026-01-01", "t", "n", "other")
        store.commit()
        self.assertEqual(store.count_announcements(), 1)

    def test_explicit_read_only_v2_db_reads_announcement(self):
        self._make_db(2)
        reader = Store(self.path, read_only=True)
        self.addCleanup(reader.close)
        self.assertEqual(reader.count_announcements(), 2)

    def test_explicit_read_only_rejects_insert_and_delete(self):
        self._make_db()
        reader = Store(self.path, read_only=True)
        self.addCleanup(reader.close)
        with self.assertRaises(sqlite3.OperationalError):
            reader.db.execute("INSERT INTO announcement(url,title) VALUES('https://x/n','t')")
        with self.assertRaises(sqlite3.OperationalError):
            reader.db.execute("DELETE FROM announcement")

    def test_auto_and_explicit_read_only_coexist(self):
        self._make_db()
        self.path.chmod(0o444)
        self.dir.chmod(0o555)
        self.addCleanup(self.dir.chmod, 0o755)
        self.addCleanup(self.path.chmod, 0o644)
        auto = Store(self.path)
        self.addCleanup(auto.close)
        self.assertEqual(auto.count_announcements(), 1)
        explicit = Store(self.path, read_only=True)
        self.addCleanup(explicit.close)
        self.assertEqual(explicit.count_announcements(), 1)

    def test_explicit_read_only_reads_pending_wal_latest(self):
        writer = Store(self.path)
        self.addCleanup(writer.close)
        writer.upsert_announcement("https://x/1", "2026-01-01", "t", "n", "other")
        writer.upsert_announcement("https://x/2", "2026-01-01", "t", "n", "other")
        writer.commit()
        self.assertTrue((self.dir / "catalog.db-wal").exists())
        reader = Store(self.path, read_only=True)
        self.addCleanup(reader.close)
        # 不能静默读主文件旧快照：WAL 里已提交的 2 条必须可见。
        self.assertEqual(reader.count_announcements(), 2)

    def test_explicit_read_only_cross_thread_readable(self):
        self._make_db()
        reader = Store(self.path, read_only=True, cross_thread=True)
        self.addCleanup(reader.close)
        errors: list[Exception] = []

        def query():
            try:
                reader.count_announcements()
            except Exception as exc:  # 记录以便断言零异常
                errors.append(exc)

        thread = threading.Thread(target=query)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(reader.count_announcements(), 1)

    def test_v0_legacy_db_read_only_is_not_rebuilt(self):
        raw = sqlite3.connect(self.path)
        raw.execute("CREATE TABLE old(x TEXT)")
        raw.execute("INSERT INTO old VALUES('keep')")
        raw.execute("PRAGMA user_version = 0")
        raw.commit()
        raw.close()
        before = self.path.read_bytes()
        with self.assertRaises(NeedsHuman):
            Store(self.path, read_only=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.dir.glob("*.legacy-*")), [])

    def test_read_only_construction_executes_no_ddl_or_journal_mode(self):
        self._make_db()
        statements: list[str] = []
        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        with mock.patch("guiju.store.sqlite.sqlite3.connect", side_effect=tracking_connect):
            reader = Store(self.path, read_only=True)
        reader.close()
        joined = "\n".join(statements).lower()
        for forbidden in ("create table", "alter table", "insert into", "pragma journal_mode"):
            self.assertNotIn(forbidden, joined, f"只读构造执行了写操作：{forbidden}")
        self.assertTrue(any("pragma table_info" in s.lower() for s in statements))


class CorruptStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(render.configure, as_json=False, quiet=False)
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
        writer.db.execute("DROP TABLE IF EXISTS currency_cache")
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


class CliW4Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(render.configure, as_json=False, quiet=False)
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


class StatsNoDoubleCountTest(unittest.TestCase):
    def test_stats_counts_null_year_once(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            "os.environ", {"GUIJU_HOME": home}
        ):
            store = Store()
            store.upsert_announcement("https://x/1.html", "2024-01-01", "t", "d", "national")
            store.upsert_announcement("https://x/2.html", "2024-06-01", "t", "d", "national")
            store.upsert_announcement("https://x/3.html", None, "t", "d", "national")
            store.commit()
            s = store.stats()
            self.assertEqual(s["announcements"], 3)
            self.assertEqual(s["by_year"], {"2024": 2})
            store.close()

    def test_stats_does_not_call_count_announcements(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            "os.environ", {"GUIJU_HOME": home}
        ):
            store = Store()
            store.upsert_announcement("https://x/1.html", "2024-01-01", "t", "d", "national")
            with mock.patch.object(
                store, "count_announcements", wraps=store.count_announcements
            ) as spy:
                store.stats()
                self.assertEqual(spy.call_count, 0)
            store.close()


if __name__ == "__main__":
    unittest.main()
