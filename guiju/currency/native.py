"""原生时效编排：路由/准入 → TTL 缓存 → 上游 → 明示的有界陈旧缓存。

不装配具体存储，不自行落盘；DiskCurrencyCache 由调用者从 store 层显式注入。
本模块就是 CLI 默认实现（GUIJU_CURRENCY=native 或不设）；cmd/mcp 覆盖已随 spec-currency 退役删除。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, fields

from ..errors import GuijuError, NeedsHuman, NoCurrencyRecord, SourceUnreachable
from .cache import DEFAULT_STALE_LIMIT, CacheEntry, CurrencyCache
from .codes import same_family
from .model import Currency, QueryCancelled, Record
from .registry import Registry, default_registry


class NoCurrencySource(NoCurrencyRecord):
    """旧名保留向后兼容；语义归并为 34 no_currency_record。"""


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
    # W3/P1-4：结果是否完整可信。False = 没取全（无总页数证据/截断/明文回退），
    # 不会被写成缓存，调用方必须把它当作"可能缺少记录"而不是权威空结论。
    complete: bool = True
    incomplete_reason: str = ""
    #: 源明确回答"没有这条记录"（pageNum=0 的 csres 空结果）。可与 complete=False
    #: 并存：明文回退的空结果不可缓存，但仍是权威空而不是"需人工排查"。
    empty_authoritative: bool = False
    #: 本次结果来自明文回退；写缓存必须同时满足 complete 与 not insecure。
    insecure_transport: bool = False
    #: 翻页是否被上限截断；has_more 是完整性证据（False 才表示末页）。
    #: 这两个标志用于区分“明文但完整”与“明文且不完整”，_pick_current_version
    #: 只允许前者参与同名定版。
    truncated: bool = False
    has_more: bool | None = None

    @property
    def fallback_source(self) -> str:
        return next((row.fallback.strip() for row in self.results if row.fallback.strip()), "")

    def critical_notes(self) -> list[str]:
        notes = []
        if not self.complete:
            reason = f"：{self.incomplete_reason}" if self.incomplete_reason else ""
            notes.append(f"⚠ 结果不完整{reason}；未写入缓存，重要结论建议复核")
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
        if not self.complete:
            parts.append("[! 不完整: 可能缺少记录，未写缓存]")
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
        # 进程级备忘只在"没有缓存后端"时启用：有缓存时 TTL 命中本身就是 M11 要的
        # 去重（零 HTTP、还带回 from_cache/age），memo 放在它前面会绕过 TTL 边界、
        # 陈旧上限与负缓存替换。无缓存时第二次调用才会重新打上游，此时 memo 窗口取 60s；
        # 从不记忆失败，取消/撤销的策略也不会拿到 memo 答案。
        self._memo: dict[str, tuple[float, Outcome]] = {}

    def _guard(self, code: str) -> None:
        """仅取消检查；admit 不再随每次守卫重复调用（计数型 admit 会被重复计费）。"""
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
        except (GuijuError, QueryCancelled):
            raise  # 取消/合规等控制流异常必须原样冒泡，绝不能被缓存层吞掉
        except Exception:
            return None  # 缓存是加速手段，不是查询的新单点（OSError/ValueError/sqlite3.Error 等后端故障同理）

    def _memo_ttl(self) -> float:
        ttl = getattr(self.cache, "ttl", None)
        if isinstance(ttl, (int, float)) and math.isfinite(ttl) and ttl > 0:
            return float(ttl)
        return 60.0  # 无缓存后端时收敛到一个短窗口，memo 不得让结果"永不过期"

    def query(self, code: str) -> Outcome:
        self.last_outcome = None
        self._guard(code)
        if self.admit is not None:
            self.admit(code)  # 准入计数只在这里与 put 前各记一次，不随每个 guard 重复计费
        self._guard(code)
        if self.cache is None:
            # 无缓存后端时同一编号的第二次调用会重新打上游，memo 顶掉它；
            # 策略已在上方 admit，被取消则 _guard 拦住，不会返回旧答案。
            memoed = self._memo.get(code)
            if memoed is not None:
                stamped, outcome = memoed
                if self.clock() - stamped <= self._memo_ttl():
                    self._guard(code)
                    return self._finish(outcome)
                self._memo.pop(code, None)
        adapter = self.registry.match(code)
        if adapter is None:
            raise NoCurrencySource(f"时效侧没有该编号的记录：原生时效路由未覆盖此前缀，而非本地目录未收录（31）：{code}")
        namespace = self.registry.namespace_for(code, adapter)
        entry = self._read(code, namespace)
        if self._usable(entry, adapter, code):
            self._guard(code)
            return self._succeed(code, Outcome(entry.results, adapter.name, True, entry.age(self.clock())))
        try:
            rows = adapter.lookup(code)
        except (SourceUnreachable, NeedsHuman) as exc:
            # 40、取消与编程缺陷不在可恢复白名单，绝不能让旧缓存遮蔽它们。
            self._guard(code)
            entry = self._read(code, namespace, any_age=True)
            if self._usable(entry, adapter, code) and entry.age(self.clock()) <= DEFAULT_STALE_LIMIT:
                self._guard(code)
                return self._succeed(code, Outcome(entry.results, adapter.name, True, entry.age(self.clock()), True, exc))
            raise
        self._guard(code)
        # 只有"完整且可信"的结果才能当权威结论写缓存：不完整的空结果一旦落盘，
        # 就是 24 小时假阴性（W3/P1-4）；没有 complete 字段的老适配器沿用旧契约。
        complete = bool(getattr(adapter, "complete", True))
        insecure_transport = bool(getattr(adapter, "insecure_transport", False))
        cacheable = complete and not insecure_transport
        empty_authoritative = bool(getattr(adapter, "empty_authoritative", False))
        truncated = bool(getattr(adapter, "truncated", False))
        has_more = getattr(adapter, "has_more", None)
        incomplete_reason = "" if complete else str(getattr(adapter, "incomplete_reason", "") or "")
        if self.admit is not None:
            self.admit(code)  # put 前复验：查到的数据还没落盘，此刻拒绝就不能写缓存
        if self.cache is not None and cacheable:
            try:
                self.cache.put(code, rows, namespace=namespace)
            except (GuijuError, QueryCancelled):
                raise
            except Exception:
                pass  # 写不进缓存不影响把刚查到的结果给调用方
        self._guard(code)
        return self._succeed(code, Outcome(
            tuple(rows), adapter.name, complete=complete, incomplete_reason=incomplete_reason,
            empty_authoritative=empty_authoritative, insecure_transport=insecure_transport,
            truncated=truncated, has_more=has_more,
        ))

    def _succeed(self, code: str, outcome: Outcome) -> Outcome:
        # 只有完整走到底的成果才进 memo（且仅无缓存路径会用）；失败/取消/拒绝都不会被记忆。
        if self.cache is None:
            self._memo[code] = (self.clock(), outcome)
        return self._finish(outcome)

    def _finish(self, outcome: Outcome) -> Outcome:
        self.last_outcome = outcome
        return outcome

    def lookup(self, code: str) -> list[Record]:
        return list(self.query(code).results)

    @property
    def from_cache(self) -> bool:
        return self.last_outcome is not None and self.last_outcome.from_cache

    @property
    def stale(self) -> bool:
        """上游刚失败、靠有界陈旧缓存回答 → True；which --json 的告警依据，不是 from_cache。"""
        return self.last_outcome is not None and self.last_outcome.stale

    @property
    def complete(self) -> bool:
        """最近一次成功结果是否完整；False = 没取全、未写缓存，结论需复核。"""
        return self.last_outcome is not None and self.last_outcome.complete

    @property
    def empty_authoritative(self) -> bool:
        """最近一次空结果是否是源明确回答"没有"（而不是没读懂/没取全）。"""
        return self.last_outcome is not None and self.last_outcome.empty_authoritative

    @property
    def insecure_transport(self) -> bool:
        """最近一次结果是否走了明文回退（写缓存前必须同时满足 complete）。"""
        return self.last_outcome is not None and self.last_outcome.insecure_transport

    @property
    def truncated(self) -> bool:
        return self.last_outcome is not None and self.last_outcome.truncated

    @property
    def has_more(self) -> bool | None:
        return self.last_outcome.has_more if self.last_outcome is not None else None

    @property
    def upstream_error(self) -> Exception | None:
        return (self.last_outcome is not None and self.last_outcome.upstream_error) or None

    @property
    def cache_age_days(self) -> float | None:
        if self.last_outcome is None or not self.last_outcome.from_cache:
            return None
        return self.last_outcome.cache_age / 86400
