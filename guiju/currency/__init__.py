"""时效端口与兼容门面 —— ADR 0002 M1 只搬包，不切默认适配器。

原生 csres 位于 .csres，须显式构造；默认 cmd / mcp / off 选择与缓存保持原样。

借用 spec-currency 的权威结论，替代本地"编号索引"。

为什么 index 要放弃
------------------
index 的本质是**在本地维护一份"公告 ⇄ 编号"的映射**，而这份映射上游并不提供
（住建部列表接口只给标题，编号只在详情页正文）。维护它意味着：每有新公告就要扫一遍，
站点改版还要重扫 —— 这正是"持续维护"的成本。

而 spec-currency 已经用**权威源**回答了同一个问题：给一个编号，它返回
`code / name / status(现行/被替代/即将实施/废止) / implement_date / source`。
有了名称，"编号 → 公告"就退化成一次本地检索（名称检索，不需要任何预建索引）。

合规分层（重要）
----------------
`compliance.py` 的白名单只约束**内容抓取**（正文只从 *.gov.cn 来）。
本模块只向一个已审批的元数据服务取"编号/名称/状态"，不下载任何标准正文、不落文件，
因此不走 compliance 的闸门 —— 这条边界是刻意的，**不要把它当成合规豁免来复用**。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..errors import SourceUnreachable
from .model import Currency as Currency
from .model import Record as Record

# 公共端点走 HTTPS + Bearer（GUIJU_CURRENCY_TOKEN）；原 NAS :18083 已于 2026-11-01 退役。
# 自托管的裸 HTTP 端点（如 hk 本机 127.0.0.1:18083）用 GUIJU_CURRENCY_URL 覆盖。
# 常量本身存 punycode（等价于 guiju.守夜人.中国），避免任何一层把原始
# IDN 直接喂给 http.client；用户自定义 URL 由 McpCurrency 构造器统一归一化。
DEFAULT_MCP_URL = "https://guiju.xn--gmqx6we1b.xn--fiqs8s/mcp"
DEFAULT_CMD = "spec-currency"
TIMEOUT = 25


def native_currency(cache_dir: str | None = None):
    """显式构造原生时效适配器；缓存目录可用参数或 GUIJU_CURRENCY_CACHE 覆盖。

    本层生效的缓存开关：
    - ``GUIJU_CURRENCY_CACHE=off``：选 no-cache 适配器（等价于 cache_dir="off"）。
    - ``GUIJU_CURRENCY_CACHE_OFF=1``：全局禁原生磁盘缓存，优先级最高；
      同一变量对 cmd/mcp 端点的作用见 CommandCurrency/McpCurrency。
    """
    from ..store.currency import DiskCurrencyCache
    from .native import NativeCurrency

    if os.environ.get("GUIJU_CURRENCY_CACHE_OFF") == "1":
        return NativeCurrency()
    selected = cache_dir if cache_dir is not None else os.environ.get("GUIJU_CURRENCY_CACHE")
    if selected == "off":
        return NativeCurrency()
    root = os.environ.get("GUIJU_HOME") or os.path.expanduser("~/.guiju")
    directory = Path(selected) if selected else Path(root) / "currency-cache"
    return NativeCurrency(cache=DiskCurrencyCache(directory))


class NullCurrency:
    """没有可用时效服务时的兜底：不猜、不报错，只是没有结论。"""

    name = "none"

    def lookup(self, code: str) -> list[Record]:
        return []


def parse_tsv(text: str) -> list[Record]:
    """解析 spec-currency 的 TSV 契约。

    列：`code \t status \t name \t implement_date \t source`
    `#` 开头的行是"结果可能不可信"的提示（降级/备用源），按契约必须跳过。
    """
    out: list[Record] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        out.append(
            Record(
                code=cols[0].strip(),
                status=cols[1].strip().lower(),
                name=cols[2].strip() if len(cols) > 2 else "",
                implement_date=cols[3].strip() if len(cols) > 3 else "",
                source=cols[4].strip() if len(cols) > 4 else "",
            )
        )
    return out


class CommandCurrency:
    """调用本地 spec-currency 二进制（最稳，离线 NAS 也能用）。"""

    name = "cmd"

    def __init__(self, binary: str) -> None:
        self.binary = binary

    def lookup(self, code: str) -> list[Record]:
        argv = [self.binary, "query", code]
        if os.environ.get("GUIJU_CURRENCY_CACHE_OFF") == "1":
            argv += ["--cache", "off"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceUnreachable(f"spec-currency 不可执行（{self.binary}）：{exc}") from exc
        if proc.returncode != 0 and not proc.stdout.strip():
            raise SourceUnreachable(
                f"spec-currency 执行失败（exit={proc.returncode}）：{proc.stderr.strip()[:120]}"
            )
        return parse_tsv(proc.stdout)


def _idna_url(url: str) -> str:
    """把 URL 里的 IDN 域名转成 punycode，其余部分原样保留。

    http.client 写 Host 头用 latin-1 编码，中文域名（如 guiju.守夜人.中国）会在
    第一次请求时就抛 UnicodeEncodeError，被统一归为 SourceUnreachable —— 整个
    mcp 路径必死。域名交给 DNS 前必须是 ASCII，所以构造时统一归一化。
    """
    parts = urlsplit(url)
    host = parts.hostname
    if not host or host.isascii():
        return url
    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += ":" + parts.password
        userinfo += "@"
    port = f":{parts.port}" if parts.port is not None else ""
    netloc = f"{userinfo}{host.encode('idna').decode('ascii')}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class McpCurrency:
    """通过 spec-currency 的 MCP server（streamable-http）查询。"""

    name = "mcp"

    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = _idna_url(url)
        self.token = token

    def _token(self) -> str | None:
        # 环境变量在请求时才读：进程启动后补设 token 不需要重启
        return self.token or os.environ.get("GUIJU_CURRENCY_TOKEN") or None

    def _post(self, payload: dict, session: str | None = None) -> tuple[str, str | None]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2024-11-05",
        }
        if session:
            headers["mcp-session-id"] = session
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "ignore")
                sid = resp.headers.get("mcp-session-id") or session
        except Exception as exc:  # 网络异常种类多，统一归为不可达
            # 关键区分：连不上 ≠ 没有这个编号。前者是 20（可重试），后者才是 31（别再试）
            raise SourceUnreachable(f"时效服务不可达（{self.url}）：{type(exc).__name__}") from exc
        return body, sid

    def _rpc(self, payload: dict, session: str | None = None) -> tuple[dict | None, str | None]:
        body, sid = self._post(payload, session)
        parsed = _parse_sse(body)
        if parsed is None:
            raise SourceUnreachable(f"时效服务响应无法解析（{self.url}）")
        if payload.get("method") == "initialize" and isinstance(parsed, dict) and "error" not in parsed:
            # MCP 要求握手后补 notifications/initialized；fire-and-forget：
            # 服务器不回/拒收不影响握手本身，绝不能因此把可用服务报成不可达。
            try:
                self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            except SourceUnreachable:
                pass
        return parsed, sid

    def lookup(self, code: str, *, cache: str | None = None) -> list[Record]:
        arguments = {"code": code}
        # 显式 cache 参数优先；未显式时 GUIJU_CURRENCY_CACHE_OFF=1 透传成 cache=off，
        # 与 CommandCurrency 的 --cache off 同层同义（都在端点适配器层生效）。
        effective = cache if cache is not None else (
            "off" if os.environ.get("GUIJU_CURRENCY_CACHE_OFF") == "1" else None
        )
        if effective is not None:
            arguments["cache"] = effective
        init, sid = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "guiju", "version": "0.3.0"},
                },
            }
        )
        if init is None or "error" in init:
            err = (init or {}).get("error") or {}
            raise SourceUnreachable(
                f"时效服务握手失败（{self.url}）：{err.get('code', '?')} {err.get('message', '无响应')}"
            )
        result, _ = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "query", "arguments": arguments},
            },
            sid,
        )
        if result is not None and "error" in result:
            # JSON-RPC 错误是"服务活着但调用失败"——绝不能静默当"没查到"（31）处理
            err = result["error"] or {}
            raise SourceUnreachable(
                f"时效服务调用失败（{self.url}）：{err.get('code', '?')} {err.get('message', '未知错误')}"
            )
        if result is not None:
            tool_result = result.get("result")
            if isinstance(tool_result, dict) and tool_result.get("isError"):
                # MCP 工具层失败（isError:true）也是调用失败，同样不得伪装成"未收录"
                detail = _tool_text(result).strip()
                raise SourceUnreachable(f"时效服务工具调用失败（{self.url}）：{detail or 'isError'}")
        if not result:
            return []
        text = _tool_text(result)
        return parse_tsv(text)


def _parse_sse(body: str) -> dict | None:
    """解析 SSE 或纯 JSON 两种响应形态。"""
    body = body.strip()
    if body.startswith("{"):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
    return None


def _tool_text(result: dict) -> str:
    out = result.get("result") or {}
    parts = []
    for item in out.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


_cache: dict[tuple[str, str], list[Record]] = {}


class MisconfiguredCurrency:
    """显式指定了适配器但它不可用：**查询时报错，绝不悄悄换成别的源**。

    这曾经是个真问题：`GUIJU_CURRENCY=cmd` 而机器上没装 spec-currency 时，
    代码直接往下走到自动探测，最终用了 mcp —— 用户以为走的是本地二进制，
    实际用的是另一条链路。配置错了就要说出来。
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def lookup(self, code: str) -> list[Record]:
        raise SourceUnreachable(
            self.reason,
            hint="可用值：cmd[:二进制路径] / mcp[:URL] / native[:off|缓存目录] / off；或先装好对应组件",
        )


def endpoint_of(cur: object) -> str | None:
    """当前适配器实际使用的端点（URL / 二进制 / 缺失原因），用于 status 与 --json 结果。

    没有这个字段时，"我设了 GUIJU_CURRENCY_URL 但好像没生效"只能靠猜。
    """
    url = getattr(cur, "url", None)
    if url:
        return str(url)
    binary = getattr(cur, "binary", None)
    if binary:
        return str(binary)
    reason = getattr(cur, "reason", None)
    if reason:
        return f"不可用：{reason}"
    if getattr(cur, "name", "") == "native":
        return "native"
    return None


def default_currency() -> Currency:
    """默认使用原生时效；显式 cmd / mcp / native / off 仍可覆盖，环境变量绝不静默替换。

    这是 ADR 0002 期1 M4 的默认切换点：原生内部按 TTL 管理缓存，
    不再外包旧 CachedCurrency 的进程内 memo。

    ``native[:arg]`` 的 arg 是原生磁盘缓存目录（``native:off`` 等价关缓存），
    与 cmd/mcp 的「机器地址」语义不同 —— 原生没有远端可指，只有缓存位置可调。
    ``GUIJU_CURRENCY_CACHE`` / ``GUIJU_CURRENCY_CACHE_OFF=1`` 在 native_currency 层生效。
    """
    override = os.environ.get("GUIJU_CURRENCY")  # cmd[:path] / mcp[:url] / native[:off|dir] / off
    if override:
        kind, _, arg = override.partition(":")
        if kind == "off":
            return NullCurrency()
        if kind == "cmd":
            binary = arg or shutil.which(DEFAULT_CMD)
            if not binary:
                return MisconfiguredCurrency(
                    "cmd(缺失)", f"GUIJU_CURRENCY=cmd，但找不到 {DEFAULT_CMD} 可执行文件"
                )
            return CommandCurrency(binary)
        if kind == "mcp":
            # GUIJU_CURRENCY=mcp 曾经会忽略 GUIJU_CURRENCY_URL（用户设了 URL 却被静默丢弃，
            # 实际连的是默认 NAS）。显式指定机制时，环境变量的每一部分都要生效。
            return McpCurrency(arg or os.environ.get("GUIJU_CURRENCY_URL") or DEFAULT_MCP_URL)
        if kind == "native":
            return native_currency(arg or None)
        return MisconfiguredCurrency(f"{kind}(未知)", f"GUIJU_CURRENCY={override} 无法识别")
    return native_currency()


class CachedCurrency:
    """两级缓存：进程内（同一次命令不问两次）+ 落库（下次命令、以及服务不可达时）。

    落库这一层解决一个真实问题：时效服务在私网里，断了以后"编号 → 名称"就没法回答，
    但同一台机器上昨天刚查过的编号，没理由今天答不出来。缓存只存元数据，不存正文。
    """

    def __init__(self, inner: Currency | None = None, store: object | None = None) -> None:
        self.inner = inner or default_currency()
        self.name = self.inner.name
        self.store = store
        self.from_cache = False
        self.stale = False
        self.upstream_error: Exception | None = None
        self.cache_age_days: float | None = None

    def lookup(self, code: str) -> list[Record]:
        # 每次都重置：上一题的"缓存命中/降级"绝不能污染本题。
        self.from_cache = False
        self.stale = False
        self.upstream_error = None
        self.cache_age_days = None
        if self.inner.name == "native":
            # 原生内部已按 TTL 管理缓存；不能再外包旧 CachedCurrency 的无 TTL memo。
            records = self.inner.lookup(code)
            self.from_cache = bool(getattr(self.inner, "from_cache", False))
            self.stale = bool(getattr(self.inner, "stale", False))
            self.upstream_error = getattr(self.inner, "upstream_error", None)
            self.cache_age_days = getattr(self.inner, "cache_age_days", None)
            return records
        # 进程级 memo 的 key 必须带端点：同进程换 GUIJU_CURRENCY（cmd↔mcp↔换 URL）
        # 不能用旧端点的结论回答新端点的问题。
        key = (endpoint_of(self.inner), self.name, code)
        hit = _cache.get(key)
        if hit is not None:
            records, self.from_cache, self.stale, self.upstream_error, self.cache_age_days = hit
            return list(records)
        try:
            records = self.inner.lookup(code)
        except SourceUnreachable as exc:
            hit = self._load(code)
            if hit is None:
                raise
            records, self.cache_age_days = hit
            self.from_cache = True
            self.stale = True  # 落库缓存无 TTL：只有上游刚失败才会走到这里 → 一律按陈旧上报
            self.upstream_error = exc
            _cache[key] = (records, True, True, exc, self.cache_age_days)
            return records
        if records:
            self._save(code, records)
        _cache[key] = (records, self.from_cache, self.stale, self.upstream_error, self.cache_age_days)
        return records

    # ---- 落库缓存 ----
    @staticmethod
    def _key(code: str) -> str:
        from ..domain.codes import normalize_key

        return normalize_key(code)

    def _save(self, code: str, records: list[Record]) -> None:
        if self.store is None:
            return
        import json

        try:
            self.store.cache_currency(  # type: ignore[attr-defined]
                self._key(code), json.dumps([r.as_payload() for r in records], ensure_ascii=False)
            )
        except Exception:  # 缓存失败是可容忍的：不要让它毁掉一次正常查询
            return

    def _load(self, code: str) -> tuple[list[Record], float | None] | None:
        if self.store is None:
            return None
        import json

        try:
            hit = self.store.cached_currency(self._key(code))  # type: ignore[attr-defined]
        except Exception:
            return None
        if not hit:
            return None
        payload, fetched_at = hit
        try:
            rows = json.loads(payload)
            records = [
                Record(
                    code=row.get("code", ""),
                    status=row.get("status", "unknown"),
                    name=row.get("name", ""),
                    implement_date=row.get("implement_date", ""),
                    source=row.get("source", ""),
                    replaces=row.get("replaces", ""),
                )
                for row in rows
            ]
        except Exception:
            return None
        age = None
        try:
            from datetime import datetime

            raw_seconds = (datetime.now() - datetime.strptime(
                fetched_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if raw_seconds < -3600:
                # 时钟回拨/未来时间戳超过 1h：年龄不可信，保持 None（调用方按"无年龄信息"处理），
                # 不钳成 0 伪装成"刚写入"。小幅负值（时钟漂移）则钳 0，不误报。
                age = None
            else:
                age = max(0.0, round(raw_seconds / 86400, 2))
        except (ValueError, TypeError):
            pass
        return records, age
