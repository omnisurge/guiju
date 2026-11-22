"""时效端口 —— spec-currency 已退役（2026-11-13），默认且唯一上游是 native。

GUIJU_CURRENCY 只接受 native[:off|dir] 与 off；其他值报 20，不再静默换源。

用权威时效服务的结论替代本地"编号索引"。

为什么 index 要放弃
------------------
index 的本质是**在本地维护一份"公告 ⇄ 编号"的映射**，而这份映射上游并不提供
（住建部列表接口只给标题，编号只在详情页正文）。维护它意味着：每有新公告就要扫一遍，
站点改版还要重扫 —— 这正是"持续维护"的成本。

而权威时效服务（当前实现是 native 路由族）已经回答了同一个问题：给一个编号，它返回
`code / name / status(现行/被替代/即将实施/废止) / implement_date / source`。
有了名称，"编号 → 公告"就退化成一次本地检索（名称检索，不需要任何预建索引）。

合规分层（重要）
----------------
`compliance.py` 的白名单只约束**正文下载**（正文只从 *.gov.cn 来）。
时效适配器只向各自的元数据源取"编号/名称/状态"，不下载任何标准正文、不落文件，
因此不走 compliance 的闸门 —— 这条边界是刻意的，**不要把它当成合规豁免来复用**。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import SourceUnreachable
from .model import Currency as Currency
from .model import Record as Record


def native_currency(cache_dir: str | None = None):
    """显式构造原生时效适配器；缓存目录可用参数或 GUIJU_CURRENCY_CACHE 覆盖。

    本层生效的缓存开关：
    - ``GUIJU_CURRENCY_CACHE=off``：选 no-cache 适配器（等价于 cache_dir="off"）。
    - ``GUIJU_CURRENCY_CACHE_OFF=1``：全局禁原生磁盘缓存，优先级最高。
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


_cache: dict[tuple[object, ...], tuple] = {}


class MisconfiguredCurrency:
    """显式指定了适配器但它不可用：**查询时报错，绝不悄悄换成别的源**。

    这曾经是个真问题：配置没生效时绝不能静默换源，否则会拿另一条链路的
    结论冒充用户指定的那条 —— 配置错了就要说出来。
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason

    def lookup(self, code: str) -> list[Record]:
        raise SourceUnreachable(
            self.reason,
            hint="GUIJU_CURRENCY 可用值：native[:off|缓存目录] / off（spec-currency 已退役）",
        )


def endpoint_of(cur: object) -> str | None:
    """当前适配器实际使用的端点（native / 不可用原因），用于 status 与 --json 结果。

    没有这个字段时，"我设了 GUIJU_CURRENCY 但好像没生效"只能靠猜。
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
    """默认原生时效；GUIJU_CURRENCY 只认 native[:off|dir] 与 off，其他值报 20。

    spec-currency 已退役（2026-11-13）：显式的 cmd / mcp 不再可用，
    GUIJU_CURRENCY_URL / GUIJU_CURRENCY_TOKEN 成为死配置，一律报「无法识别」。

    ``native[:arg]`` 的 arg 是原生磁盘缓存目录（``native:off`` 等价关缓存），
    原生没有远端可指，只有缓存位置可调。
    ``GUIJU_CURRENCY_CACHE`` / ``GUIJU_CURRENCY_CACHE_OFF=1`` 在 native_currency 层生效。
    """
    override = os.environ.get("GUIJU_CURRENCY")  # native[:off|dir] / off
    if override:
        kind, _, arg = override.partition(":")
        if kind == "off":
            return NullCurrency()
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
        # W3/P1-4：结果是否完整可信（False = 没取全、未写缓存）。
        # None 表示内层适配器没有完整性契约（老实现），调用方不得当成"已完整"。
        self.complete: bool | None = None
        # W3-2：空结果是否来自源明确回答"没有"（可与 complete=False 并存）。
        self.empty_authoritative = False
        self.insecure_transport = False

    def lookup(self, code: str) -> list[Record]:
        # 每次都重置：上一题的"缓存命中/降级"绝不能污染本题。
        self.from_cache = False
        self.stale = False
        self.upstream_error = None
        self.cache_age_days = None
        self.complete = None
        self.empty_authoritative = False
        self.insecure_transport = False
        if isinstance(self.inner, MisconfiguredCurrency):
            # W3-7：显式配置错误必须立刻报 20；旧缓存不是"换个源"的理由。
            self.inner.lookup(code)  # 总是抛 SourceUnreachable
        if self.inner.name == "native":
            # 原生内部已按 TTL 管理缓存；不能再外包旧 CachedCurrency 的无 TTL memo。
            records = self.inner.lookup(code)
            self.from_cache = bool(getattr(self.inner, "from_cache", False))
            self.stale = bool(getattr(self.inner, "stale", False))
            self.upstream_error = getattr(self.inner, "upstream_error", None)
            self.cache_age_days = getattr(self.inner, "cache_age_days", None)
            self.complete = getattr(self.inner, "complete", None)
            self.empty_authoritative = bool(getattr(self.inner, "empty_authoritative", False))
            self.insecure_transport = bool(getattr(self.inner, "insecure_transport", False))
            return records
        # 进程级 memo 的 key 必须带端点：同进程换 GUIJU_CURRENCY
        # 不能用旧端点的结论回答新端点的问题。
        key = (endpoint_of(self.inner), self.name, code)
        hit = _cache.get(key)
        if hit is not None:
            (
                records, self.from_cache, self.stale, self.upstream_error,
                self.cache_age_days, self.complete, self.empty_authoritative,
                self.insecure_transport,
            ) = hit
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
            _cache[key] = (records, True, True, exc, self.cache_age_days, None, False, False)
            return records
        self.complete = getattr(self.inner, "complete", None)
        self.empty_authoritative = bool(getattr(self.inner, "empty_authoritative", False))
        self.insecure_transport = bool(getattr(self.inner, "insecure_transport", False))
        # 不完整的答案不落库（P1-4）：否则下次进程会在 24 小时缓存里
        # 把"没取全"当成"没有"。complete=None 是老适配器契约，按完整处理。
        # 明文回退（含空结果）永远不写缓存（W3-2 的 cacheable 口径）。
        if records and self.complete is not False and not self.insecure_transport:
            self._save(code, records)
        _cache[key] = (
            records, self.from_cache, self.stale, self.upstream_error,
            self.cache_age_days, self.complete, self.empty_authoritative,
            self.insecure_transport,
        )
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
                self._key(code), json.dumps([r.cache_payload() for r in records], ensure_ascii=False)
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
                    fallback=row.get("fallback", ""),
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
            # 时钟回拨/未来时间戳超过 1h：年龄不可信，保持 None（调用方按"无年龄信息"处理），
            # 不钳成 0 伪装成"刚写入"。小幅负值（时钟漂移）则钳 0，不误报。
            age = None if raw_seconds < -3600 else max(0.0, round(raw_seconds / 86400, 2))
        except (ValueError, TypeError):
            pass
        return records, age
