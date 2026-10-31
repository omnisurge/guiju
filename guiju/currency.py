"""时效端口 —— 借用 spec-currency 的权威结论，替代本地"编号索引"。

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
from dataclasses import dataclass
from typing import Protocol

from .errors import SourceUnreachable

DEFAULT_MCP_URL = "http://100.66.1.x:18083/mcp"
DEFAULT_CMD = "spec-currency"
TIMEOUT = 25


@dataclass(frozen=True)
class Record:
    code: str
    status: str          # current / superseded / upcoming / withdrawn / unknown
    name: str = ""
    implement_date: str = ""
    source: str = ""
    replaces: str = ""

    @property
    def is_current(self) -> bool:
        return self.status == "current"

    def as_payload(self) -> dict:
        return {
            "code": self.code,
            "status": self.status,
            "name": self.name,
            "implement_date": self.implement_date,
            "source": self.source,
        }


class Currency(Protocol):
    name: str

    def lookup(self, code: str) -> list[Record]: ...


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


class McpCurrency:
    """通过 spec-currency 的 MCP server（streamable-http）查询。"""

    name = "mcp"

    def __init__(self, url: str) -> None:
        self.url = url

    def _rpc(self, payload: dict, session: str | None = None) -> tuple[dict | None, str | None]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session:
            headers["mcp-session-id"] = session
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
        parsed = _parse_sse(body)
        if parsed is None:
            raise SourceUnreachable(f"时效服务响应无法解析（{self.url}）")
        return parsed, sid

    def lookup(self, code: str) -> list[Record]:
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
        if init is None:
            return []
        result, _ = self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "query", "arguments": {"code": code}},
            },
            sid,
        )
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
            hint="可用值：cmd[:二进制路径] / mcp[:URL] / off；或先装好对应组件",
        )


def default_currency() -> Currency:
    """优先本地二进制（快、可离线），否则 MCP 服务，最后退化为空。

    注意：**环境变量一旦显式指定，要么照办，要么报错**，不做静默替换。
    """
    override = os.environ.get("GUIJU_CURRENCY")  # cmd[:path] / mcp[:url] / off
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
            return McpCurrency(arg or DEFAULT_MCP_URL)
        return MisconfiguredCurrency(f"{kind}(未知)", f"GUIJU_CURRENCY={override} 无法识别")
    binary = shutil.which(DEFAULT_CMD)
    if binary:
        return CommandCurrency(binary)
    if os.environ.get("GUIJU_CURRENCY_URL") or DEFAULT_MCP_URL:
        return McpCurrency(os.environ.get("GUIJU_CURRENCY_URL") or DEFAULT_MCP_URL)
    return NullCurrency()


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
        self.cache_age_days: float | None = None

    def lookup(self, code: str) -> list[Record]:
        key = (self.name, code)
        if key in _cache:
            return _cache[key]
        try:
            records = self.inner.lookup(code)
        except SourceUnreachable:
            hit = self._load(code)
            if hit is None:
                raise
            records, self.cache_age_days = hit
            self.from_cache = True
            _cache[key] = records
            return records
        if records:
            self._save(code, records)
        _cache[key] = records
        return records

    # ---- 落库缓存 ----
    @staticmethod
    def _key(code: str) -> str:
        from .domain.codes import normalize_key

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

            age = round((datetime.now() - datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400, 2)
        except ValueError:
            pass
        return records, age
