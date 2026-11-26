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
from typing import Any

from ..domain.codes import Codes, Confidence, Role, _is_older_same_family
from ..errors import NeedsHuman, SourceUnreachable

SCHEMA_VERSION = 2
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def default_home() -> Path:
    return Path(os.environ.get("GUIJU_HOME") or (Path.home() / ".guiju"))


def _is_locked(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，保持用户输入的 % / _ / \\ 为字面量。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        cross_thread: bool = False,
        read_only: bool = False,
    ) -> None:
        # cross_thread=True：MCP 这类共享连接的场景放开 check_same_thread；
        # CPython sqlite3 threadsafety=1，共享后调用方必须自己串行化访问。
        # read_only=True：调用方承诺只读（MCP 共享库）；缺库时明确失败，绝不偷偷建库。
        home = None
        if path is None:
            home = default_home()
            path = home / "catalog.db"
        self.path = Path(path)
        self.legacy_backup: Path | None = None
        self._cross_thread = cross_thread
        if read_only and not self.path.exists():
            raise NeedsHuman(
                f"只读打开要求数据库已存在：{self.path}",
                hint="先让 guiju 在可写目录创建并同步一次，或用 --db 指向已有数据库",
            )
        # 文件或父目录不可写时，用 SQLite 的 mode=ro 打开；否则 WAL 的连接
        # 初始化会尝试写 -wal/-shm，只读目录下连 SELECT 都打不开。
        self._read_only = bool(read_only) or (
            self.path.exists() and (
                not os.access(self.path, os.W_OK)
                or not os.access(self.path.parent, os.W_OK)
            )
        )
        # 公开只读标记：调用方（MCP/测试）不必摸私有字段。
        self.read_only = self._read_only
        self._read_only_dir = self.path.exists() and not os.access(self.path.parent, os.W_OK)
        ok = False
        try:
            if self._read_only_dir and self._has_pending_wal():
                # immutable=1 会忽略 -wal；只读目录里若还有未 checkpoint 的 WAL，
                # 静默读主文件会给出旧快照，必须明确拒绝而不是返回错数据。
                raise NeedsHuman(
                    f"只读目录中的数据库仍有未合并的 WAL：{self.path}",
                    hint="在可写目录把库 checkpoint 后再复制；只读目录无法安全读取未合并 WAL",
                )
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
            # 老 v2 库可能没有 mention.confidence 列（只读库不能补列）：
            # 读路径据此决定用 COALESCE(m.confidence,e.confidence) 还是 e.confidence。
            self._mention_confidence = self._mention_has_confidence()
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

    def _has_pending_wal(self) -> bool:
        wal = self.path.with_name(self.path.name + "-wal")
        try:
            return wal.exists() and wal.stat().st_size > 0
        except OSError:
            return True

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
        if self._read_only:
            # 只读库绝不执行 DDL/迁移：0 字节空文件、旧格式库、未来版本都必须显式交人，
            # 不能在 read-only 连接上偷偷建表（那只会得到 SQLite 的只读错误）。
            raise NeedsHuman(
                f"只读库 schema 版本 {version} 不是当前版本（{SCHEMA_VERSION}）：{self.path}",
                hint="把数据库复制到可写目录后重新打开，或让 guiju 在可写 GUIJU_HOME 重建",
            )
        if version == 0 and self._has_tables():
            stamp = now().replace(":", "").replace(" ", "-")
            backup = self.path.with_name(f"{self.path.name}.legacy-{stamp}")
            suffix = 1
            while backup.exists():
                backup = self.path.with_name(f"{self.path.name}.legacy-{stamp}-{suffix}")
                suffix += 1
            self.db.close()
            shutil.copy2(self.path, backup)
            self.path.unlink()
            self.legacy_backup = backup
            self.db = sqlite3.connect(
                self.path, check_same_thread=not self._cross_thread)
            self.db.row_factory = sqlite3.Row
            self._configure_connection()
        elif version not in (0, SCHEMA_VERSION):
            raise NeedsHuman(
                f"数据库 schema 版本 {version} 超出本程序认知（{SCHEMA_VERSION}）",
                hint="备份数据库后确认 guiju 版本；需要升级/迁移时联系维护者，勿直接删除数据",
            )
        self.db.executescript(_SCHEMA_FILE.read_text(encoding="utf-8"))
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.db.commit()

    def _ensure_additions(self) -> None:
        """只做"加法"的结构变更：已有库升级到本版本时补表/补列，绝不动已有数据。

        为什么不上提 schema 版本：缓存表与按公告置信度都是可重建的派生数据，
        把它们算进版本迁移会让"版本对了但少张表/少一列"变成一类新故障。
        """
        if self._read_only:
            # 只读库即使缺表/缺列也不能补 DDL；读命令继续可用，写缓存时
            # 由调用方得到明确的只读写失败（CLI 映射 50）。
            return
        changed = False
        if not self._table_exists("currency_cache"):
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS currency_cache ("
                "code_key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL)"
            )
            changed = True
        # v1.2.0a4：mention 增加按公告的 confidence。老 v2 库只补列不动数据；
        # 旧行保持 NULL，读路径 COALESCE 回退 edition.confidence。
        if self._table_exists("mention") and not self._mention_has_confidence():
            self.db.execute("ALTER TABLE mention ADD COLUMN confidence TEXT")
            changed = True
        if changed:
            self.db.commit()

    def _table_exists(self, name: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def _mention_has_confidence(self) -> bool:
        rows = self.db.execute("PRAGMA table_info(mention)").fetchall()
        return any(row["name"] == "confidence" for row in rows)

    def _mention_confidence_sql(self) -> str:
        """按公告 confidence：新库用 m.confidence，老只读库回退 edition.confidence。"""
        return (
            "COALESCE(m.confidence, e.confidence)"
            if self._mention_confidence
            else "e.confidence"
        )

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
        # INSERT OR IGNORE + 条件 UPDATE：两个连接同时写同一 URL 时由 UNIQUE
        # 约束原子裁决，不会出现 SELECT 后再 INSERT 的 UNIQUE race；
        # UPDATE 不触碰 fetched_at，首次抓取时间保持不变。
        cur = self.db.execute(
            "INSERT OR IGNORE INTO announcement(url,published_on,title,doc_name,category,fetched_at) "
            "VALUES(?,?,?,?,?,?)",
            (url, published_on, title, doc_name, category, now()),
        )
        fresh = cur.rowcount == 1
        if fresh:
            return int(cur.lastrowid or 0), True
        self.db.execute(
            "UPDATE announcement SET published_on=?, title=?, doc_name=?, category=? WHERE url=?",
            (published_on, title, doc_name, category, url),
        )
        row = self.db.execute("SELECT id FROM announcement WHERE url=?", (url,)).fetchone()
        if row is None:
            raise NeedsHuman(
                f"公告写入竞争失败：{url}",
                hint="重试一次；若持续出现，检查数据库完整性和并发写入",
            )
        return int(row["id"]), False

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
            where.append("(a.title LIKE ? ESCAPE '\\' OR a.doc_name LIKE ? ESCAPE '\\' OR e.code_key LIKE ? ESCAPE '\\')")
            like = f"%{_escape_like(kw)}%"
            code_like = f"%{_escape_like(kw.upper().replace(' ', ''))}%"
            params += [like, like, code_like]
        if status:
            where.append("e.status = ?")
            params.append(status)
        sql = (
            "SELECT a.*, e.code_key, e.code_display, e.status AS code_status,"
            f"       {self._mention_confidence_sql()} AS confidence,"
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
    def upsert_edition(
        self,
        key: str,
        display: str,
        title: str | None,
        confidence: str,
        status: str | None = None,
    ) -> None:
        """写版本；status 只有拿到明确现行性（current/withdrawn…）时才覆盖已知值。

        插入用 status or "unknown" 满足 NOT NULL；ON CONFLICT 里 NULL/''/'unknown'
        一律保留旧值，避免"这次没查到现行性"把已确认的 current 洗回 unknown。
        """
        self.db.execute(
            "INSERT INTO edition(code_key,code_display,title,confidence,status)"
            " VALUES(?,?,?,?,?) "
            "ON CONFLICT(code_key) DO UPDATE SET"
            " confidence=CASE WHEN edition.confidence='parsed' AND excluded.confidence!='parsed'"
            " THEN edition.confidence ELSE excluded.confidence END,"
            " title=COALESCE(edition.title, excluded.title),"
            " status=CASE WHEN excluded.status IS NULL OR excluded.status IN ('','unknown')"
            " THEN edition.status ELSE excluded.status END",
            (key, display, title, confidence, status or "unknown"),
        )

    def add_mention(
        self,
        ann_id: int,
        code_key: str,
        role: str,
        evidence: str,
        confidence: str | None = None,
    ) -> None:
        """写提及；confidence 是按公告的置信度，None 时读路径回退 edition.confidence。"""
        if self._mention_confidence:
            self.db.execute(
                "INSERT INTO mention(ann_id,code_key,role,evidence,confidence) VALUES(?,?,?,?,?) "
                "ON CONFLICT(ann_id,code_key) DO UPDATE SET role=excluded.role,"
                " evidence=excluded.evidence,"
                " confidence=COALESCE(excluded.confidence, mention.confidence)",
                (ann_id, code_key, role, evidence, confidence),
            )
            return
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
            "SELECT m.code_key, m.role, m.evidence, e.code_display, e.status,"
            f" {self._mention_confidence_sql()} AS confidence"
            " FROM mention m LEFT JOIN edition e ON e.code_key = m.code_key"
            " WHERE m.ann_id = ? ORDER BY m.role, m.code_key",
            (ann_id,),
        ).fetchall()

    def find_by_code(self, key: str) -> list[sqlite3.Row]:
        exact = self.db.execute(
            "SELECT m.code_key, m.role, m.evidence, e.code_display, e.status,"
            f" {self._mention_confidence_sql()} AS confidence, a.*"
            " FROM mention m JOIN announcement a ON a.id = m.ann_id"
            " LEFT JOIN edition e ON e.code_key = m.code_key"
            " WHERE m.code_key = ? ORDER BY a.published_on DESC",
            (key,),
        ).fetchall()
        if exact:
            return exact
        return self.db.execute(
            "SELECT m.code_key, m.role, m.evidence, e.code_display, e.status,"
            f" {self._mention_confidence_sql()} AS confidence, a.*"
            " FROM mention m JOIN announcement a ON a.id = m.ann_id"
            " LEFT JOIN edition e ON e.code_key = m.code_key"
            " WHERE m.code_key LIKE ? ESCAPE '\\' ORDER BY a.published_on DESC LIMIT 20",
            (f"%{_escape_like(key)}%",),
        ).fetchall()

    # ---------------------------------------------------------------- probe / meta
    def persist_codes(self, ann_id: int, codes: Codes, doc_name: str, status: str | None = None) -> None:
        """应用层端口：落一次 classify 结果；SQL 只留在适配器内。"""
        persist_codes(self, ann_id, codes, doc_name, status=status)

    def log_probe(self, source: str, url: str, status: int | None, size: int | None,
                  outcome: str, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO probe(source,url,status,bytes,outcome,note,at) VALUES(?,?,?,?,?,?,?)",
            (source, url, status, size, outcome, note, now()),
        )

    def record_body_hash(self, ann_id: int, digest: str, *, source: str = "") -> bool:
        """记录详情页 body_sha256；仅在已有非空旧指纹且发生变化时返回 True 并写 probe。"""
        if not digest:
            return False
        row = self.db.execute(
            "SELECT url, body_sha256 FROM announcement WHERE id=?", (ann_id,)
        ).fetchone()
        if row is None:
            return False
        prev = row["body_sha256"] or ""
        if prev == digest:
            return False
        self.db.execute("UPDATE announcement SET body_sha256=? WHERE id=?", (digest, ann_id))
        if prev:
            self.log_probe(
                source or "unknown",
                row["url"] or f"ann:{ann_id}",
                200,
                None,
                "body_changed",
                f"{prev[:12]}->{digest[:12]}",
            )
            return True
        return False

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
        age = (datetime.now() - then).total_seconds() / 86400
        # 未来时间戳超过 1 小时不是“新鲜”，而是元数据损坏；小幅时钟漂移按 0 处理。
        if age < -(1 / 24):
            return None
        return max(0.0, age)

    def catalog_meta_corrupt(self, source: str | None = None) -> str | None:
        """对应源的 synced_at 存在但解析不出 → 返回描述串；否则 None。"""
        raw = self._synced_at(source)
        if not raw:
            return None
        label = f"sync:{source}:synced_at" if source else "synced_at"
        try:
            then = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return f"catalog meta 损坏：{label}={raw!r}"
        if (datetime.now() - then).total_seconds() < -3600:
            return f"catalog meta 损坏：{label}={raw!r}（未来时间戳超过 1 小时）"
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
                age = None if age < -(1 / 24) else max(0.0, age)
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
    @property
    def schema_version(self) -> int:
        """对应用层暴露 schema 版本，不让 application 依赖模块常量。"""
        return SCHEMA_VERSION

    def probe_counts(self) -> dict[str, int]:
        """时效探测台账的 outcome 计数（status 用），封装 SQL 留在适配器。"""
        rows = self.db.execute(
            "SELECT outcome, COUNT(*) c FROM probe GROUP BY outcome ORDER BY c DESC"
        ).fetchall()
        return {row["outcome"]: row["c"] for row in rows}

    def probe_summary_by_source(self, *, recent_failures: int = 5) -> dict[str, dict[str, Any]]:
        """按数据源聚合 probe 台账与近期异常，供 sources 健康度视图消费。"""
        limit = max(0, int(recent_failures))
        summary: dict[str, dict[str, Any]] = {}
        rows = self.db.execute(
            "SELECT COALESCE(NULLIF(source, ''), 'unknown') AS src,"
            "       outcome, COUNT(*) AS c, MAX(at) AS last_at"
            " FROM probe GROUP BY src, outcome"
        ).fetchall()
        for row in rows:
            src = str(row["src"])
            entry = summary.setdefault(
                src,
                {"total": 0, "counts": {}, "last_at": "", "body_changes": 0, "recent_failures": []},
            )
            count = int(row["c"])
            outcome = str(row["outcome"] or "")
            entry["total"] += count
            entry["counts"][outcome] = count
            last_at = str(row["last_at"] or "")
            if last_at > entry["last_at"]:
                entry["last_at"] = last_at
            if outcome == "body_changed":
                entry["body_changes"] += count
        if limit > 0 and summary:
            fail_rows = self.db.execute(
                "SELECT COALESCE(NULLIF(source, ''), 'unknown') AS src,"
                "       url, status, bytes, outcome, note, at"
                " FROM probe WHERE outcome != 'ok' ORDER BY id DESC"
            ).fetchall()
            for row in fail_rows:
                entry = summary.get(str(row["src"]))
                if entry is None or len(entry["recent_failures"]) >= limit:
                    continue
                entry["recent_failures"].append(
                    {
                        "url": row["url"],
                        "status": row["status"],
                        "bytes": row["bytes"],
                        "outcome": row["outcome"],
                        "note": row["note"] or "",
                        "at": row["at"] or "",
                    }
                )
        return summary

    def body_hash_coverage(self) -> dict[str, int]:
        """详情页指纹覆盖度：已记录 body_sha256 的公告数与总公告数。"""
        row = self.db.execute(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(CASE WHEN body_sha256 IS NOT NULL AND body_sha256 != '' THEN 1 ELSE 0 END), 0) AS hashed"
            " FROM announcement"
        ).fetchone()
        return {"hashed": int(row["hashed"]), "total": int(row["total"])}

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


def persist_codes(
    store: Store, ann_id: int, codes: Codes, doc_name: str, status: str | None = None
) -> None:
    """把一次 classify 的结果落库（edition + mention + scan）。

    重扫必须替换旧结果：先删该公告的旧 mention，再写本次结果，避免"新解析比旧结果少"
    时旧编号永久残留。mention 的角色只在同族同号且年份更早时降级，不能把无关引用、
    年份更新或年份缺失的编号说成被代替。

    confidence 按公告写入 mention：edition 是跨公告的全局表，不能代表某条公告
    自己的解析置信度（同名标准 A 解析成功、B 只是提及，两者必须可区分）。

    status 只用于 primary 版本，且只在调用方拿到明确现行性（pull 的 current/withdrawn）
    时传入；None 表示这次没查，不能覆盖库里已知状态。
    """
    if codes.primary_key:
        store.upsert_edition(
            codes.primary_key,
            codes.primary_display or codes.primary_key,
            doc_name,
            codes.confidence.value,
            status=status,
        )
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
        # 兜底降级只在 primary 是决定性证据（parsed）时成立，且必须同族同号、
        # 候选年份严格更早：2010 primary + 2022 mentioned 不是"旧版被代替"。
        if (
            codes.primary_key
            and codes.confidence.value == "parsed"
            and m.code_key != codes.primary_key
            and m.role in (Role.MENTIONED, Role.TARGET_OF_REVISION, Role.PRIMARY)
            and _is_older_same_family(m.code_key, codes.primary_key)
        ):
            role = "superseded_ref"
        # 非 primary 的置信度：只有整体 parsed 且角色本身是强证据时才是 parsed；
        # mentioned/weak_primary 以及整条公告 ambiguous 时都不算确定引用。
        if m.code_key == codes.primary_key:
            mention_confidence = codes.confidence.value
        elif codes.confidence is Confidence.PARSED and role in (
            Role.TARGET_OF_REVISION.value,
            Role.PRIMARY.value,
            Role.SUPERSEDED_REF.value,
        ):
            mention_confidence = Confidence.PARSED.value
        else:
            mention_confidence = Confidence.AMBIGUOUS.value
        store.add_mention(
            ann_id, m.code_key, role, m.evidence, confidence=mention_confidence
        )
    store.mark_scanned(ann_id, len(codes.mentions))
