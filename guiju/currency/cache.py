"""原生时效缓存端口与值对象；具体磁盘持久化只在 guiju.store 层。"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Protocol

from .model import Record

DEFAULT_TTL = 24 * 60 * 60
DEFAULT_STALE_LIMIT = 30 * 24 * 60 * 60
CACHE_NAMESPACE = "guiju-native-m2-v1"


@dataclass(frozen=True)
class CacheEntry:
    queried_at: float
    results: tuple[Record, ...]

    def age(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.queried_at

    def fresh(self, ttl: float, now: float | None = None) -> bool:
        age = self.age(now)
        return math.isfinite(self.queried_at) and self.queried_at > 0 and math.isfinite(ttl) and ttl > 0 and 0 <= age < ttl


class CurrencyCache(Protocol):
    def get(self, code: str, *, namespace: str = CACHE_NAMESPACE) -> CacheEntry | None: ...

    def get_any(self, code: str, *, namespace: str = CACHE_NAMESPACE) -> CacheEntry | None: ...

    def put(self, code: str, records: list[Record], *, namespace: str = CACHE_NAMESPACE) -> None: ...
