"""SQLite 存储 —— 带 schema 版本与迁移。

旧版（2026-10-30 之前的 cli.py/db.py）库为 user_version=0 且表结构不同。
本模块的策略：识别到旧库即**改名备份**后重建，绝不半迁移（旧库只有 0 条生产数据，
半迁移带来的隐性错位比重建贵得多）。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from ..domain.codes import Codes, Confidence, Role, _code_stem
from ..errors import GuijuError, NeedsHuman, SourceUnreachable

SCHEMA_VERSION = 2
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def default_home() -> Path:
    return Path(os.environ.get("GUIJU_HOME") or (Path.home() / ".guiju"))


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class Store:
    def __init__(self, path: str | Path | None = None, *, cross_thread: bool = False) -> None:
        # cross_thread=True：MCP 这类共享连接的场景放开 check_same_thread；
        # CPython sqlite3 threadsafety=1，共享后调用方必须自己串行化访问。
        home = None
        if path is None:
            home = default_home()
            path = home / "catalog.db"
        self.path = Path(path)
        self.legacy_backup: Path | None = None
        self._cross_thread = cross_thread
        # 文件或父目录不可写时，用 SQLite 的 mode=ro 打开；否则 WAL 的连接
        # 初始化会尝试写 -wal/-shm，只读目录下连 SELECT 都打不开。
        self._read_only = self.path.exists() and (
            not os.access(self.path, os.W_OK)
            or not os.access(self.path.parent, os.W_OK)
        )
        self._read_only_dir = self.path.exists() and not os.access(self.path.parent, os.W_OK)
        ok = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._read_only:
                uri = self.path.resolve().as_uri() + "?mode=ro"
                if self._read_only_dir:
                    # 只读目录里没有可写 -shm/-wal；immutable 跳过 SQLite 的
                    # 共享内存/锁文件初始化，读已 checkpoint 的库才是安全的。
                    uri += "&immutable=1"
                self.db = sqlite3.connect(
                    uri, uri=True, check_same_thread=not cross_thread)
            else:
                self.db = sqlite3.connect(
                    self.path, check_same_thread=not cross_thread)
            self.db.row_factory = sqlite3.Row
            self._configure_connection()
            self._migrate()
            self._ensure_additions()
            ok = True
        except sqlite3.OperationalError as exc:
            # 初始化/迁移期的瞬时锁是可重试的 20，不是“数据库损坏”的 50；
            # 只读父目录等永久写失败仍归 50 交人处理。
            if _is_locked(exc):
                raise SourceUnreachable(
                    f"数据库正被其他进程占用：{self.path}（{exc}）",
                    hint="稍后重试一次；若持续出现，检查是否有另一个 guiju 进程在长期写库",
                ) from exc
            raise NeedsHuman(
                f"数据库无法打开或已损坏：{self.path}（{type(exc).__name__}: {exc}）",
                hint="先备份该文件，再把它移走让 guiju 重建目录；原始文件保留用于排查",
            ) from exc
        except (sqlite3.DatabaseError, OSError) as exc:
            # 打开/建库/迁移阶段的损坏库、权限、只读父目录、锁：不能让 CLI
            # 以裸 traceback 结束，也不能假装"目录为空"继续查。给出可操作的 50。
            raise NeedsHuman(
                f"数据库无法打开或已损坏：{self.path}（{type(exc).__name__}: {exc}）",
                hint="先备份该文件，再把它移走让 guiju 重建目录；原始文件保留用于排查",
            ) from exc
        finally:
            if not ok:
                # 包括 GuijuError（未来 schema）和 KeyboardInterrupt：
                # __init__ 失败时不能把连接留给 GC，否则长跑 MCP 会泄漏。
                db = getattr(self, "db", None)
                with contextlib.suppress(Exception):
                    if db is not None:
                        db.close()

    # ---------------------------------------------------------------- schema
    def _configure_connection(self) -> None:
        """连接级设置：busy_timeout 让并发写等待而不是立刻 InternalError；
        WAL 提升读写并发。只读旧库上 WAL 可能被拒绝，读取仍应可用。"""
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA busy_timeout = 5000")
        if not self._read_only:
            # 只读库/只读目录不切换 journal mode，否则 SQLite 会在连接初始化时
            # 尝试写 -wal/-shm，连只读 SELECT 都被拖成 50。
            with contextlib.suppress(sqlite3.OperationalError):
                self.db.execute("PRAGMA journal_mode = WAL")

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 0 and self._has_tables():
            if self._read_only:
                raise NeedsHuman(
                    f"只读旧库无法迁移到当前 schema：{self.path}",
                    hint="把数据库复制到可写目录后重新打开，或让 guiju 在可写 GUIJU_HOME 重建",
                )
            backup = self.path.with_name(f"{self.path.name}.legacy-{now().replace(':', '').replace(' ', '-')}")
            self.db.close()
            shutil.copy2(self.path, backup)
            self.path.unlink()
            self.legacy_backup = backup
            self.db = sqlite3.connect(
                self.path, check_same_thread=not self._cross_thread)
            self.db.row_factory = sqlite3.Row
            self._configure_connection()
        elif version not in (0, SCHEMA_VERSION):
            raise GuijuError(f"数据库 schema 版本 {version} 超出本程序认知（{SCHEMA_VERSION}）")
        self.db.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    def _ensure_additions(self) -> None:
        """只做"加法"的结构变更：已有库升级到本版本时补表，绝不动已有数据。

        为什么不上提 schema 版本：缓存表是完全可丢弃的派生数据，
        把它算进版本迁移会让"版本对了但少张表"变成一类新故障。
        """
        if self._read_only:
            # 只读旧库即使缺 cache 表也不能补 DDL；读命令继续可用，写缓存时
            # 由调用方得到明确的只读写失败（CLI 映射 50）。
            return
        exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='currency_cache'"
        ).fetchone()
        if exists is not None:
            # 不重复执行 DDL：只读的既有 v2 库不应因为每次构造都写 schema 而失败。
            return
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS currency_cache ("
            "code_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
        )
        self.db.commit()

    def cache_currency(self, code_key: str, payload: str) -> None:
        self.db.execute(
            "INSERT INTO currency_cache(code_key,payload,fetched_at) VALUES(?,?,?) "
            "ON CONFLICT(code_key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at",
            (code_key, payload, now()),
        )
        self.db.commit()

    def cached_currency(self, code_key: str) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT payload, fetched_at FROM currency_cache WHERE code_key=?", (code_key,)
        ).fetchone()
        return (row["payload"], row["fetched_at"]) if row else None

    def _has_tables(self) -> bool:
        row = self.db.execute(
            "SELECT COUNT(*) c FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()
        return bool(row["c"])

    # ---------------------------------------------------------------- announcement
    def upsert_announcement(
        self, url: str, published_on: str, title: str, doc_name: str, category: str
    ) -> tuple[int, bool]:
        """按 URL 幂等写入；已存在时更新可变的展示字段，保留 id 与首次 fetched_at。

        旧实现是 INSERT OR IGNORE：标题/分类/日期永远停留在首抓值，修好解析器也修不到
        存量。这里只更新事实字段，不改 fetched_at（它表示首次抓取时间）；id 不变，
        已有 mention 的引用关系不会被破坏。
        """
        row = self.db.execute("SELECT id FROM announcement WHERE url=?", (url,)).fetchone()
        if row is not None:
            self.db.execute(
                "UPDATE announcement SET published_on=?, title=?, doc_name=?, category=? WHERE id=?",
                (published_on, title, doc_name, category, int(row["id"])),
            )
            return int(row["id"]), False
        cur = self.db.execute(
            "INSERT INTO announcement(url,published_on,title,doc_name,category,fetched_at) "
            "VALUES(?,?,?,?,?,?)",
            (url, published_on, title, doc_name, category, now()),
        )
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

    def catalog_entries(self) -> list[dict]:
        """把目录（含编号）读进内存供检索打分。数据量 3k 级，不值得建全文索引。"""
        anns = self.db.execute(
            "SELECT id,url,published_on,title,doc_name,category FROM announcement"
        ).fetchall()
        codes: dict[int, list[str]] = {}
        # mention 只存角色，展示用编号在 edition 表
        for row in self.db.execute(
            "SELECT m.ann_id, e.code_display, m.code_key, m.role"
            " FROM mention m LEFT JOIN edition e ON e.code_key = m.code_key"
            " ORDER BY m.role"
        ):
            codes.setdefault(int(row["ann_id"]), []).append(row["code_display"] or row["code_key"])
        return [
            {
                "id": int(a["id"]),
                "url": a["url"],
                "published_on": a["published_on"],
                "name": a["doc_name"],
                "title": a["title"],
                "category": a["category"],
                "codes": codes.get(int(a["id"]), []),
            }
            for a in anns
        ]

    # ---------------------------------------------------------------- edition / mention
    def upsert_edition(self, key: str, display: str, title: str | None, confidence: str) -> None:
        self.db.execute(
            "INSERT INTO edition(code_key,code_display,title,confidence) VALUES(?,?,?,?) "
            "ON CONFLICT(code_key) DO UPDATE SET"
            " confidence=CASE WHEN edition.confidence='parsed' AND excluded.confidence!='parsed'"
            " THEN edition.confidence ELSE excluded.confidence END,"
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

    # ---------------------------------------------------------------- probe / meta
    def log_probe(self, source: str, url: str, status: int | None, size: int | None,
                  outcome: str, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO probe(source,url,status,bytes,outcome,note,at) VALUES(?,?,?,?,?,?,?)",
            (source, url, status, size, outcome, note, now()),
        )

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO corpus_meta(k,v) VALUES(?,?)", (key, value))

    def _synced_at(self, source: str | None = None) -> str:
        """按源取 synced_at；mohurd 在过渡期回退旧的全局键。"""
        if source is None:
            return self.get_meta("synced_at")
        raw = self.get_meta(f"sync:{source}:synced_at")
        if not raw and source == "mohurd":
            raw = self.get_meta("synced_at")
        return raw

    def catalog_age_days(self, source: str | None = None) -> float | None:
        """目录上次同步距今多少天；从未同步返回 None。可按源查询。"""
        raw = self._synced_at(source)
        if not raw:
            return None
        try:
            then = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # 有 meta 但解析不了 ≠ 没同步：返回 None 保安全（调用方按"旧"处理），
            # 但别让它静默 —— catalog_meta_corrupt() 负责把脏信号显式化。
            return None
        return (datetime.now() - then).total_seconds() / 86400

    def catalog_meta_corrupt(self, source: str | None = None) -> str | None:
        """对应源的 synced_at 存在但解析不出 → 返回描述串；否则 None。"""
        raw = self._synced_at(source)
        if not raw:
            return None
        try:
            datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            label = f"sync:{source}:synced_at" if source else "synced_at"
            return f"catalog meta 损坏：{label}={raw!r}"
        return None

    def source_meta(self, source: str) -> dict:
        """读一个源的同步状态；catalog 完成不再用全局单值代替所有源。"""
        def field(name: str) -> str:
            return self.get_meta(f"sync:{source}:{name}")

        complete_raw = field("complete")
        if not complete_raw and source == "mohurd":
            complete_raw = self.get_meta("catalog_complete")
        try:
            total: int | None = int(field("total")) if field("total") else None
        except ValueError:
            total = None
        try:
            seen: int | None = int(field("seen")) if field("seen") else None
        except ValueError:
            seen = None
        synced_at = self._synced_at(source)
        # 直接按本源 synced_at 计算，不能对 mohurd 回退到全局值：只写
        # sync:mohurd:synced_at 时 status.sources 也应给出真实 age，而不是 None。
        age: float | None = None
        if synced_at:
            try:
                then = datetime.strptime(synced_at, "%Y-%m-%d %H:%M:%S")
                age = (datetime.now() - then).total_seconds() / 86400
            except ValueError:
                age = None
        return {
            "source": source,
            "complete": complete_raw == "1",
            "reason": field("reason"),
            "total": total,
            "seen": seen,
            "synced_at": synced_at,
            "age_days": None if age is None else round(age, 2),
        }

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM corpus_meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        q = self.db.execute
        # published_on 可能为 NULL：不 WHERE 掉、用 COALESCE 归入 ""，
        # 这样 by_year 的总和就是公告总数，announcements 不必再单独查一遍。
        by_year_rows = q(
            "SELECT COALESCE(substr(published_on,1,4),'') y, COUNT(*) c"
            " FROM announcement GROUP BY y ORDER BY y"
        ).fetchall()
        by_year = {r["y"]: r["c"] for r in by_year_rows}
        return {
            "announcements": sum(by_year.values()),
            "editions": q("SELECT COUNT(*) c FROM edition").fetchone()["c"],
            "mentions": q("SELECT COUNT(*) c FROM mention").fetchone()["c"],
            "scanned": q("SELECT COUNT(*) c FROM scan").fetchone()["c"],
            "by_year": {k: v for k, v in by_year.items() if k},
            "synced_at": self.get_meta("synced_at"),
        }

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        """幂等关闭。谁创建谁负责关（泄漏会以 ResourceWarning 暴露）。"""
        with contextlib.suppress(sqlite3.ProgrammingError):  # 已关闭
            self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def persist_codes(store: Store, ann_id: int, codes: Codes, doc_name: str, status: str = "unknown") -> None:
    """把一次 classify 的结果落库（edition + mention + scan）。

    重扫必须替换旧结果：先删该公告的旧 mention，再写本次结果，避免"新解析比旧结果少"
    时旧编号永久残留。mention 的角色只在同族同号时降级，不能把无关引用说成被代替。
    """
    if codes.primary_key:
        store.upsert_edition(codes.primary_key, codes.primary_display or codes.primary_key,
                             doc_name, codes.confidence.value)
    store.db.execute("DELETE FROM mention WHERE ann_id=?", (ann_id,))
    for m in codes.mentions:
        if m.code_key != codes.primary_key:
            # 整体 ambiguous 时，即使单条角色看起来像 target/primary，也不能
            # 把它落成 parsed —— 否则 cite 会把多 target 的猜测重新变成确定引用。
            decisive = (
                codes.confidence is Confidence.PARSED
                and m.role in (Role.TARGET_OF_REVISION, Role.PRIMARY, Role.SUPERSEDED_REF)
            )
            store.upsert_edition(
                m.code_key, m.code_display, None, "parsed" if decisive else "ambiguous"
            )
        role = m.role.value
        # 兜底降级只在 primary 是决定性证据（parsed）时成立，且必须同族同号：
        # "参考 GB 50016-2014" 这类无关引用不能被说成被 primary 代替。
        if (codes.primary_key and codes.confidence.value == "parsed"
                and m.code_key != codes.primary_key and m.role is Role.MENTIONED
                and _code_stem(m.code_key) == _code_stem(codes.primary_key)):
            role = "superseded_ref"
        store.add_mention(ann_id, m.code_key, role, m.evidence)
    store.mark_scanned(ann_id, len(codes.mentions))
