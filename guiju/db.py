"""本地目录库（SQLite）。

三张主表：
  announcements  公告目录（标题级，来自列表接口，3254+ 条）
  codes          编号索引（编号级，需解析详情页，惰性建立）
  files          本地归档（文件级，含 sha256 与来源链）

设计要点：目录与编号**分层**。列表接口只给标题，不给编号；
编号必须开详情页才拿得到。所以 codes 是一张"按需填充"的表，
而不是同步时必须付出的成本。
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    url     TEXT NOT NULL UNIQUE,
    date    TEXT,
    title   TEXT NOT NULL,
    name    TEXT,
    kind    TEXT,
    seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_date ON announcements(date);
CREATE INDEX IF NOT EXISTS idx_ann_name ON announcements(name);

CREATE TABLE IF NOT EXISTS codes (
    key         TEXT NOT NULL,   -- 归一化：GB55037-2022（无空格大写）
    code        TEXT NOT NULL,   -- 展示用：GB 55037-2022
    ann_id      INTEGER NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY (key, ann_id)
);
CREATE INDEX IF NOT EXISTS idx_codes_key ON codes(key);

CREATE TABLE IF NOT EXISTS files (
    code       TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    bytes      INTEGER,
    ann_id     INTEGER,
    src_url    TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

-- 已扫过详情页的公告。用于避免"正文无编号"的公告被反复重抓。
CREATE TABLE IF NOT EXISTS scanned (
    ann_id     INTEGER PRIMARY KEY,
    scanned_at TEXT
);
"""

TITLE_NAME = re.compile(r"《([^》]+)》")

_KIND_RULES = (
    ("局部修订", "局部修订"),
    ("征求意见", "征求意见"),
    ("国家标准", "国家标准"),
    ("行业标准", "行业标准"),
    ("地方标准", "地方标准"),
    ("团体标准", "团体标准"),
)


def parse_title(title: str) -> tuple[str, str]:
    """从公告标题里拆出规范名与类别。"""
    match = TITLE_NAME.search(title)
    name = match.group(1).strip() if match else title.strip()
    kind = "其他"
    for needle, label in _KIND_RULES:
        if needle in title:
            kind = label
            break
    return name, kind


def default_home() -> Path:
    return Path(os.environ.get("GUIJU_HOME", Path.home() / ".guiju"))


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path: Path | str | None = None) -> None:
        if path is None:
            home = default_home()
            home.mkdir(parents=True, exist_ok=True)
            path = home / "catalog.db"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---------- announcements ----------

    def upsert_announcement(self, url: str, date: str, title: str) -> bool:
        """插入一条公告。返回 True 表示新条目。"""
        name, kind = parse_title(title)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO announcements(url,date,title,name,kind,seen_at) "
            "VALUES(?,?,?,?,?,?)",
            (url, date, title, name, kind, now()),
        )
        return cur.rowcount > 0

    def has_url(self, url: str) -> bool:
        row = self.db.execute("SELECT 1 FROM announcements WHERE url=?", (url,)).fetchone()
        return row is not None

    def count_announcements(self) -> int:
        return self.db.execute("SELECT COUNT(*) c FROM announcements").fetchone()["c"]

    def search(self, keywords: list[str], limit: int = 20) -> list[sqlite3.Row]:
        if not keywords:
            return []
        where, params = [], []
        for kw in keywords:
            where.append("(title LIKE ? OR name LIKE ?)")
            params += [f"%{kw}%", f"%{kw}%"]
        sql = (
            "SELECT * FROM announcements WHERE " + " AND ".join(where) +
            " ORDER BY date DESC LIMIT ?"
        )
        return self.db.execute(sql, (*params, limit)).fetchall()

    def get_ann(self, ann_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()

    def get_ann_by_url(self, url: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM announcements WHERE url=?", (url,)).fetchone()

    def anns_without_code(self, match: str | None, limit: int) -> list[sqlite3.Row]:
        sql = (
            "SELECT * FROM announcements a WHERE "
            "NOT EXISTS (SELECT 1 FROM codes c WHERE c.ann_id=a.id) AND "
            "NOT EXISTS (SELECT 1 FROM scanned s WHERE s.ann_id=a.id)"
        )
        params: list = []
        if match:
            sql += " AND (a.title LIKE ? OR a.name LIKE ?)"
            params += [f"%{match}%", f"%{match}%"]
        sql += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        return self.db.execute(sql, params).fetchall()

    def mark_scanned(self, ann_id: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO scanned(ann_id,scanned_at) VALUES(?,?)", (ann_id, now())
        )

    # ---------- codes ----------

    def add_code(self, key: str, code: str, ann_id: int) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO codes(key,code,ann_id,resolved_at) VALUES(?,?,?,?)",
            (key, code, ann_id, now()),
        )

    def find_codes(self, key: str) -> list[sqlite3.Row]:
        rows = self.db.execute(
            "SELECT c.key,c.code,c.ann_id,a.name,a.title,a.date,a.url,a.kind "
            "FROM codes c JOIN announcements a ON a.id=c.ann_id WHERE c.key=?",
            (key,),
        ).fetchall()
        if rows:
            return rows
        # 模糊：允许只给数字（如 55037）
        return self.db.execute(
            "SELECT c.key,c.code,c.ann_id,a.name,a.title,a.date,a.url,a.kind "
            "FROM codes c JOIN announcements a ON a.id=c.ann_id "
            "WHERE c.key LIKE ? ORDER BY a.date DESC LIMIT 20",
            (f"%{key}%",),
        ).fetchall()

    def codes_for_ann(self, ann_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT key,code FROM codes WHERE ann_id=? ORDER BY key", (ann_id,)
        ).fetchall()

    def count_codes(self) -> int:
        return self.db.execute("SELECT COUNT(DISTINCT key) c FROM codes").fetchone()["c"]

    # ---------- files ----------

    def record_file(self, code: str, path: str, sha256: str, size: int,
                    ann_id: int, src_url: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO files(code,path,sha256,bytes,ann_id,src_url,fetched_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (code, path, sha256, size, ann_id, src_url, now()),
        )

    def get_file(self, code: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM files WHERE code=?", (code,)).fetchone()

    def get_file_by_path(self, path: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()

    def all_files(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM files ORDER BY code").fetchall()

    # ---------- meta ----------

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (key, value))

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()
