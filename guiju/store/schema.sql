-- 规矩 guiju · schema v2（2026-10-30 重建）
--
-- 建模要点：公告（现实存在物）与版本号（引用主体）分层，二者是 M:N + 角色。
-- 旧模型以公告为主、编号为附属索引，导致 39% 无编号公告无法寻址、同号多版本无法表达。

CREATE TABLE IF NOT EXISTS announcement (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    legacy_url    TEXT,
    published_on  TEXT,
    title         TEXT NOT NULL,
    doc_name      TEXT,
    category      TEXT NOT NULL DEFAULT 'other',
    body_sha256   TEXT,
    fetched_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_date ON announcement(published_on);
CREATE INDEX IF NOT EXISTS idx_ann_name ON announcement(doc_name);

CREATE TABLE IF NOT EXISTS edition (
    code_key      TEXT PRIMARY KEY,
    code_display  TEXT NOT NULL,
    title         TEXT,
    mandatory     INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'unknown',
    replaces      TEXT,
    replaced_by   TEXT,
    confidence    TEXT NOT NULL DEFAULT 'parsed'
);

CREATE TABLE IF NOT EXISTS mention (
    ann_id        INTEGER NOT NULL REFERENCES announcement(id) ON DELETE CASCADE,
    code_key      TEXT NOT NULL REFERENCES edition(code_key),
    role          TEXT NOT NULL,
    evidence      TEXT,
    PRIMARY KEY (ann_id, code_key)
);
CREATE INDEX IF NOT EXISTS idx_mention_code ON mention(code_key);

-- 已扫过详情页的公告（正文无编号时也要留痕，避免反复重抓）
CREATE TABLE IF NOT EXISTS scan (
    ann_id        INTEGER PRIMARY KEY REFERENCES announcement(id) ON DELETE CASCADE,
    scanned_at    TEXT NOT NULL,
    codes_found   INTEGER NOT NULL DEFAULT 0
);

-- 正文文件：独立于编号存在，允许「无编号但有正文」
CREATE TABLE IF NOT EXISTS artifact (
    id            INTEGER PRIMARY KEY,
    ann_id        INTEGER NOT NULL REFERENCES announcement(id) ON DELETE CASCADE,
    code_key      TEXT,
    source        TEXT NOT NULL DEFAULT 'mohurd',
    page_url      TEXT NOT NULL,
    file_url      TEXT NOT NULL,
    label         TEXT,
    sha256        TEXT NOT NULL UNIQUE,
    bytes         INTEGER,
    magic         TEXT,
    pages         INTEGER,
    verdict       TEXT NOT NULL DEFAULT 'ok',
    local_path    TEXT,
    fetched_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifact_code ON artifact(code_key);

-- 每次出站请求的韧性台账（M4 观测的数据源）
CREATE TABLE IF NOT EXISTS probe (
    id            INTEGER PRIMARY KEY,
    source        TEXT,
    url           TEXT,
    status        INTEGER,
    bytes         INTEGER,
    outcome       TEXT,
    note          TEXT,
    at            TEXT
);

CREATE TABLE IF NOT EXISTS corpus_meta (k TEXT PRIMARY KEY, v TEXT);

-- 时效查询缓存：服务不可达时仍能回答"这个编号是什么"（只存元数据，不存正文）
CREATE TABLE IF NOT EXISTS currency_cache (
    code_key   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
