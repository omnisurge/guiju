"""原生时效编排：路由/准入 → TTL 缓存 → 上游 → 明示的有界陈旧缓存。

不装配具体存储，不自行落盘；DiskCurrencyCache 由调用者从 store 层显式注入。
本模块不是 CLI 默认实现；M4 逐字段 diff 通过前不得替换 cmd / mcp / off。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, fields

from ..errors import NeedsHuman, NotIndexed, SourceUnreachable
from .cache import DEFAULT_STALE_LIMIT, CacheEntry, CurrencyCache
from .codes import same_family
from .model import Currency, QueryCancelled, Record
from .registry import Registry, default_registry


class NoCurrencySource(NotIndexed):
    hint = "当前原生时效路由未覆盖此前缀；默认外置适配器尚未切换"


def human_age(seconds: float) -> str:
    if seconds < 60:
        return "不到 1 分钟"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时"
    return f"{int(seconds // 86400)} 天"


@dataclass(frozen=True)
class Outcome:
    results: tuple[Record, ...]
    source: str
    from_cache: bool = False
    cache_age: float = 0.0
    stale: bool = False
    upstream_error: Exception | None = None

    @property
    def fallback_source(self) -> str:
        return next((row.fallback.strip() for row in self.results if row.fallback.strip()), "")

    def critical_notes(self) -> list[str]:
        notes = []
        if self.stale:
            notes.append(f"⚠ 上游查询失败，以下为 {human_age(self.cache_age)}前的缓存结果，可能已过期：{self.upstream_error}")
        if self.fallback_source:
            notes.append(f"⚠ 主源不可用，结果由备用源 {self.fallback_source} 提供（覆盖范围与口径可能与主源不同，重要结论建议人工确认）")
        return notes

    def notes(self) -> list[str]:
        notes = self.critical_notes()
        if self.from_cache and not self.stale:
            notes.insert(0, f"命中本地缓存（{human_age(self.cache_age)}前查询，本次未访问上游）")
        return notes

    def annotation(self) -> str:
        parts = []
        if self.stale:
            parts.append(f"[! 降级: {human_age(self.cache_age)}前的缓存]")
        elif self.from_cache:
            parts.append(f"[缓存: {human_age(self.cache_age)}前]")
        if self.fallback_source:
            parts.append(f"[备用源: {self.fallback_source}]")
        return " ".join(parts)


class NativeCurrency:
    name = "native"

    def __init__(self, registry: Registry | None = None, cache: CurrencyCache | None = None, *,
                 clock: Callable[[], float] = time.time, admit: Callable[[str], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> None:
        self.cancelled = cancelled
        self.registry = registry if registry is not None else default_registry(cancelled=cancelled)
        self.cache = cache
        self.clock = clock
        self.admit = admit
        self.last_outcome: Outcome | None = None

    def _guard(self, code: str) -> None:
        if self.cancelled is not None and self.cancelled():
            raise QueryCancelled("查询已取消，不返回缓存结果")
        if self.admit is not None:
            self.admit(code)
        if self.cancelled is not None and self.cancelled():
            raise QueryCancelled("查询已取消，不返回缓存结果")

    def _usable(self, entry: CacheEntry | None, adapter: Currency, code: str) -> bool:
        if not isinstance(entry, CacheEntry) or type(entry.queried_at) not in (int, float):
            return False
        if not math.isfinite(entry.queried_at) or entry.queried_at <= 0 or entry.age(self.clock()) < 0:
            return False
        if not isinstance(entry.results, tuple):
            return False
        allowed = getattr(adapter, "source_names", (adapter.name,))
        for row in entry.results:
            if not isinstance(row, Record) or any(not isinstance(getattr(row, field.name), str) for field in fields(Record)):
                return False
            if row.status not in {"current", "superseded", "upcoming", "withdrawn", "unknown"}:
                return False
            if row.source not in allowed:
                return False
            accepts = getattr(adapter, "accepts_record", None)
            if accepts is not None:
                if not accepts(code, row):
                    return False
            elif not same_family(code, row.code):
                return False
            if row.fallback and (row.fallback not in allowed or row.fallback != row.source):
                return False
            if row.source != adapter.name and not row.fallback:
                return False
        return True

    def _read(self, code: str, namespace: str, *, any_age: bool = False) -> CacheEntry | None:
        if self.cache is None:
            return None
        try:
            method = self.cache.get_any if any_age else self.cache.get
            return method(code, namespace=namespace)
        except OSError:
            return None  # 缓存是加速手段，不是查询的新单点。

    def query(self, code: str) -> Outcome:
        self.last_outcome = None
        self._guard(code)
        adapter = self.registry.match(code)
        if adapter is None:
            raise NoCurrencySource(f"无可用原生时效数据源：{code}")
        namespace = self.registry.namespace_for(code, adapter)
        entry = self._read(code, namespace)
        if self._usable(entry, adapter, code):
            self._guard(code)
            return self._finish(Outcome(entry.results, adapter.name, True, entry.age(self.clock())))
        try:
            rows = adapter.lookup(code)
        except (SourceUnreachable, NeedsHuman) as exc:
            # 40、取消与编程缺陷不在可恢复白名单，绝不能让旧缓存遮蔽它们。
            self._guard(code)
            entry = self._read(code, namespace, any_age=True)
            if self._usable(entry, adapter, code) and entry.age(self.clock()) <= DEFAULT_STALE_LIMIT:
                self._guard(code)
                return self._finish(Outcome(entry.results, adapter.name, True, entry.age(self.clock()), True, exc))
            raise
        self._guard(code)
        if self.cache is not None:
            with suppress(OSError):
                self.cache.put(code, rows, namespace=namespace)
        self._guard(code)
        return self._finish(Outcome(tuple(rows), adapter.name))

    def _finish(self, outcome: Outcome) -> Outcome:
        self.last_outcome = outcome
        return outcome

    def lookup(self, code: str) -> list[Record]:
        return list(self.query(code).results)

    @property
    def from_cache(self) -> bool:
        return self.last_outcome is not None and self.last_outcome.from_cache

    @property
    def cache_age_days(self) -> float | None:
        if self.last_outcome is None or not self.last_outcome.from_cache:
            return None
        return self.last_outcome.cache_age / 86400
