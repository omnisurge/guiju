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
    """薄透传时效包装（1.3.0 起不再维护旧进程/SQLite 缓存，由 NativeCurrency 管理 TTL 磁盘缓存）。

    保留 ``CachedCurrency(inner=..., store=...)`` 签名以兼容 S3c 退场前的入口与测试注入；
    每次 ``lookup()`` 先重置 7 个状态属性再透传内层状态。
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
        records = self.inner.lookup(code)
        self.from_cache = bool(getattr(self.inner, "from_cache", False))
        self.stale = bool(getattr(self.inner, "stale", False))
        self.upstream_error = getattr(self.inner, "upstream_error", None)
        self.cache_age_days = getattr(self.inner, "cache_age_days", None)
        self.complete = getattr(self.inner, "complete", None)
        self.empty_authoritative = bool(getattr(self.inner, "empty_authoritative", False))
        self.insecure_transport = bool(getattr(self.inner, "insecure_transport", False))
        return records
