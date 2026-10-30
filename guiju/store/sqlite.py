"""SQLite 存储 —— 带 schema 版本与迁移。

旧版（2026-10-30 之前的 cli.py/db.py）库为 user_version=0 且表结构不同。
本模块的策略：识别到旧库即**改名备份**后重建，绝不半迁移（旧库只有 0 条生产数据，
半迁移带来的隐性错位比重建贵得多）。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..domain.codes import Codes
from ..errors import GuijuError

SCHEMA_VERSION = 2
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def default_home() -> Path:
    return Path(os.environ.get("GUIJU_HOME") or (Path.home() / ".guiju"))


class Store:
    def __init__(self, path: str | Path | None = None) -> None:
        home = None
        if path is None:
            home = default_home()
            home.mkdir(parents=True, exist_ok=True)
            path = home / "catalog.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy_backup: Path | None = None
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # ---------------------------------------------------------------- schema
    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 0 and self._has_tables():
            backup = self.path.with_name(f"{self.path.name}.legacy-{now().replace(':', '').replace(' ', '-')}")
            self.db.close()
            shutil.copy2(self.path, backup)
            self.path.unlink()
            self.legacy_backup = backup
            self.db = sqlite3.connect(self.path)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
        elif version not in (0, SCHEMA_VERSION):
            raise GuijuError(f"数据库 schema 版本 {version} 超出本程序认知（{SCHEMA_VERSION}）")
        self.db.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    def _has_tables(self) -> bool:
        row = self.db.execute(
            "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        return bool(row["c"])

    # ---------------------------------------------------------------- announcement
    def upsert_announcement(
        self, url: str, published_on: str, title: str, doc_name: str, category: str
    ) -> tuple[int, bool]:
        cur = self.db.execute(
            "INSERT INTO announcement(url,published_on,title,doc_name,category,fetched_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO NOTHING",
            (url, published_on, title, doc_name, category, now()),
        )
        is_new = cur.rowcount > 0
        if not is_new:
            row = self.db.execute("SELECT id FROM announcement WHERE url=?", (url,)).fetchone()
            return int(row["id"]), False
        return int(cur.lastrowid or 0), True

    def get_ann(self, ann_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM announcement WHERE id=?", (ann_id,)).fetchone()

    def get_ann_by_url(self, url: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM announcement WHERE url=?", (url,)).fetchone()

    def count_announcements(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) c FROM announcement").fetchone()["c"])

    def max_published_on(self) -> str:
        row = self.db.execute("SELECT MAX(published_on) d FROM announcement").fetchone()
        return row["d"] or ""

    def search(self, keywords: list[str], limit: int = 20, status: str | None = None) -> list[sqlite3.Row]:
        if not keywords:
            return []
        where, params = [], []
        for kw in keywords:
            where.append("(a.title LIKE ? OR a.doc_name LIKE ? OR e.code_key LIKE ?)")
            like = f"%{kw}%"
            params += [like, like, f"%{kw.upper().replace(' ', '')}%"]
        if status:
            where.append("e.status = ?")
            params.append(status)
        sql = (
            "SELECT a.*, e.code_key, e.code_display, e.status AS code_status, e.confidence,"
            "       m.role, m.evidence"
            " FROM announcement a"
            " LEFT JOIN mention m ON m.ann_id = a.id"
            " LEFT JOIN edition e ON e.code_key = m.code_key"
            " WHERE " + " AND ".join(where) +
            " ORDER BY a.published_on DESC LIMIT ?"
        )
        return self.db.execute(sql, (*params, limit)).fetchall()

    def announcements_without_mention(self, match: str | None, limit: int) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM announcement a WHERE "
            "NOT EXISTS (SELECT 1 FROM scan s WHERE s.ann_id = a.id)"
        )
        params: list = []
        if match:
            sql += " AND (a.title LIKE ? OR a.doc_name LIKE ?)"
            params += [f"%{match}%", f"%{match}%"]
        sql += " ORDER BY a.published_on DESC LIMIT ?"
        params.append(limit)
        return self.db.execute(sql, params).fetchall()

    # ---------------------------------------------------------------- edition / mention
    def upsert_edition(self, key: str, display: str, title: str | None, confidence: str) -> None:
        self.db.execute(
            "INSERT INTO edition(code_key,code_display,title,confidence) VALUES(?,?,?,?) "
            "ON CONFLICT(code_key) DO UPDATE SET confidence=excluded.confidence,"
            " title=COALESCE(edition.title, excluded.title)",
            (key, display, title, confidence),
        )

    def add_mention(self, ann_id: int, code_key: str, role: str, evidence: str) -> None:
        self.db.execute(
            "INSERT INTO mention(ann_id,code_key,role,evidence) VALUES(?,?,?,?) "
            "ON CONFLICT(ann_id,code_key) DO UPDATE SET role=excluded.role, evidence=excluded.evidence",
            (ann_id, code_key, role, evidence),
        )

    def mark_scanned(self, ann_id: int, codes_found: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO scan(ann_id,scanned_at,codes_found) VALUES(?,?,?)",
            (ann_id, now(), codes_found),
        )

    def mentions_for(self, ann_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT m.code_key, m.role, m.evidence, e.code_display, e.status, e.confidence"
            " FROM mention m LEFT JOIN edition e ON e.code_key = m.code_key"
            " WHERE m.ann_id = ? ORDER BY m.role, m.code_key",
            (ann_id,),
        ).fetchall()

    def find_by_code(self, key: str) -> list[sqlite3.Row]:
        exact = self.db.execute(
            "SELECT m.code_key, m.role, m.evidence, e.code_display, e.status, e.confidence, a.*"
            " FROM mention m JOIN announcement a ON a.id = m.ann_id"
            " LEFT JOIN edition e ON e.code_key = m.code_key"
            " WHERE m.code_key = ? ORDER BY a.published_on DESC",
            (key,),
        ).fetchall()
        if exact:
            return exact
        return self.db.execute(
            "SELECT m.code_key, m.role, m.evidence, e.code_display, e.status, e.confidence, a.*"
            " FROM mention m JOIN announcement a ON a.id = m.ann_id"
            " LEFT JOIN edition e ON e.code_key = m.code_key"
            " WHERE m.code_key LIKE ? ORDER BY a.published_on DESC LIMIT 20",
            (f"%{key}%",),
        ).fetchall()

    # ---------------------------------------------------------------- artifact
    def record_artifact(
        self, *, ann_id: int, code_key: str | None, source: str, page_url: str, file_url: str,
        label: str, sha256: str, size: int, magic: str, local_path: str, verdict: str = "ok",
    ) -> None:
        self.db.execute(
            "INSERT INTO artifact(ann_id,code_key,source,page_url,file_url,label,sha256,bytes,"
            "magic,verdict,local_path,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(sha256) DO UPDATE SET local_path=excluded.local_path, verdict=excluded.verdict",
            (ann_id, code_key, source, page_url, file_url, label, sha256, size, magic,
             verdict, local_path, now()),
        )

    def artifact_by_code(self, code_key: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM artifact WHERE code_key = ? ORDER BY fetched_at DESC LIMIT 1", (code_key,)
        ).fetchone()

    def artifact_by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM artifact WHERE sha256 = ?", (sha256,)).fetchone()

    def all_artifacts(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM artifact ORDER BY code_key, id").fetchall()

    # ---------------------------------------------------------------- probe / meta
    def log_probe(self, source: str, url: str, status: int | None, size: int | None,
                  outcome: str, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO probe(source,url,status,bytes,outcome,note,at) VALUES(?,?,?,?,?,?,?)",
            (source, url, status, size, outcome, note, now()),
        )

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO corpus_meta(k,v) VALUES(?,?)", (key, value))

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM corpus_meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        q = self.db.execute
        by_year = q(
            "SELECT substr(published_on,1,4) y, COUNT(*) c FROM announcement"
            " WHERE published_on IS NOT NULL GROUP BY y ORDER BY y"
        ).fetchall()
        return {
            "announcements": self.count_announcements(),
            "editions": q("SELECT COUNT(*) c FROM edition").fetchone()["c"],
            "mentions": q("SELECT COUNT(*) c FROM mention").fetchone()["c"],
            "scanned": q("SELECT COUNT(*) c FROM scan").fetchone()["c"],
            "artifacts": q("SELECT COUNT(*) c FROM artifact").fetchone()["c"],
            "by_year": {r["y"]: r["c"] for r in by_year},
            "synced_at": self.get_meta("synced_at"),
        }

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def persist_codes(store: Store, ann_id: int, codes: Codes, doc_name: str, status: str = "unknown") -> None:
    """把一次 classify 的结果落库（edition + mention + scan）。"""
    if codes.primary_key:
        store.upsert_edition(codes.primary_key, codes.primary_display or codes.primary_key,
                             doc_name, codes.confidence.value)
    for m in codes.mentions:
        if m.code_key != codes.primary_key:
            store.upsert_edition(m.code_key, m.code_display, None, "parsed")
        role = m.role.value
        if codes.primary_key and m.code_key != codes.primary_key and m.role.value == "mentioned":
            role = "superseded_ref"
        store.add_mention(ann_id, m.code_key, role, m.evidence)
    store.mark_scanned(ann_id, len(codes.mentions))
