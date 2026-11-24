"""guiju MCP 服务面：query / verify / batch / announcement 的纯标准库 JSON-RPC。

默认 stdio；--http 时提供 POST /mcp（仅 application/json；Host/Origin 按可配置
allowlist 校验，防 DNS rebinding）。业务后端优先使用 NativeCurrency.query() 的不可变 Outcome（旧适配器保留
Currency.lookup 兼容路径），不在这里复制适配器、正文下载或时效路由逻辑
（旧 spec-currency 已退役）。
announcement 只回公告元数据（编号/名称/可得性/附件链接）；会抓公告页解析附件，但不下载正文、不写盘。
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ._version import __version__
from .domain.currency import Record
from .errors import ComplianceDenied, GuijuError, NeedsHuman
from .ports.currency import CurrencyPort

PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PATH = "/mcp"
# POST body 硬上限：Content-Length 取客户端声明，不设限可被大 body 撑爆内存。
# JSON-RPC 请求正常只有几 KB，4MB 已是很宽的余量。
MAX_BODY_BYTES = 4 * 1024 * 1024
# HTTP 读超时与并发上限：慢连接不能无限占用 handler 线程。
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_MAX_WORKERS = 8

#: announcement 只读库必须完整具备的核心表；缺任何一张都不能假装"目录为空"。
_CORE_SCHEMA_TABLES = ("announcement", "edition", "mention", "scan", "corpus_meta")

TOOLS = (
    {
        "name": "query",
        "description": "查询规范现行状态：输入规范号，返回全部命中版本及统一状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "verify",
        "description": "核验规范状态（jl-docx code_refs 用）：返回 code<TAB>status。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "batch",
        "description": "批量盘点规范清单：优先传 codes 内联编号数组；list/out 文件路径仅为过渡兼容（限制在 GUIJU_HOME/mcp 下，1.3.0 删除）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "codes": {"type": "array", "items": {"type": "string"}},
                "list": {"type": "string", "description": "deprecated：过渡期文件清单"},
                "out": {"type": "string", "description": "deprecated：过渡期盘点表输出"},
            },
        },
    },
    {
        "name": "announcement",
        "description": "按编号/规范名/公告ID/公告URL定位公告并抓公告页解析附件链接，返回元数据（不下载正文、不落盘）。",
        "inputSchema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
)


def _mcp_io_root() -> Path:
    """MCP batch 的唯一文件根：GUIJU_HOME/mcp。

    服务面不再拥有 CLI 的任意路径能力。根不存在时按需创建（batch 本来就要
    落 out 文件）；但拒绝把根本身做成 symlink/普通文件，避免部署时把
    "专用根"指到 `/` 或其他宽目录后重新放开任意路径。
    """
    from .store.sqlite import default_home

    raw = default_home() / "mcp"
    if raw.is_symlink():
        raise ValueError(f"MCP 文件根不能是 symlink：{raw}")
    if raw.exists() and not raw.is_dir():
        raise ValueError(f"MCP 文件根不是目录：{raw}")
    raw.mkdir(parents=True, exist_ok=True)
    return raw.resolve()


def _safe_mcp_path(raw: str, *, label: str) -> Path:
    """把 batch 的 list/out 限制在 GUIJU_HOME/mcp 下，跟随 symlink 后仍须在根内。"""
    path = Path(raw).expanduser()
    root = _mcp_io_root()
    if not path.is_absolute():
        # 相对路径按 MCP 专用根解析，避免服务进程 cwd（/opt/guiju）把合法清单挡在外面。
        path = root / path
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"{label}必须位于 {root} 下（收到 {raw}）；MCP 不接受任意文件路径"
        ) from exc
    return resolved


def _text_result(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": bool(error)}


def _error_result(message: str) -> dict:
    return _text_result(f"错误: {message}", error=True)


def _rows_text(rows: list[Record], *, verify: bool = False) -> str:
    if not rows:
        return "unknown" if verify else "无命中"
    lines = []
    for row in rows:
        if verify:
            lines.append(f"{row.code}\t{row.status}")
        else:
            lines.append(f"{row.code}\t{row.status}\t{row.name}\t{row.implement_date}\t{row.source}")
    return "\n".join(lines) + "\n"


def _as_error(exc: GuijuError) -> dict:
    """错误体与 CLI --json 同构：保留 exit/reason/hint/候选等完整 payload。

    只回 message 会让 MCP 消费者拿不到退出码决策表（20 重试 / 34 换路径 / 50 上报）。
    """
    return _text_result(json.dumps(exc.payload(), ensure_ascii=False), error=True)


def _cache_status(source: object) -> dict | None:
    """读取一次查询结果的状态，返回 null 表示没有需要上报的告警。

    source 可以是 NativeCurrency.query() 返回的不可变 Outcome，也可以是
    旧适配器/测试替身（提供 stale/from_cache 等属性）。MCP 不再从共享
    backend 实例上摸 last_outcome，避免 HTTP 并发请求互相覆盖状态。
    """
    stale = bool(getattr(source, "stale", False))
    from_cache = bool(getattr(source, "from_cache", False))
    age = getattr(source, "cache_age_days", None)
    if age is None:
        age_seconds = getattr(source, "cache_age", None)
        if isinstance(age_seconds, (int, float)) and not isinstance(age_seconds, bool):
            age = round(age_seconds / 86400.0, 2)
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not from_cache:
        age = None
    complete = getattr(source, "complete", None)
    incomplete_reason = getattr(source, "incomplete_reason", "") or ""
    fallback_source = getattr(source, "fallback_source", "") or ""
    if not stale and not from_cache and complete is not False and not fallback_source:
        return None
    status: dict = {"stale": stale, "from_cache": from_cache, "cache_age_days": age}
    if complete is False:
        status["complete"] = False
        if incomplete_reason:
            status["incomplete_reason"] = str(incomplete_reason)
    if fallback_source:
        status["fallback_source"] = str(fallback_source)
    return status


def _cache_warning_text(status: dict) -> str:
    # 兼容既有 MCP 消费者的 "缓存告警" 前缀；纯完整性/备源告警用"结果告警"，
    # 两者都是同一条机器可读 content，不静默丢掉。
    prefix = "# 缓存告警" if (status.get("stale") or status.get("from_cache")) else "# 结果告警"
    return prefix + " " + json.dumps(status, ensure_ascii=False)


def _query_result(backend: object, code: str) -> tuple[list[Record], object]:
    """优先用 query() 的不可变 Outcome，旧后端保留 lookup() 兼容路径。

    NativeCurrency.query() 返回 Outcome；旧测试替身可能只有 lookup()。
    只要拿到 Outcome，就绝不能再回头读 backend.last_outcome。
    """
    query = getattr(backend, "query", None)
    if callable(query):
        outcome = query(code)
        rows = getattr(outcome, "results", None)
        if rows is not None:
            return list(rows), outcome
    return backend.lookup(code), backend  # type: ignore[attr-defined]


class _ReadOnlyStore:
    """announcement 工具专用的只读存储门面。

    契约是"只回元数据、不落盘"：resolve 在读路径上若走到缓存/持久化分支，
    会写 edition / mention / scan / currency_cache / corpus_meta。
    这里把所有已知写方法拦成 no-op（或返回"什么都没写"的占位），读方法原样转发；
    底层 Store 由 _ensure_readonly_catalog 以 mode=ro 打开，形成双保险。
    注意：缺库/0 字节时 _ensure_readonly_catalog 会先建一次空 schema（只有表结构，
    不含任何公告数据），之后才启用本门面。
    """

    _WRITES = frozenset({
        "upsert_announcement", "upsert_edition", "add_mention", "mark_scanned",
        "log_probe", "set_meta", "cache_currency", "commit",
    })

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        if name in self._WRITES:
            # upsert_announcement 期望返回 (row_id, inserted)；其余返回值没人消费，一律 no-op。
            return (lambda *a, **k: (0, False)) if name == "upsert_announcement" else (lambda *a, **k: None)
        return getattr(self._inner, name)

    def close(self) -> None:
        self._inner.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def _default_catalog_path() -> Path:
    from .store.sqlite import default_home

    return default_home() / "catalog.db"


def _schema_snapshot(path: Path) -> tuple[int, set[str]]:
    """用 mode=ro 读取 schema 版本与表名；不触发建表/迁移/备份。"""
    import sqlite3

    uri = path.resolve().as_uri() + "?mode=ro"
    # 父目录不可写时 SQLite 无法创建 -shm/-wal；与 Store(read_only) 同样使用
    # immutable=1，但只限于没有未合并 WAL 的情况，否则会静默读旧快照。
    if not os.access(path.parent, os.W_OK):
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size > 0:
            raise NeedsHuman(
                f"只读目录中的数据库仍有未合并的 WAL：{path}",
                hint="在可写目录把库 checkpoint 后再复制；只读目录无法安全读取未合并 WAL",
            )
        uri += "&immutable=1"
    try:
        db = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise NeedsHuman(
            f"数据库无法打开或已损坏：{path}（{type(exc).__name__}: {exc}）",
            hint="先备份该文件，再让 guiju 在可写目录重建目录；MCP 不会替用户修改数据库",
        ) from exc
    try:
        try:
            row = db.execute("PRAGMA user_version").fetchone()
            version = int(row[0]) if row else 0
            tables = {
                str(item[0])
                for item in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
        except sqlite3.Error as exc:
            raise NeedsHuman(
                f"数据库无法读取：{path}（{type(exc).__name__}: {exc}）",
                hint="先备份该文件，再让 guiju 在可写目录重建目录；MCP 不会替用户修改数据库",
            ) from exc
    finally:
        db.close()
    return version, tables


def _create_empty_catalog(path: Path) -> None:
    """只建空 schema（announcement 等表），不写任何公告数据。"""
    from .store.sqlite import Store

    store = Store(path, cross_thread=True)
    store.close()


def _ensure_readonly_catalog(path: Path):
    """打开 announcement 专用只读 Store。

    分支语义（W5-5）：
      - 缺库 / 0 字节 / schema v0 且完全无表：先建空 schema，再以 mode=ro 打开；
      - v2 完整表：直接只读；
      - v0 且有表、v2 缺核心表、非 v2/损坏：NeedsHuman(50)，绝不替用户备份/重建/补 DDL。
    """
    from .store.sqlite import SCHEMA_VERSION, Store

    if not path.exists() or path.stat().st_size == 0:
        _create_empty_catalog(path)
    else:
        version, tables = _schema_snapshot(path)
        if version == SCHEMA_VERSION:
            missing = [name for name in _CORE_SCHEMA_TABLES if name not in tables]
            if missing:
                raise NeedsHuman(
                    f"数据库缺少核心表：{path}（缺少 {', '.join(missing)}）",
                    hint="该库不是完整的 guiju v2 目录库；请用 --db 指向完整库，"
                         "或让 guiju 在可写 GUIJU_HOME 重新 sync。MCP 不会自动补 DDL",
                )
        elif version == 0:
            if tables:
                raise NeedsHuman(
                    f"只读使用遇到旧版（schema v0）数据库：{path}",
                    hint="MCP 不会替用户备份/重建旧库；请先运行可写的 guiju sync 完成迁移，"
                         "或用 --db 指向 v2 数据库",
                )
            _create_empty_catalog(path)
        else:
            raise NeedsHuman(
                f"数据库 schema 版本 {version} 超出本程序认知（{SCHEMA_VERSION}）：{path}",
                hint="备份数据库后确认 guiju 版本；需要升级/迁移时联系维护者，勿直接删除数据",
            )
    return Store(path, cross_thread=True, read_only=True)


class McpServer:
    """与传输无关的 JSON-RPC 处理核心。"""

    def __init__(self, backend: CurrencyPort, *, store_path: str | Path | None = None) -> None:
        self.backend = backend
        # announcement 的共享只读存储：避免每请求跑一次建表/迁移 DDL。
        # CPython sqlite3 模块 threadsafety=1，共享连接必须外部串行化 ——
        # self._store_lock 包住"创建 + 每次 _announcement 调用"（含 detail 抓取，
        # announcement 本身低频，串行可接受）。
        self.store_path = Path(store_path) if store_path is not None else None
        self._store = None
        self._store_lock = threading.Lock()

    def _ann_store(self):
        """惰性创建共享的只读 Store（cross_thread 允许连接跨线程使用）。"""
        if self._store is None:
            path = self.store_path if self.store_path is not None else _default_catalog_path()
            self._store = _ReadOnlyStore(_ensure_readonly_catalog(path))
        return self._store

    def close(self) -> None:
        """释放共享连接（幂等）。serve_* 退出时调用；测试用完也建议调。"""
        with self._store_lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    def _query(self, arguments: dict) -> dict:
        code = str(arguments.get("code") or "").strip()
        if not code:
            return _error_result("缺少 code 参数")
        try:
            rows, state = _query_result(self.backend, code)
            text = _rows_text(rows)
        except GuijuError as exc:
            return _as_error(exc)
        status = _cache_status(state)
        if status is not None:
            text += "\n" + _cache_warning_text(status)
        return _text_result(text)

    def _verify(self, arguments: dict) -> dict:
        code = str(arguments.get("code") or "").strip()
        if not code:
            return _error_result("缺少 code 参数")
        try:
            rows, state = _query_result(self.backend, code)
            text = _rows_text(rows, verify=True)
        except GuijuError as exc:
            return _as_error(exc)
        result = _text_result(text)
        status = _cache_status(state)
        if status is not None:
            # content[0] 是调用方严格解析的 code\tstatus 契约，不得混入告警。
            result["content"].append({
                "type": "text",
                "text": json.dumps(status, ensure_ascii=False),
            })
        return result

    def _batch(self, arguments: dict) -> dict:
        codes: list[str] = []
        raw_codes = arguments.get("codes")
        if raw_codes is not None:
            if not isinstance(raw_codes, list) or any(not isinstance(c, str) for c in raw_codes):
                return _error_result("codes 必须是字符串数组")
            codes.extend(c.strip() for c in raw_codes if c.strip())
        list_path = str(arguments.get("list") or "").strip()
        if list_path:
            try:
                list_file = _safe_mcp_path(list_path, label="清单")
                lines = list_file.read_text(encoding="utf-8").splitlines()
            except ValueError as exc:
                return _error_result(str(exc))
            except OSError as exc:
                return _error_result(f"清单不可读：{exc}")
            codes.extend(line.split("#", 1)[0].strip() for line in lines)
        codes = [code for code in codes if code]
        if not codes:
            return _error_result("缺少 list 或 codes 参数" if not list_path else "清单为空")
        # 先校验 out，再查后端：越界 out 不应先消耗一批上游请求。
        out_path = str(arguments.get("out") or "").strip()
        out_file = None
        if out_path:
            try:
                out_file = _safe_mcp_path(out_path, label="盘点表")
            except ValueError as exc:
                return _error_result(str(exc))
            except OSError as exc:
                return _error_result(f"盘点表不可写：{exc}")
        completed = skipped = failed = unmatched = 0
        out_rows: list[str] = []
        warnings: list[dict] = []
        for code in codes:
            try:
                rows, state = _query_result(self.backend, code)
            except ComplianceDenied:
                # 40 是硬停止：不得继续查询后续编号，也不得写盘点表假装"部分完成"。
                raise
            except GuijuError:
                failed += 1
                out_rows.append(f"{code}\t查询失败")
                continue
            status = _cache_status(state)
            if status:
                warnings.append({"code": code, **status})
            if not rows:
                unmatched += 1
                continue
            for row in rows:
                if row.status == "unknown":
                    skipped += 1
                else:
                    completed += 1
                out_rows.append("\t".join((code, row.code, row.status, row.name, row.implement_date, row.source)))
        if out_file is not None:
            try:
                out_file.write_text(
                    "# format: guiju-mcp-batch-v1\n# 输入\t规范号\t状态\t名称\t实施日期\t数据源\n"
                    + "\n".join(out_rows) + ("\n" if out_rows else ""),
                    encoding="utf-8",
                )
            except OSError as exc:
                return _error_result(f"盘点表写入失败：{exc}")
        result = _text_result(
            f"总数 {len(codes)} | 完成 {completed} | 跳过 {skipped} | 失败 {failed} | 无源 {unmatched} | 盘点表 {out_path or '(未输出文件，仅统计)'}"
        )
        if warnings:
            result["content"].append({
                "type": "text",
                "text": json.dumps({"warnings": warnings}, ensure_ascii=False),
            })
        return result

    def _announcement(self, arguments: dict) -> dict:
        """公告元数据解析：与 pull 同源定位，但只回链接，不下文件、不写盘。"""
        ref = str(arguments.get("ref") or "").strip()
        if not ref:
            return _error_result("缺少 ref 参数")
        from .application import resolve as lookup
        from .domain import announcement, availability
        from .domain import attachment as att_domain
        from .sources import registry

        try:
            with self._store_lock:
                # 契约是"只回元数据、不落盘"：allow_network=False 堵死 resolve 里唯一的写分支
                # （拉详情页 + persist_codes），_ann_store 已是结构只读库 + 写方法 no-op 门面。
                ro = self._ann_store()
                target = lookup.resolve(
                    ro, ref, currency=self.backend, allow_network=False,
                    sources=registry())
                ann = target["ann"]
                window = availability.state(ann["published_on"])
                source_id = lookup.source_of(ann["url"])["id"]
                source = registry().get(source_id)
                if source is None:
                    # 公告 URL 的 host 不在源表（_SOURCE_HOSTS）里：宁可显式报错
                    # 也不能静默拿 mohurd 去解析一个根本不是它的页面结构。
                    raise NeedsHuman(
                        f"公告页归属站点无法识别（source_id={source_id}，url={ann['url']}）",
                        hint="若站点改了域名请在 guiju/application/resolve.py 的 _SOURCE_HOSTS 补映射，"
                             "或为新站点补适配器",
                    )
                names = announcement.names_of(ann["title"], ann["doc_name"])
                picked_name = ann["doc_name"] or ""
                ref_name = ""
                if target.get("via") in ("name", "name_current_version"):
                    chosen, _matched = announcement.match_name(names, ref)
                    if chosen:
                        picked_name = ref_name = chosen
                choice = None
                candidates: list = []
                if getattr(source, "api_mode", False):
                    # API 源没有公告页可抓（shzjw）：正文链接在检索结果里，不在页面上
                    verdict = "api_mode"
                else:
                    html, _ = source.detail(ann["url"])
                    atts = source.attachments(html)
                    picked = att_domain.pick(
                        atts, names, ref_name=ref_name, announcement_title=ann["title"])
                    choice = picked.choice if picked.ok else None
                    candidates = (picked.as_payload() or {}).get("candidates") or []
                    verdict = "ok" if picked.ok else getattr(
                        picked.verdict, "value", str(picked.verdict))
                payload = {
                    "ok": True,
                    "announcement_id": ann["id"],
                    "code": target.get("code_display") or target.get("code_key"),
                    "name": picked_name,
                    "names": list(names),
                    "published_on": ann["published_on"],
                    "title": ann["title"],
                    "category": ann["category"],
                    "page_url": ann["url"],
                    "source": source_id,
                    "availability": window.value,
                    "status": target.get("status") or "",
                    "confidence": target.get("confidence") or "",
                    "attachment_verdict": verdict,
                    "attachment_url": choice.url if choice else None,
                    "attachment_label": choice.label if choice else None,
                    "attachment_candidates": candidates,
                }
                return _text_result(json.dumps(payload, ensure_ascii=False))
        except GuijuError as exc:
            # 保留完整 payload（reason/hint/candidates）：32 歧义时
            # candidates 正是调用方要拿给人看的那部分
            return _text_result(
                json.dumps(exc.payload(), ensure_ascii=False), error=True)

    def _call(self, name: str, arguments: dict) -> dict:
        try:
            if name == "query":
                return self._query(arguments)
            if name == "verify":
                return self._verify(arguments)
            if name == "batch":
                return self._batch(arguments)
            if name == "announcement":
                return self._announcement(arguments)
            return _error_result(f"未知工具：{name}")
        except (KeyboardInterrupt, SystemExit):
            raise
        except GuijuError as exc:
            # batch 等工具内部的契约错误（尤其是 40）走统一完整 payload。
            return _as_error(exc)
        except Exception:
            # 工具内部异常（sqlite、网络、解析等任何非 GuijuError）不得
            # 冒到调用方：stdio 会杀死整个服务循环，HTTP 线程会断连。
            import traceback

            traceback.print_exc(file=sys.stderr)
            return _error_result("工具执行失败（内部错误，见服务端 stderr）")

    def handle(self, payload: dict) -> dict | None:
        if not isinstance(payload, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "请求必须是 JSON object"}}
        method = payload.get("method")
        request_id = payload.get("id")
        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            # 非 object 的 params 以前会在 initialize/tools.call 的 .get() 上抛异常，
            # 直接打死 stdio 循环；这里统一按 JSON-RPC -32602 处理。
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32602, "message": "params 必须是 JSON object"}}
        if method == "initialize":
            requested = str(params.get("protocolVersion") or PROTOCOL_VERSIONS[-1])
            return {
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "guiju", "version": __version__},
                },
            }
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": list(TOOLS)}}
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32602, "message": "arguments 必须是 object"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": self._call(name, arguments)}
        if method == "server/discover":
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"supportedVersions": list(PROTOCOL_VERSIONS),
                               "serverInfo": {"name": "guiju", "version": __version__}}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"未知方法：{method}"}}


def serve_stdio(server: McpServer) -> int:
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                print(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "JSON 解析失败"}}, ensure_ascii=False), flush=True)
                continue
            try:
                response = server.handle(payload)
            except Exception:
                # 兜底：即使某个新方法再次漏掉类型校验，也不能让一行坏请求
                # 杀死整个 stdio 服务；回一条 JSON-RPC 内部错误后继续下一行。
                import traceback

                traceback.print_exc(file=sys.stderr)
                response = {"jsonrpc": "2.0",
                            "id": payload.get("id") if isinstance(payload, dict) else None,
                            "error": {"code": -32603, "message": "内部错误"}}
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        server.close()
    return 0


def default_backend() -> object:
    from .currency import native_currency

    return native_currency()


def _split_allowlist(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _canonical_host(value: str) -> str:
    """Host/Origin 主机名规范形：小写、IDNA、IPv6 加括号、可选端口。"""
    import ipaddress

    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        try:
            value = urlsplit(value).netloc
        except ValueError:
            return ""
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return ""
        host_part = value[: end + 1]
        rest = value[end + 1 :]
        if rest and not rest.startswith(":"):
            return ""
        port_part = rest[1:] if rest else ""
    else:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            host_part, sep, maybe_port = value.rpartition(":")
            if sep and maybe_port.isdigit():
                port_part = maybe_port
            else:
                host_part, port_part = value, ""
        else:
            host_part = f"[{ip.compressed}]" if ip.version == 6 else ip.compressed
            port_part = ""
    if not host_part:
        return ""
    if not host_part.startswith("["):
        try:
            host_part = host_part.encode("idna").decode("ascii")
        except UnicodeError:
            return ""
        host_part = host_part.rstrip(".")
    return f"{host_part}:{port_part}" if port_part else host_part


def _default_allowed_hosts(host: str, port: int) -> set[str]:
    """默认只接受绑定地址与 localhost 等价形式，防 DNS rebinding。"""
    entries: set[str] = set()
    for base in (host, "localhost", "127.0.0.1", "[::1]"):
        canonical = _canonical_host(base)
        if canonical:
            entries.add(canonical)
            entries.add(f"{canonical}:{port}")
    return entries


def _canonical_origin(value: str) -> str:
    raw = (value or "").strip()
    if not raw or raw.lower() == "null":
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return ""
    if parts.username or parts.password:
        return ""
    host = _canonical_host(parts.netloc)
    if not host:
        return ""
    scheme = parts.scheme.lower()
    # 默认端口不是 origin 的一部分：https://x:443 与 https://x 等价。
    if scheme == "https" and host.endswith(":443"):
        host = host[:-4]
    elif scheme == "http" and host.endswith(":80"):
        host = host[:-3]
    return f"{scheme}://{host}"


def _origin_allowed(origin: str, allowed_origins: set[str], allowed_hosts: set[str]) -> bool:
    """Origin 缺省允许（非浏览器客户端）；存在时必须命中白名单或与允许 Host 同源。"""
    canonical = _canonical_origin(origin)
    if not canonical:
        return False
    if canonical in allowed_origins:
        return True
    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return False
    origin_host = _canonical_host(parts.netloc)
    # 同一 host 已配置了另一种 scheme 的 origin 时，禁止用 Host 兜底做
    # http<->https 互换（例如只允许 https://x 时 http://x 必须 403）。
    for allowed in allowed_origins:
        try:
            allowed_parts = urlsplit(allowed)
        except ValueError:
            continue
        if (
            _canonical_host(allowed_parts.netloc) == origin_host
            and allowed_parts.scheme.lower() != parts.scheme.lower()
        ):
            return False
    # 同源兜底只适用于本服务实际使用的 http；https Origin 必须精确命中
    # allowed_origins，不能因为 host 相同就放行 http<->https 互换。
    if parts.scheme != "http":
        return False
    host = _canonical_host(parts.netloc)
    if host in allowed_hosts:
        return True
    try:
        port = parts.port
    except ValueError:
        return False
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    hostname = parts.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return _canonical_host(f"{hostname}:{port}") in allowed_hosts


def _configured_allowlist(explicit: list[str] | None, env_name: str) -> list[str]:
    values = _split_allowlist(os.environ.get(env_name, ""))
    values.extend(explicit or [])
    return values


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """限制同时处理的请求线程数：慢连接/突发流量不能无限堆线程。"""

    daemon_threads = True

    def __init__(self, server_address, handler_cls, *, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        if max_workers < 1:
            raise ValueError("max_workers 必须 >= 1")
        super().__init__(server_address, handler_cls)
        self.max_workers = max_workers
        self._worker_slots = threading.BoundedSemaphore(max_workers)

    def process_request(self, request, client_address) -> None:
        # 在 accept 循环里取槽位：没有空闲槽时让连接留在内核 backlog，
        # 而不是创建无限多的线程。
        self._worker_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


def make_http_server(
    server: McpServer,
    host: str,
    port: int,
    path: str = DEFAULT_PATH,
    *,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    max_workers: int = DEFAULT_MAX_WORKERS,
    allow_remote: bool = False,
    bearer_token: str | None = None,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "guiju-mcp"

        def setup(self) -> None:
            self.timeout = getattr(self.server, "request_timeout", DEFAULT_REQUEST_TIMEOUT)
            super().setup()

        def _send(
            self,
            code: int,
            body: bytes,
            content_type: str = "application/json",
            *,
            close: bool = False,
            headers: dict[str, str] | None = None,
        ) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if close:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                if close:
                    self.close_connection = True

        def _check_host_origin(self) -> bool:
            if _canonical_host(self.headers.get("Host", "")) not in self.server.allowed_hosts:
                self._send(403, b'{"error":"forbidden host"}', close=True)
                return False
            origin = self.headers.get("Origin")
            if origin is not None and not _origin_allowed(
                    origin, self.server.allowed_origins, self.server.allowed_hosts):
                self._send(403, b'{"error":"forbidden origin"}', close=True)
                return False
            return True

        def _check_auth(self) -> bool:
            if not getattr(self.server, "require_bearer", False):
                return True
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                self._send(401, b'{"error":"unauthorized"}',
                           headers={"WWW-Authenticate": "Bearer"}, close=True)
                return False
            if not hmac.compare_digest(header[len(prefix):], self.server.bearer_token):
                self._send(401, b'{"error":"unauthorized"}',
                           headers={"WWW-Authenticate": "Bearer"}, close=True)
                return False
            return True

        def do_GET(self):
            if not self._check_host_origin():
                return
            if not self._check_auth():
                return
            route = urlsplit(self.path).path
            if route == "/health":
                self._send(200, b'{"ok":true}')
            elif route == path:
                # MCP Streamable HTTP：不支持 SSE 的服务对 GET /mcp 明确 405。
                self._send(405, b'{"error":"method not allowed"}', headers={"Allow": "POST"})
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if urlsplit(self.path).path != path:
                self._send(404, b'{"error":"not found"}')
                return
            if not self._check_host_origin():
                return
            if not self._check_auth():
                return
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                # 浏览器跨站"简单请求"只能发 text/plain 等，要求 JSON 直接挡掉 CSRF 面。
                self._send(415, b'{"error":"unsupported media type"}')
                return
            raw_len = self.headers.get("Content-Length")
            if raw_len is None:
                # 缺失/负值都读不了确定长度：按无效请求拒绝
                self._send(400, b'{"error":"missing Content-Length"}')
                return
            try:
                length = int(raw_len)
            except ValueError:
                self._send(400, b'{"error":"bad Content-Length"}')
                return
            if length < 0:
                self._send(400, b'{"error":"bad Content-Length"}')
                return
            if length > MAX_BODY_BYTES:
                # 声明超限直接拒，不等 body：读完才报错等于先被耗一轮内存
                self._send(413, b'{"error":"request body too large"}', close=True)
                return
            try:
                body = self.rfile.read(length)
            except TimeoutError:
                self._send(408, b'{"error":"request timeout"}', close=True)
                return
            except OSError:
                self._send(400, b'{"error":"request read failed"}', close=True)
                return
            if len(body) != length:
                self._send(400, b'{"error":"incomplete request body"}', close=True)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send(400, b'{"error":"invalid json"}')
                return
            try:
                response = server.handle(payload)
            except Exception:
                # 兜底：handle 内部异常也不能断连裸奔
                response = {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32603, "message": "服务端内部错误"}}
            if response is None:
                self._send(202, b"")
                return
            self._send(200, json.dumps(response, ensure_ascii=False).encode("utf-8"))

        def log_message(self, format, *args):
            return

    bearer = (bearer_token or os.environ.get("GUIJU_MCP_BEARER_TOKEN", "")).strip()
    allow_bearer = bool(bearer)
    if not _is_loopback(host):
        if not allow_remote:
            raise ValueError(
                "MCP HTTP 绑定非回环地址必须显式传 --allow-remote"
            )
        if not allow_bearer:
            raise ValueError(
                "MCP HTTP 远程绑定必须设置 GUIJU_MCP_BEARER_TOKEN（Bearer 鉴权）"
            )
    httpd = _BoundedThreadingHTTPServer((host, port), Handler, max_workers=max_workers)
    httpd.require_bearer = allow_bearer
    httpd.bearer_token = bearer
    actual_port = int(httpd.server_address[1])
    httpd.request_timeout = request_timeout
    httpd.allowed_hosts = _default_allowed_hosts(host, actual_port)
    for raw in _configured_allowlist(allowed_hosts, "GUIJU_MCP_ALLOWED_HOSTS"):
        canonical = _canonical_host(raw)
        if canonical:
            httpd.allowed_hosts.add(canonical)
    httpd.allowed_origins = {
        f"http://{item}:{actual_port}" for item in ("localhost", "127.0.0.1", "[::1]")
    }
    for raw in _configured_allowlist(allowed_origins, "GUIJU_MCP_ALLOWED_ORIGINS"):
        canonical = _canonical_origin(raw)
        if canonical:
            httpd.allowed_origins.add(canonical)
    return httpd


_LOOPBACK_HOSTS = {"localhost"}


def _is_loopback(host: str) -> bool:
    """127.x / ::1 全段都算回环（不只 127.0.0.1）；非 IP 字面量按名字表判。"""
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve_http(
    server: McpServer,
    host: str,
    port: int,
    path: str = DEFAULT_PATH,
    *,
    allowed_hosts: list[str] | None = None,
    allowed_origins: list[str] | None = None,
    allow_remote: bool = False,
    bearer_token: str | None = None,
) -> int:
    httpd = make_http_server(
        server, host, port, path,
        allowed_hosts=allowed_hosts, allowed_origins=allowed_origins,
        allow_remote=allow_remote, bearer_token=bearer_token,
    )
    print(f"guiju mcp listening on http://{host}:{port}{path}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        server.close()
    return 0
