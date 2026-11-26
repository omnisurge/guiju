"""1.2.0a4 W2-3..W2-6 回归：年份降级、Store 只读、按公告 confidence、edition.status。

这些用例按修复前缺陷编写：在 f98e0d2（1.2.0a3）上应当失败。
"""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from guiju.domain.codes import Codes, Confidence, Mention, Role
from guiju.domain.search import rank, tokens_of
from guiju.errors import NeedsHuman
from guiju.store.sqlite import Store, persist_codes

_LEGACY_V2_SCHEMA = """
CREATE TABLE announcement (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    legacy_url TEXT,
    published_on TEXT,
    title TEXT NOT NULL,
    doc_name TEXT,
    category TEXT NOT NULL DEFAULT 'other',
    body_sha256 TEXT,
    fetched_at TEXT
);
CREATE TABLE edition (
    code_key TEXT PRIMARY KEY,
    code_display TEXT NOT NULL,
    title TEXT,
    mandatory INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unknown',
    replaces TEXT,
    replaced_by TEXT,
    confidence TEXT NOT NULL DEFAULT 'parsed'
);
CREATE TABLE mention (
    ann_id INTEGER NOT NULL REFERENCES announcement(id) ON DELETE CASCADE,
    code_key TEXT NOT NULL REFERENCES edition(code_key),
    role TEXT NOT NULL,
    evidence TEXT,
    PRIMARY KEY (ann_id, code_key)
);
CREATE TABLE scan (
    ann_id INTEGER PRIMARY KEY,
    scanned_at TEXT NOT NULL,
    codes_found INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE probe (
    id INTEGER PRIMARY KEY, source TEXT, url TEXT, status INTEGER,
    bytes INTEGER, outcome TEXT, note TEXT, at TEXT
);
CREATE TABLE corpus_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE currency_cache (
    code_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL
);
"""


def _write_legacy_v2(path: Path) -> None:
    """1.2.0a4 之前的 v2 库：user_version=2，mention 表没有 confidence 列。"""
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_V2_SCHEMA)
    conn.execute("PRAGMA user_version = 2")
    conn.execute(
        "INSERT INTO announcement(id,url,published_on,title,doc_name,category,fetched_at)"
        " VALUES(1,'https://x/a','2026-01-01','关于发布某标准的公告','某标准','national',"
        "'2026-01-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO announcement(id,url,published_on,title,doc_name,category,fetched_at)"
        " VALUES(2,'https://x/b','2026-01-01','关于某标准的公告','某标准','national',"
        "'2026-01-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO edition(code_key,code_display,title,status,confidence)"
        " VALUES('GB50016-2014','GB 50016-2014','某标准','unknown','parsed')"
    )
    conn.execute(
        "INSERT INTO mention(ann_id,code_key,role,evidence)"
        " VALUES(1,'GB50016-2014','primary','编号为GB 50016-2014')"
    )
    conn.commit()
    conn.close()


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "catalog.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def _ann(self, url: str = "https://x/1", name: str = "某标准") -> int:
        ann_id, _ = self.store.upsert_announcement(
            url, "2026-01-01", f"关于发布《{name}》的公告", name, "national"
        )
        self.store.commit()
        return ann_id


class PersistCodesYearDemotionTest(_StoreCase):
    """W2-3：older 降级必须同时有 role/confidence/同族/4 位年份证据。"""

    def _persist(self, primary_key: str, mentions, confidence=Confidence.PARSED) -> int:
        ann_id = self._ann()
        codes = Codes(
            mentions=tuple(mentions),
            primary_key=primary_key,
            primary_display=primary_key,
            confidence=confidence,
        )
        persist_codes(self.store, ann_id, codes, "某标准")
        self.store.commit()
        return ann_id

    def _roles(self, ann_id: int) -> dict:
        return {r["code_key"]: r["role"] for r in self.store.mentions_for(ann_id)}

    def test_newer_same_family_mention_is_not_demoted(self):
        ann_id = self._persist(
            "GB/T50362-2010",
            [
                Mention("GB/T50362-2010", "GB/T 50362-2010", Role.PRIMARY, "发布"),
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.MENTIONED, "同页出现"),
            ],
        )
        self.assertEqual(self._roles(ann_id)["GB/T50362-2022"], "mentioned")

    def test_older_target_of_revision_is_demoted(self):
        ann_id = self._persist(
            "GB/T50362-2022",
            [
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.PRIMARY, "发布"),
                Mention("GB/T50362-2005", "GB/T 50362-2005", Role.TARGET_OF_REVISION, "修改为"),
            ],
        )
        self.assertEqual(self._roles(ann_id)["GB/T50362-2005"], "superseded_ref")

    def test_older_primary_is_demoted(self):
        ann_id = self._persist(
            "GB/T50362-2022",
            [
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.PRIMARY, "发布"),
                Mention("GB/T50362-2010", "GB/T 50362-2010", Role.PRIMARY, "同页另一个现批准"),
            ],
        )
        self.assertEqual(self._roles(ann_id)["GB/T50362-2010"], "superseded_ref")

    def test_older_mentioned_is_demoted(self):
        ann_id = self._persist(
            "GB/T50362-2022",
            [
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.PRIMARY, "发布"),
                Mention("GB/T50362-2005", "GB/T 50362-2005", Role.MENTIONED, "同页出现"),
            ],
        )
        self.assertEqual(self._roles(ann_id)["GB/T50362-2005"], "superseded_ref")

    def test_missing_year_is_not_demoted(self):
        ann_id = self._persist(
            "GB50016-2014",
            [
                Mention("GB50016-2014", "GB 50016-2014", Role.PRIMARY, "发布"),
                Mention("GB50016", "GB 50016", Role.MENTIONED, "同页出现"),
            ],
        )
        self.assertEqual(self._roles(ann_id)["GB50016"], "mentioned")

    def test_non_numeric_year_is_not_demoted(self):
        ann_id = self._persist(
            "GB50016-2014",
            [
                Mention("GB50016-2014", "GB 50016-2014", Role.PRIMARY, "发布"),
                Mention("GB50016-20X4", "GB 50016-20X4", Role.MENTIONED, "同页出现"),
            ],
        )
        self.assertEqual(self._roles(ann_id)["GB50016-20X4"], "mentioned")

    def test_same_year_alternate_spelling_is_not_demoted(self):
        # 同年、同族但 /T 写法不同（GB vs GB/T）不是"旧版被代替"。
        ann_id = self._persist(
            "GB50016-2014",
            [
                Mention("GB50016-2014", "GB 50016-2014", Role.PRIMARY, "发布"),
                Mention("GB/T50016-2014", "GB/T 50016-2014", Role.MENTIONED, "同页出现"),
            ],
        )
        self.assertEqual(self._roles(ann_id)["GB/T50016-2014"], "mentioned")

    def test_weak_primary_older_is_not_demoted(self):
        ann_id = self._persist(
            "GB/T50362-2022",
            [
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.PRIMARY, "发布"),
                Mention("GB/T50362-2005", "GB/T 50362-2005", Role.WEAK_PRIMARY, "提及"),
            ],
        )
        self.assertEqual(self._roles(ann_id)["GB/T50362-2005"], "weak_primary")

    def test_ambiguous_codes_do_not_demote(self):
        ann_id = self._persist(
            "GB/T50362-2022",
            [
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.PRIMARY, "发布"),
                Mention("GB/T50362-2005", "GB/T 50362-2005", Role.MENTIONED, "同页出现"),
            ],
            confidence=Confidence.AMBIGUOUS,
        )
        self.assertEqual(self._roles(ann_id)["GB/T50362-2005"], "mentioned")


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


class MentionConfidenceTest(_StoreCase):
    """W2-5：confidence 按公告存，不能被别的公告的 edition 结果污染。"""

    def _persist(self, ann_id: int, primary_key: str | None, mentions,
                 confidence: Confidence) -> None:
        codes = Codes(
            mentions=tuple(mentions),
            primary_key=primary_key,
            primary_display=primary_key,
            confidence=confidence,
        )
        persist_codes(self.store, ann_id, codes, "某标准")
        self.store.commit()

    def _confidence_by_ann(self, rows) -> dict[int, str]:
        return {int(r["id"]): r["confidence"] for r in rows}

    def _parsed_primary(self, ann_id: int) -> None:
        self._persist(
            ann_id, "GB50016-2014",
            [Mention("GB50016-2014", "GB 50016-2014", Role.PRIMARY, "编号为GB 50016-2014")],
            Confidence.PARSED,
        )

    def test_other_announcements_ambiguous_mention_does_not_pollute_parsed(self):
        a = self._ann("https://x/a")
        b = self._ann("https://x/b")
        self._parsed_primary(a)
        self._persist(
            b, None,
            [Mention("GB50016-2014", "GB 50016-2014", Role.MENTIONED, "参考GB 50016-2014")],
            Confidence.AMBIGUOUS,
        )
        self.assertEqual(
            {r["code_key"]: r["confidence"] for r in self.store.mentions_for(a)},
            {"GB50016-2014": "parsed"},
        )
        self.assertEqual(
            {r["code_key"]: r["confidence"] for r in self.store.mentions_for(b)},
            {"GB50016-2014": "ambiguous"},
        )
        # edition 仍是全局表：保持 parsed 优先，但读路径必须按公告区分。
        edition = self.store.db.execute(
            "SELECT confidence FROM edition WHERE code_key='GB50016-2014'"
        ).fetchone()
        self.assertEqual(edition["confidence"], "parsed")

    def test_find_by_code_and_search_use_per_announcement_confidence(self):
        a = self._ann("https://x/a")
        b = self._ann("https://x/b")
        self._parsed_primary(a)
        self._persist(
            b, None,
            [Mention("GB50016-2014", "GB 50016-2014", Role.MENTIONED, "参考GB 50016-2014")],
            Confidence.AMBIGUOUS,
        )
        found = self._confidence_by_ann(self.store.find_by_code("GB50016-2014"))
        self.assertEqual(found[a], "parsed")
        self.assertEqual(found[b], "ambiguous")
        hits, _ = rank(self.store.catalog_entries(), tokens_of("50016"))
        self.assertEqual({int(h.entry["id"]) for h in hits}, {a, b})

    def test_parsed_primary_plus_mentioned_ref(self):
        ann_id = self._ann()
        self._persist(
            ann_id, "GB55037-2022",
            [
                Mention("GB55037-2022", "GB 55037-2022", Role.PRIMARY, "发布"),
                Mention("GB50016-2014", "GB 50016-2014", Role.MENTIONED, "参考"),
            ],
            Confidence.PARSED,
        )
        rows = {r["code_key"]: r for r in self.store.mentions_for(ann_id)}
        self.assertEqual(rows["GB55037-2022"]["confidence"], "parsed")
        self.assertEqual(rows["GB50016-2014"]["confidence"], "ambiguous")

    def test_ambiguous_rescan_downgrades_only_that_announcement(self):
        a = self._ann("https://x/a")
        b = self._ann("https://x/b")
        self._parsed_primary(a)
        self._parsed_primary(b)
        self._persist(
            b, "GB50016-2014",
            [Mention("GB50016-2014", "GB 50016-2014", Role.PRIMARY, "编号为GB 50016-2014")],
            Confidence.AMBIGUOUS,
        )
        self.assertEqual(
            {r["code_key"]: r["confidence"] for r in self.store.mentions_for(a)},
            {"GB50016-2014": "parsed"},
        )
        self.assertEqual(
            {r["code_key"]: r["confidence"] for r in self.store.mentions_for(b)},
            {"GB50016-2014": "ambiguous"},
        )

    def test_add_mention_confidence_conflict_keeps_or_replaces(self):
        ann_id = self._ann()
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(ann_id, "GB50016-2014", "primary", "ev", confidence="parsed")
        self.store.commit()
        # 不带 confidence 的旧调用不抹掉已有按公告置信度
        self.store.add_mention(ann_id, "GB50016-2014", "primary", "ev2")
        self.store.commit()
        self.assertEqual(self.store.mentions_for(ann_id)[0]["confidence"], "parsed")
        # 显式新值覆盖
        self.store.add_mention(ann_id, "GB50016-2014", "mentioned", "ev3", confidence="ambiguous")
        self.store.commit()
        self.assertEqual(self.store.mentions_for(ann_id)[0]["confidence"], "ambiguous")

    def test_writable_legacy_db_adds_column_and_falls_back_to_edition(self):
        path = self.dir / "legacy.db"
        _write_legacy_v2(path)
        store = Store(path)
        self.addCleanup(store.close)
        columns = [r["name"] for r in store.db.execute("PRAGMA table_info(mention)")]
        self.assertIn("confidence", columns)
        # 老行 m.confidence IS NULL → COALESCE 回退 edition.confidence
        rows = store.mentions_for(1)
        self.assertEqual(rows[0]["confidence"], "parsed")
        store.add_mention(2, "GB50016-2014", "primary", "ev")
        self.assertEqual(store.mentions_for(2)[0]["confidence"], "parsed")

    def test_read_only_legacy_db_without_column_is_readable(self):
        path = self.dir / "ro-legacy.db"
        _write_legacy_v2(path)
        path.chmod(0o444)
        self.dir.chmod(0o555)
        self.addCleanup(self.dir.chmod, 0o755)
        self.addCleanup(path.chmod, 0o644)
        store = Store(path, read_only=True)
        self.addCleanup(store.close)
        rows = store.mentions_for(1)
        self.assertEqual(rows[0]["confidence"], "parsed")
        columns = [r["name"] for r in store.db.execute("PRAGMA table_info(mention)")]
        self.assertNotIn("confidence", columns)
        self.assertIsNotNone(store.find_by_code("GB50016-2014"))
        hits, _ = rank(store.catalog_entries(), tokens_of("50016"))
        self.assertEqual([int(h.entry["id"]) for h in hits], [1])
        self.assertEqual(store.find_by_code("GB50016-2014")[0]["confidence"], "parsed")


class EditionStatusTest(_StoreCase):
    """W2-6：persist_codes/upsert_edition 真正写 status，且不把已知值洗成 unknown。"""

    def _status(self, key: str) -> str:
        row = self.store.db.execute(
            "SELECT status FROM edition WHERE code_key=?", (key,)
        ).fetchone()
        self.assertIsNotNone(row)
        return row["status"]

    def test_current_and_withdrawn_are_persisted(self):
        self.store.upsert_edition("GB1-2020", "GB 1-2020", None, "parsed", status="current")
        self.store.upsert_edition("GB2-2020", "GB 2-2020", None, "parsed", status="withdrawn")
        self.store.commit()
        self.assertEqual(self._status("GB1-2020"), "current")
        self.assertEqual(self._status("GB2-2020"), "withdrawn")

    def test_none_empty_and_unknown_do_not_overwrite_known(self):
        self.store.upsert_edition("GB1-2020", "GB 1-2020", None, "parsed", status="current")
        for value in (None, "", "unknown"):
            self.store.upsert_edition("GB1-2020", "GB 1-2020", None, "parsed", status=value)
            self.assertEqual(self._status("GB1-2020"), "current")
        self.store.commit()

    def test_insert_with_none_satisfies_not_null(self):
        self.store.upsert_edition("GB1-2020", "GB 1-2020", None, "parsed", status=None)
        self.store.commit()
        self.assertEqual(self._status("GB1-2020"), "unknown")

    def test_persist_codes_status_only_applies_to_primary(self):
        ann_id = self._ann()
        codes = Codes(
            mentions=(
                Mention("GB1-2020", "GB 1-2020", Role.PRIMARY, "发布"),
                Mention("GB0-2010", "GB 0-2010", Role.TARGET_OF_REVISION, "修改为"),
            ),
            primary_key="GB1-2020",
            primary_display="GB 1-2020",
            confidence=Confidence.PARSED,
        )
        persist_codes(self.store, ann_id, codes, "某标准", status="current")
        self.store.commit()
        self.assertEqual(self._status("GB1-2020"), "current")
        self.assertEqual(self._status("GB0-2010"), "unknown")

    def test_persist_codes_default_none_does_not_overwrite(self):
        ann_id = self._ann()
        self.store.upsert_edition("GB1-2020", "GB 1-2020", None, "parsed", status="withdrawn")
        codes = Codes(
            mentions=(Mention("GB1-2020", "GB 1-2020", Role.PRIMARY, "发布"),),
            primary_key="GB1-2020",
            primary_display="GB 1-2020",
            confidence=Confidence.PARSED,
        )
        persist_codes(self.store, ann_id, codes, "某标准")
        self.store.commit()
        self.assertEqual(self._status("GB1-2020"), "withdrawn")


if __name__ == "__main__":
    unittest.main()
