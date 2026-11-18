"""A1：刷新失败时写冷却时间戳失败，也不能把查询命令带崩。"""

import sqlite3
import tempfile
import unittest
from unittest import mock

from guiju import autosync
from guiju.store.sqlite import Store


class AutosyncFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(f"{self.tmp.name}/catalog.db")
        self.addCleanup(self.store.close)

    def test_cooldown_write_failure_does_not_escape(self):
        with mock.patch("guiju.syncengine.crawl", side_effect=RuntimeError("boom")), \
                mock.patch.object(self.store, "set_meta", side_effect=sqlite3.OperationalError("readonly")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refresh_failed")
        self.assertIn("目录刷新失败", snap["message"])

    def test_clear_failure_does_not_block_refreshed(self):
        with mock.patch("guiju.syncengine.crawl", return_value={"new": 3}), \
                mock.patch.object(self.store, "set_meta", side_effect=sqlite3.OperationalError("readonly")):
            snap = autosync.evaluate(self.store, command="search")
        self.assertEqual(snap["state"], "refreshed")
        self.assertEqual(snap["new"], 3)


if __name__ == "__main__":
    unittest.main()
