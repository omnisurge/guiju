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
from guiju.errors import GuijuError, NeedsHuman
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
