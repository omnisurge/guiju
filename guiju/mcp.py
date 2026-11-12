"""guiju MCP 服务面：query / verify / batch / announcement 的纯标准库 JSON-RPC。

默认 stdio；--http 时提供 POST /mcp。业务后端只依赖 Currency.lookup，
不在这里复制适配器、正文下载或旧 spec-currency 逻辑。
announcement 只回公告元数据（编号/名称/可得性/附件链接）；会抓公告页解析附件，但不下载正文、不写盘。
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ._version import __version__
from .currency.model import Currency, Record
from .currency.native import NativeCurrency
from .errors import GuijuError, NeedsHuman

PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PATH = "/mcp"
# POST body 硬上限：Content-Length 取客户端声明，不设限可被大 body 撑爆内存。
# JSON-RPC 请求正常只有几 KB，4MB 已是很宽的余量。
MAX_BODY_BYTES = 4 * 1024 * 1024

TOOLS = (
    {
        "name": "query",
        "description": "查询规范现行状态：输入规范号，返回全部命中版本及统一状态。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "cache": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "verify",
        "description": "核验规范状态（jl-docx code_refs 用）：返回 code<TAB>status。",
        "inputSchema": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "cache": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "batch",
        "description": "批量盘点规范清单：输入清单文件路径，执行批量查询，返回统计。",
        "inputSchema": {
            "type": "object",
            "properties": {"list": {"type": "string"}, "out": {"type": "string"}, "cache": {"type": "string"}},
            "required": ["list"],
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
    return _error_result(exc.message)


class _ReadOnlyStore:
    """announcement 工具专用的只读存储门面。

    契约是"只回元数据、不落盘"：resolve 在读路径上若走到缓存/持久化分支，
    会写 edition / mention / scan / currency_cache / corpus_meta。
    这里把所有写方法拦成 no-op（或返回"什么都没写"的占位），读方法原样转发，
    用白名单比黑名单抗未来改动 —— 新出现的写方法默认也被拦。
    注意：Store() 构造时的建表 DDL 在门面生效前已发生，属共享 schema 初始化，不在拦截范围。
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


class McpServer:
    """与传输无关的 JSON-RPC 处理核心。"""

    def __init__(self, backend: Currency) -> None:
        self.backend = backend
        # announcement 的共享只读存储：避免每请求跑一次建表/迁移 DDL。
        # CPython sqlite3 模块 threadsafety=1，共享连接必须外部串行化 ——
        # self._store_lock 包住"创建 + 每次 _announcement 调用"（含 detail 抓取，
        # announcement 本身低频，串行可接受）。
        self._store = None
        self._store_lock = threading.Lock()

    def _ann_store(self):
        """惰性创建共享 Store（cross_thread 允许连接跨线程使用）。"""
        if self._store is None:
            from .store.sqlite import Store

            self._store = Store(cross_thread=True)
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
            return _text_result(_rows_text(self.backend.lookup(code)))
        except GuijuError as exc:
            return _as_error(exc)

    def _verify(self, arguments: dict) -> dict:
        code = str(arguments.get("code") or "").strip()
        if not code:
            return _error_result("缺少 code 参数")
        try:
            return _text_result(_rows_text(self.backend.lookup(code), verify=True))
        except GuijuError as exc:
            return _as_error(exc)

    def _batch(self, arguments: dict) -> dict:
        list_path = str(arguments.get("list") or "").strip()
        if not list_path:
            return _error_result("缺少 list 参数")
        try:
            lines = Path(list_path).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return _error_result(f"清单不可读：{exc}")
        codes = [line.split("#", 1)[0].strip() for line in lines]
        codes = [code for code in codes if code]
        if not codes:
            return _error_result("清单为空")
        completed = skipped = failed = unmatched = 0
        out_rows: list[str] = []
        for code in codes:
            try:
                rows = self.backend.lookup(code)
            except GuijuError:
                failed += 1
                out_rows.append(f"{code}\t查询失败")
                continue
            if not rows:
                unmatched += 1
                continue
            for row in rows:
                if row.status == "unknown":
                    skipped += 1
                else:
                    completed += 1
                out_rows.append("\t".join((code, row.code, row.status, row.name, row.implement_date, row.source)))
        out_path = str(arguments.get("out") or "").strip()
        if out_path:
            try:
                Path(out_path).write_text(
                    "# format: guiju-mcp-batch-v1\n# 输入\t规范号\t状态\t名称\t实施日期\t数据源\n"
                    + "\n".join(out_rows) + ("\n" if out_rows else ""),
                    encoding="utf-8",
                )
            except OSError as exc:
                return _error_result(f"盘点表写入失败：{exc}")
        return _text_result(
            f"总数 {len(codes)} | 完成 {completed} | 跳过 {skipped} | 失败 {failed} | 无源 {unmatched} | 盘点表 {out_path or '(未输出文件，仅统计)'}"
        )

    def _announcement(self, arguments: dict) -> dict:
        """公告元数据解析：与 pull 同源定位，但只回链接，不下文件、不写盘。"""
        ref = str(arguments.get("ref") or "").strip()
        if not ref:
            return _error_result("缺少 ref 参数")
        from .cli import lookup
        from .domain import attachment as att_domain
        from .domain import availability
        from .sources import registry

        try:
            with self._store_lock:
                # 契约是"只回元数据、不落盘"：allow_network=False 堵死 resolve 里唯一的写分支
                # （拉详情页 + persist_codes），只读门面把缓存/持久化调用拦成 no-op 兜底。
                ro = _ReadOnlyStore(self._ann_store())
                target = lookup.resolve(
                    ro, ref, currency=self.backend, allow_network=False)
                ann = target["ann"]
                window = availability.state(ann["published_on"])
                source_id = lookup.source_of(ann["url"])["id"]
                source = registry().get(source_id)
                if source is None:
                    # 公告 URL 的 host 不在源表（_SOURCE_HOSTS）里：宁可显式报错
                    # 也不能静默拿 mohurd 去解析一个根本不是它的页面结构。
                    raise NeedsHuman(
                        f"公告页归属站点无法识别（source_id={source_id}，url={ann['url']}）",
                        hint="若站点改了域名请在 guiju/cli/lookup.py 的 _SOURCE_HOSTS 补映射，"
                             "或为新站点补适配器",
                    )
                choice = None
                candidates: list = []
                if getattr(source, "api_mode", False):
                    # API 源没有公告页可抓（shzjw）：正文链接在检索结果里，不在页面上
                    verdict = "api_mode"
                else:
                    html, _ = source.detail(ann["url"])
                    atts = source.attachments(html)
                    picked = att_domain.pick(
                        atts, ann["doc_name"] or "", announcement_title=ann["title"])
                    choice = picked.choice if picked.ok else None
                    candidates = (picked.as_payload() or {}).get("candidates") or []
                    verdict = "ok" if picked.ok else getattr(
                        picked.verdict, "value", str(picked.verdict))
                payload = {
                    "ok": True,
                    "announcement_id": ann["id"],
                    "code": target.get("code_display") or target.get("code_key"),
                    "name": ann["doc_name"],
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
        if method == "initialize":
            requested = str((payload.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSIONS[-1])
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
            params = payload.get("params") or {}
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
            response = server.handle(payload)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        server.close()
    return 0


def default_backend() -> NativeCurrency:
    from .currency import native_currency

    return native_currency()


def make_http_server(server: McpServer, host: str, port: int, path: str = DEFAULT_PATH) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "guiju-mcp"

        def _send(self, code: int, body: bytes, content_type: str = "application/json", *, close: bool = False) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if close:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except BrokenPipeError:
                pass
            finally:
                if close:
                    self.close_connection = True

        def do_GET(self):
            if urlsplit(self.path).path == "/health":
                self._send(200, b'{"ok":true}')
            else:
                self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            if urlsplit(self.path).path != path:
                self._send(404, b'{"error":"not found"}')
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
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
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

    if not _is_loopback(host):
        # 不阻断（可能是内网/容器部署），但 MCP 无任何鉴权，必须提示
        print("! MCP HTTP 暴露在非回环地址，确认有网关/鉴权保护", file=sys.stderr, flush=True)
    return ThreadingHTTPServer((host, port), Handler)


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


def serve_http(server: McpServer, host: str, port: int, path: str = DEFAULT_PATH) -> int:
    httpd = make_http_server(server, host, port, path)
    print(f"guiju mcp listening on http://{host}:{port}{path}", file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        server.close()
    return 0
