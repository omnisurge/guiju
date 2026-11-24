"""时效模型兼容入口：值对象已迁到 guiju.domain.currency。"""

from __future__ import annotations

from typing import Protocol

from ..domain.currency import QueryCancelled, Record

__all__ = ["Currency", "QueryCancelled", "Record"]


class Currency(Protocol):
    name: str

    def lookup(self, code: str) -> list[Record]: ...
