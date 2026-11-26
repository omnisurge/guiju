"""存储层：schema 初始化、迁移、原子 upsert、mentions 持久化、年号降级与置信度隔离。"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from guiju.domain.codes import Codes, Confidence, Mention, Role
from guiju.domain.search import rank, tokens_of
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


class CatalogMetaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.addCleanup(self.store.close)
        self.store.upsert_announcement(
            "https://www.mohurd.gov.cn/x.html", "2021-07-15",
            "标题", "工程结构通用规范", "national",
        )

    def test_upsert_announcement_counts(self):
        self.assertEqual(self.store.count_announcements(), 1)

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
        self.assertIsNone(self.store.catalog_age_days())  # 从未同步
        old = datetime.now() - timedelta(days=9)
        self.store.set_meta("synced_at", old.strftime("%Y-%m-%d %H:%M:%S"))
        self.assertAlmostEqual(self.store.catalog_age_days(), 9.0, delta=0.01)
        self.store.set_meta("synced_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.assertLess(self.store.catalog_age_days(), 0.01)


class StoreBaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)

    def test_upsert_and_search_baseline(self):
        ann_id, fresh = self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "防火规范", "建筑设计防火规范", "standard"
        )
        self.assertTrue(fresh)
        self.assertGreater(ann_id, 0)
        rows, _ = rank(self.store.catalog_entries(), tokens_of("防火"))
        self.assertEqual(len(rows), 1)
        same_id, fresh2 = self.store.upsert_announcement(
            "https://x/1", "2026-02-01", "新防火规范", "建筑设计防火规范", "standard"
        )
        self.assertEqual(same_id, ann_id)
        self.assertFalse(fresh2)


class StoreW4Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "catalog.db"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def test_concurrent_same_url_upsert_is_atomic(self):
        errors = []
        barrier = threading.Barrier(4)

        def worker(n):
            try:
                store = Store(self.path)
                try:
                    for round_no in range(20):
                        barrier.wait()
                        store.upsert_announcement(
                            "https://x/same",
                            "2026-01-01",
                            f"标题{n}-{round_no}",
                            "名称",
                            "other",
                        )
                        store.commit()
                finally:
                    store.close()
            except Exception as exc:  # 捕捉以便断言零异常
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.store.count_announcements(), 1)

    def test_upsert_keeps_id_and_first_fetched_at(self):
        ann_id, fresh = self.store.upsert_announcement(
            "https://x/1", "2020-01-01", "旧", "旧", "other"
        )
        self.assertTrue(fresh)
        fetched = self.store.get_ann(ann_id)["fetched_at"]
        same, fresh2 = self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "新", "新", "standard"
        )
        self.assertEqual(same, ann_id)
        self.assertFalse(fresh2)
        self.assertEqual(self.store.get_ann(ann_id)["fetched_at"], fetched)
        self.assertEqual(self.store.get_ann(ann_id)["title"], "新")

    def test_find_by_code_wildcard_is_literal(self):
        ann_id, _ = self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "标题", "名称", "standard"
        )
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(ann_id, "GB50016-2014", "primary", "发布")
        self.store.commit()
        self.assertEqual(self.store.find_by_code("GB%"), [])
        self.assertEqual(self.store.find_by_code("GB50016_2014"), [])
        self.assertEqual(len(self.store.find_by_code("GB50016")), 1)

    def test_search_wildcard_is_literal(self):
        self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "防火规范", "建筑设计防火规范", "standard"
        )
        self.store.commit()
        self.assertFalse(hasattr(self.store, "search"))
        entries = self.store.catalog_entries()
        self.assertEqual(rank(entries, tokens_of("%"))[0], [])
        self.assertEqual(len(rank(entries, tokens_of("防火"))[0]), 1)


class AnnouncementUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)

    def test_update_keeps_id_and_first_fetched_at(self):
        ann_id, fresh = self.store.upsert_announcement(
            "https://x/1", "2020-01-01", "旧标题", "旧名称", "other"
        )
        self.assertTrue(fresh)
        old_fetched = self.store.get_ann(ann_id)["fetched_at"]
        same_id, fresh2 = self.store.upsert_announcement(
            "https://x/1", "2026-02-02", "新标题", "新名称", "standard"
        )
        row = self.store.get_ann(ann_id)
        self.assertFalse(fresh2)
        self.assertEqual(same_id, ann_id)
        self.assertEqual(row["title"], "新标题")
        self.assertEqual(row["doc_name"], "新名称")
        self.assertEqual(row["published_on"], "2026-02-02")
        self.assertEqual(row["category"], "standard")
        self.assertEqual(row["fetched_at"], old_fetched)


class PersistCodesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "catalog.db"))
        self.addCleanup(self.store.close)
        self.ann_id, _ = self.store.upsert_announcement(
            "https://x/1", "2026-01-01", "标题", "名称", "other"
        )

    def _codes(self, primary, mentions):
        return Codes(
            mentions=tuple(mentions),
            primary_key=primary,
            primary_display=primary,
            confidence=Confidence.PARSED,
        )

    def test_rescan_replaces_mentions(self):
        first = self._codes(
            "GB50016-2014",
            [Mention("GB50016-2014", "GB 50016-2014", Role.PRIMARY, "发布")],
        )
        persist_codes(self.store, self.ann_id, first, "名称")
        second = self._codes(
            "GB50011-2010",
            [Mention("GB50011-2010", "GB 50011-2010", Role.PRIMARY, "发布")],
        )
        persist_codes(self.store, self.ann_id, second, "名称")
        keys = [r["code_key"] for r in self.store.mentions_for(self.ann_id)]
        self.assertEqual(keys, ["GB50011-2010"])

    def test_unrelated_mention_is_not_demoted(self):
        codes = self._codes(
            "GB55037-2022",
            [
                Mention("GB55037-2022", "GB 55037-2022", Role.PRIMARY, "发布"),
                Mention("GB50016-2014", "GB 50016-2014", Role.MENTIONED, "参考"),
            ],
        )
        persist_codes(self.store, self.ann_id, codes, "名称")
        rows = {r["code_key"]: r for r in self.store.mentions_for(self.ann_id)}
        self.assertEqual(rows["GB50016-2014"]["role"], "mentioned")
        self.assertEqual(rows["GB55037-2022"]["role"], "primary")

    def test_same_family_older_mention_is_demoted(self):
        codes = self._codes(
            "GB/T50362-2022",
            [
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.PRIMARY, "发布"),
                Mention("GB/T50362-2005", "GB/T 50362-2005", Role.MENTIONED, "同页出现"),
            ],
        )
        persist_codes(self.store, self.ann_id, codes, "名称")
        rows = {r["code_key"]: r for r in self.store.mentions_for(self.ann_id)}
        self.assertEqual(rows["GB/T50362-2005"]["role"], "superseded_ref")

    def test_ambiguous_target_is_not_persisted_as_parsed(self):
        codes = Codes(
            mentions=(
                Mention("GB/T50362-2022", "GB/T 50362-2022", Role.TARGET_OF_REVISION, "修改为"),
                Mention("GB/T50378-2019", "GB/T 50378-2019", Role.TARGET_OF_REVISION, "修改为"),
            ),
            confidence=Confidence.AMBIGUOUS,
        )
        persist_codes(self.store, self.ann_id, codes, "名称")
        rows = {r["code_key"]: r for r in self.store.mentions_for(self.ann_id)}
        self.assertEqual(rows["GB/T50362-2022"]["role"], "target_of_revision")
        self.assertEqual(rows["GB/T50378-2019"]["role"], "target_of_revision")
        conf = {
            r["code_key"]: r["confidence"]
            for r in self.store.db.execute("SELECT code_key,confidence FROM edition")
        }
        self.assertEqual(conf["GB/T50362-2022"], "ambiguous")
        self.assertEqual(conf["GB/T50378-2019"], "ambiguous")

    def test_non_primary_confidence_follows_evidence(self):
        codes = self._codes(
            "GB55037-2022",
            [
                Mention("GB55037-2022", "GB 55037-2022", Role.PRIMARY, "发布"),
                Mention("GB50016-2014", "GB 50016-2014", Role.MENTIONED, "参考"),
                Mention("JGJ147-2016", "JGJ 147-2016", Role.WEAK_PRIMARY, "提及"),
                Mention("CJ/T133-2007", "CJ/T 133-2007", Role.SUPERSEDED_REF, "废止"),
            ],
        )
        persist_codes(self.store, self.ann_id, codes, "名称")
        rows = {
            r["code_key"]: r
            for r in self.store.db.execute("SELECT code_key,confidence FROM edition")
        }
        self.assertEqual(rows["GB55037-2022"]["confidence"], "parsed")
        self.assertEqual(rows["GB50016-2014"]["confidence"], "ambiguous")
        self.assertEqual(rows["JGJ147-2016"]["confidence"], "ambiguous")
        self.assertEqual(rows["CJ/T133-2007"]["confidence"], "parsed")


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
