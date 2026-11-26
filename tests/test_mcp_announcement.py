"""MCP announcement 工具：--db 注入、真只读存储白名单门面、未知源 50、共享/跨线程库与并发 I/O。"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from guiju import cli
from guiju.currency.native import Outcome
from guiju.mcp import McpServer, _ReadOnlyStore
from guiju.store.sqlite import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
SHZJW_URL = "https://bzjg.zjw.sh.gov.cn/api/notice/{}"


class _FakeBackend:
    name = "fake"

    def query(self, code):
        return Outcome((), self.name)


def _call(server, name, arguments):
    return server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })


def _error_payload(result):
    return json.loads(result["content"][0]["text"])


class CliDbInjectionTest(unittest.TestCase):
    """--db 必须传给 McpServer，announcement 读的是同一个库。"""

    def _seed(self, db: Path, ann_id_url: str = SHZJW_URL.format(42)) -> int:
        store = Store(db)
        try:
            ann_id, _ = store.upsert_announcement(
                ann_id_url, "2024-01-05", "公告标题", "某标准", "other")
            store.upsert_edition("DG/TJ08-2007-2026", "DG/TJ08-2007-2026", None, "parsed")
            store.add_mention(ann_id, "DG/TJ08-2007-2026", "primary", "发布")
            store.commit()
        finally:
            store.close()
        return ann_id

    def test_cli_passes_db_path_and_reads_custom_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "custom.db"
            ann_id = self._seed(db)
            captured = {}

            def fake_serve(server):
                captured["server"] = server
                return 0

            with mock.patch.dict(
                    os.environ,
                    {"GUIJU_HOME": str(root / "home"), "GUIJU_NO_AUTO_SYNC": "1"}), \
                    mock.patch("guiju.mcp.serve_stdio", side_effect=fake_serve):
                rc = cli.main(["--db", str(db), "mcp"])
            self.assertEqual(rc, 0)
            server = captured["server"]
            try:
                self.assertEqual(Path(server.store_path), db)
                response = _call(server, "announcement", {"ref": str(ann_id)})
                payload = json.loads(response["result"]["content"][0]["text"])
                self.assertFalse(response["result"]["isError"])
                self.assertEqual(payload["announcement_id"], ann_id)
                self.assertEqual(payload["source"], "shzjw")
            finally:
                server.close()

    def test_default_library_unaffected_without_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            default_db = home / "catalog.db"
            ann_id = self._seed(default_db, SHZJW_URL.format(7))
            captured = {}

            def fake_serve(server):
                captured["server"] = server
                return 0

            with mock.patch.dict(
                    os.environ,
                    {"GUIJU_HOME": str(home), "GUIJU_NO_AUTO_SYNC": "1"}), \
                    mock.patch("guiju.mcp.serve_stdio", side_effect=fake_serve):
                rc = cli.main(["mcp"])
            self.assertEqual(rc, 0)
            server = captured["server"]
            try:
                self.assertEqual(Path(server.store_path), default_db)
                ok = _call(server, "announcement", {"ref": str(ann_id)})
                self.assertFalse(ok["result"]["isError"])
                missing = _call(server, "announcement", {"ref": "42"})
                self.assertTrue(missing["result"]["isError"])
                self.assertEqual(_error_payload(missing["result"])["exit"], 31)
            finally:
                server.close()

    def test_subprocess_cli_db_announcement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "sub.db"
            ann_id = self._seed(db)
            payloads = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "announcement", "arguments": {"ref": str(ann_id)}}},
            ]
            env = dict(os.environ)
            env.update({"GUIJU_HOME": str(root / "home"), "GUIJU_NO_AUTO_SYNC": "1"})
            proc = subprocess.run(
                [sys.executable, "-m", "guiju", "--db", str(db), "mcp"],
                input="\n".join(json.dumps(item, ensure_ascii=False) for item in payloads) + "\n",
                capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            response = next(item for item in responses if item.get("id") == 2)
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(payload["announcement_id"], ann_id)


class ReadOnlyAnnouncementStoreTest(unittest.TestCase):
    """announcement 的库必须是结构只读；旧 schema/损坏库显式 50，不替用户迁移。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = self.home / "catalog.db"
        patcher = mock.patch.dict(
            os.environ,
            {"GUIJU_HOME": str(self.home), "GUIJU_NO_AUTO_SYNC": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = McpServer(_FakeBackend())
        self.addCleanup(self.server.close)

    def _seed(self, url: str = SHZJW_URL.format(42)) -> int:
        store = Store(self.db)
        try:
            ann_id, _ = store.upsert_announcement(
                url, "2024-01-05", "公告标题", "某标准", "other")
            store.upsert_edition("DG/TJ08-2007-2026", "DG/TJ08-2007-2026", None, "parsed")
            store.add_mention(ann_id, "DG/TJ08-2007-2026", "primary", "发布")
            store.commit()
        finally:
            store.close()
        return ann_id

    def _tables(self) -> set:
        db = sqlite3.connect(self.db)
        try:
            return {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        finally:
            db.close()

    def _assert_announcement(self, ref, expected_id):
        response = _call(self.server, "announcement", {"ref": str(ref)})
        self.assertFalse(response["result"]["isError"], response)
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["announcement_id"], expected_id)

    def _assert_error(self, ref, exit_code):
        response = _call(self.server, "announcement", {"ref": str(ref)})
        self.assertTrue(response["result"]["isError"])
        payload = _error_payload(response["result"])
        self.assertEqual(payload["exit"], exit_code, payload)
        return payload

    def test_missing_db_bootstraps_empty_schema_and_returns_31(self):
        self._assert_error(1, 31)
        self.assertTrue(self.db.exists())
        self.assertIn("announcement", self._tables())
        store = Store(self.db)
        try:
            self.assertEqual(store.count_announcements(), 0)
        finally:
            store.close()

    def test_zero_byte_db_bootstraps(self):
        self.db.write_bytes(b"")
        self._assert_error(1, 31)
        self.assertIn("announcement", self._tables())

    def test_empty_db_without_tables_bootstraps(self):
        db = sqlite3.connect(self.db)
        db.execute("PRAGMA user_version = 0")
        db.commit()
        db.close()
        self._assert_error(1, 31)
        self.assertIn("announcement", self._tables())

    def test_existing_v2_db_is_readable(self):
        ann_id = self._seed()
        self._assert_announcement(ann_id, ann_id)

    def test_v0_with_tables_is_needs_human_and_not_rebuilt(self):
        self.db.unlink(missing_ok=True)
        db = sqlite3.connect(self.db)
        db.execute("CREATE TABLE announcement (id INTEGER PRIMARY KEY, url TEXT)")
        db.execute("PRAGMA user_version = 0")
        db.commit()
        db.close()
        before = self.db.read_bytes()
        payload = self._assert_error(1, 50)
        self.assertIn("schema", payload["error"])
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(list(self.home.glob("catalog.db.legacy-*")), [])

    def test_v2_missing_core_table_is_needs_human(self):
        self._seed()
        db = sqlite3.connect(self.db)
        db.execute("DROP TABLE edition")
        db.commit()
        db.close()
        payload = self._assert_error(1, 50)
        self.assertIn("edition", payload["error"])
        self.assertNotIn("edition", self._tables())

    def test_v2_only_missing_currency_cache_is_readable(self):
        ann_id = self._seed()
        db = sqlite3.connect(self.db)
        db.execute("DROP TABLE IF EXISTS currency_cache")
        db.commit()
        db.close()
        self._assert_announcement(ann_id, ann_id)
        self.assertNotIn("currency_cache", self._tables(), "只读路径不得补 DDL")

    def test_old_v2_without_mention_confidence_reads_edition_fallback(self):
        ann_id = self._seed()
        db = sqlite3.connect(self.db)
        try:
            db.execute("ALTER TABLE mention DROP COLUMN confidence")
            db.commit()
        except sqlite3.OperationalError as exc:  # pragma: no cover - 老 SQLite
            db.close()
            self.skipTest(f"SQLite 不支持 DROP COLUMN：{exc}")
        finally:
            with contextlib.suppress(sqlite3.ProgrammingError):
                db.close()
        ro = self.server._ann_store()
        rows = ro.mentions_for(ann_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["confidence"], "parsed")
        self._assert_announcement(ann_id, ann_id)

    def test_unknown_schema_version_is_needs_human(self):
        self._seed()
        db = sqlite3.connect(self.db)
        db.execute("PRAGMA user_version = 999")
        db.commit()
        db.close()
        payload = self._assert_error(1, 50)
        self.assertIn("999", payload["error"])

    def test_corrupt_file_is_needs_human(self):
        self.db.write_bytes(b"this is not a sqlite database at all" * 10)
        payload = self._assert_error(1, 50)
        self.assertIn("数据库", payload["error"])

    def test_announcement_does_not_mutate_mentions(self):
        ann_id = self._seed()
        store = Store(self.db)
        try:
            before = (store.count_announcements(), store.stats()["mentions"], store.stats()["editions"])
        finally:
            store.close()
        self._assert_announcement(ann_id, ann_id)
        store = Store(self.db)
        try:
            after = (store.count_announcements(), store.stats()["mentions"], store.stats()["editions"])
        finally:
            store.close()
        self.assertEqual(before, after)

    def test_direct_delete_on_underlying_db_is_rejected(self):
        self._seed()
        ro = self.server._ann_store()
        with self.assertRaises(AttributeError):
            _ = ro.db
        with self.assertRaises(sqlite3.OperationalError):
            ro._inner.db.execute("DELETE FROM announcement")
        self.assertEqual(ro.count_announcements(), 1)

    def test_readonly_facade_keeps_blocking_known_writes(self):
        ann_id = self._seed()
        ro = self.server._ann_store()
        for attr in (
            "upsert_announcement",
            "upsert_edition",
            "add_mention",
            "mark_scanned",
            "persist_codes",
            "log_probe",
            "record_body_hash",
            "set_meta",
            "commit",
            "delete_all",
        ):
            with self.assertRaises(AttributeError, msg=attr):
                getattr(ro, attr)
        self.assertEqual(ro.count_announcements(), 1)
        self.assertIsNotNone(ro.get_ann(ann_id))
        self.assertEqual(len(ro.mentions_for(ann_id)), 1)

    def test_readonly_parent_directory_uses_immutable(self):
        ann_id = self._seed()
        os.chmod(self.home, 0o555)
        self.addCleanup(os.chmod, self.home, 0o755)
        self._assert_announcement(ann_id, ann_id)

    def test_readonly_parent_with_pending_wal_is_needs_human(self):
        self._seed()
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"pending-wal")
        os.chmod(self.home, 0o555)
        self.addCleanup(os.chmod, self.home, 0o755)
        payload = self._assert_error(1, 50)
        self.assertIn("WAL", payload["error"])

    def test_announcement_detail_network_does_not_block_concurrent_announcement(self):
        mohurd_id = self._seed("https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2026/art_17339_1.html")
        shzjw_id = self._seed(SHZJW_URL.format(42))
        entered = threading.Event()
        release = threading.Event()

        class _SlowMohurd:
            id = "mohurd"
            api_mode = False

            def detail(self, _url):
                entered.set()
                release.wait(timeout=3)
                return "<html></html>", "正文"

            def attachments(self, _html):
                return []

        class _FastShzjw:
            id = "shzjw"
            api_mode = True

        res_a = {}
        with mock.patch(
            "guiju.sources.registry",
            return_value={"mohurd": _SlowMohurd(), "shzjw": _FastShzjw()},
        ):
            thread_a = threading.Thread(
                target=lambda: res_a.update(
                    _call(self.server, "announcement", {"ref": str(mohurd_id)})
                ),
                daemon=True,
            )
            thread_a.start()
            try:
                self.assertTrue(entered.wait(timeout=2), "线程 A 未进入 detail()")
                res_b = _call(self.server, "announcement", {"ref": str(shzjw_id)})
                self.assertFalse(res_b["result"]["isError"], res_b)
                payload_b = json.loads(res_b["result"]["content"][0]["text"])
                self.assertEqual(payload_b["announcement_id"], shzjw_id)
                self.assertEqual(payload_b["attachment_verdict"], "api_mode")
            finally:
                release.set()
                thread_a.join(timeout=3)
        self.assertFalse(thread_a.is_alive())
        self.assertIn("result", res_a)
        payload_a = json.loads(res_a["result"]["content"][0]["text"])
        self.assertEqual(payload_a["announcement_id"], mohurd_id)


class TestAnnouncementUnknownSource(unittest.TestCase):
    """_announcement 不许静默回落 mohurd：认不出的源要显式 needs_human。"""

    def test_unresolved_source_id_raises_needs_human(self):
        server = McpServer(_FakeBackend())
        ann = {"id": 1, "url": "https://some.unknown.gov.cn/notice/1",
               "doc_name": "某标准", "title": "t", "category": "",
               "published_on": "2026-01-01"}
        fake_target = {"ann": ann, "code_display": "GB 1", "status": "", "confidence": ""}
        fake_source = mock.Mock()
        with mock.patch("guiju.application.resolve.resolve", return_value=fake_target), \
                mock.patch("guiju.sources.registry", return_value={"mohurd": fake_source}), \
                tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"GUIJU_HOME": tmp}):
            try:
                result = server._announcement({"ref": "https://some.unknown.gov.cn/notice/1"})
            finally:
                server.close()
        self.assertTrue(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["reason"], "needs_human")
        self.assertEqual(payload["exit"], 50)
        fake_source.detail.assert_not_called()


class TestSharedAnnouncementStore(unittest.TestCase):
    """announcement 复用 McpServer 生命周期的共享 Store（不再每请求跑 DDL）。"""

    def _call(self, server):
        ann = {"id": 1, "url": "https://www.mohurd.gov.cn/x/1",
               "doc_name": "某标准", "title": "t", "category": "",
               "published_on": "2026-01-01"}
        fake_target = {"ann": ann, "code_display": "GB 1", "status": "", "confidence": ""}
        fake_source = mock.Mock(api_mode=True)
        with mock.patch("guiju.application.resolve.resolve", return_value=fake_target), \
                mock.patch("guiju.sources.registry", return_value={"mohurd": fake_source}):
            return server._announcement({"ref": "1"})

    def test_store_reused_across_requests_and_closed_once(self):
        created = []
        instances = []

        class _FakeStore:
            def __init__(self, path=None, **kw):
                created.append({"path": path, **kw})
                self.db = object()  # 底层连接对象（_ReadOnlyStore 白名单代理不会暴露 .db）
                self.closed = 0
                instances.append(self)

            def close(self):
                self.closed += 1

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"GUIJU_HOME": tmp}), \
                mock.patch("guiju.store.sqlite.Store", _FakeStore):
            server = McpServer(_FakeBackend())
            self._call(server)
            self._call(server)
            self.assertEqual(len(created), 2)
            self.assertEqual(created[0].get("cross_thread"), True)
            self.assertNotIn("read_only", created[0])
            self.assertEqual(created[1].get("cross_thread"), True)
            self.assertEqual(created[1].get("read_only"), True)
            server.close()
            self.assertEqual(instances[0].closed, 1)
            self.assertEqual(instances[1].closed, 1)

    def test_cross_thread_store_usable_from_other_thread(self):
        # cross_thread=True 的连接在线程间用不再触发 sqlite3.ProgrammingError
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(Path(tmp) / "c.db"), cross_thread=True)
            errors = []

            def q():
                try:
                    store.count_announcements()
                    store.catalog_age_days()
                except Exception as exc:
                    errors.append(exc)

            t = threading.Thread(target=q)
            t.start()
            t.join()
            store.close()
            self.assertEqual(errors, [])


class AnnouncementReadOnlyTest(unittest.TestCase):
    def _server(self):
        return McpServer(_FakeBackend())

    def test_announcement_uses_readonly_offline_resolve(self):
        ann = {
            "id": 7,
            "url": "https://www.mohurd.gov.cn/gov/zhengce/x/7.html",
            "published_on": "2024-01-05",
            "title": "《工程结构通用规范》发布公告",
            "doc_name": "工程结构通用规范",
            "category": "national",
        }
        target = {
            "ann": ann, "code_key": "GB55001-2021", "code_display": "GB 55001-2021",
            "confidence": "parsed", "status": "current",
        }

        class FakeSource:
            id = "mohurd"
            api_mode = False

            def detail(self, url):
                return ("<html>body</html>", "正文")

            def attachments(self, html):
                return []

        server = self._server()
        self.addCleanup(server.close)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            "os.environ", {"GUIJU_HOME": home}
        ), mock.patch(
            "guiju.application.resolve.resolve", return_value=target
        ) as resolve_mock, mock.patch(
            "guiju.sources.registry", return_value={"mohurd": FakeSource()}
        ):
            resp = server.handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "announcement", "arguments": {"ref": "GB 55001-2021"}},
            })
            self.assertFalse(resp["result"]["isError"])
            _, kwargs = resolve_mock.call_args
            self.assertEqual(kwargs.get("allow_network"), False)
            store_arg = resolve_mock.call_args.args[0]
            self.assertIsInstance(store_arg, _ReadOnlyStore)

    def test_readonly_store_blocks_non_whitelist_attributes(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(
            "os.environ", {"GUIJU_HOME": home}
        ):
            real = Store()
            real.upsert_announcement("https://x/keep.html", "2024-01-01", "t", "d", "national")
            real.commit()
            ro = _ReadOnlyStore(real)
            self.assertEqual(ro.count_announcements(), 1)
            self.assertIsNotNone(ro.get_ann(1))
            for attr in (
                "upsert_announcement",
                "upsert_edition",
                "add_mention",
                "mark_scanned",
                "persist_codes",
                "log_probe",
                "record_body_hash",
                "set_meta",
                "commit",
                "db",
                "future_write_method",
            ):
                with self.assertRaises(AttributeError, msg=attr):
                    getattr(ro, attr)
            self.assertEqual(real.count_announcements(), 1)
            self.assertIsNone(real.get_ann_by_url("https://x/new.html"))
            self.assertEqual(real.stats()["editions"], 0)
            real.close()


if __name__ == "__main__":
    unittest.main()
