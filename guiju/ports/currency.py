"""时效端口：application 只依赖这里的协议与领域值对象。"""

from __future__ import annotations

from typing import Protocol

from ..domain.currency import QueryCancelled, Record


class CurrencyPort(Protocol):
    name: str

    def lookup(self, code: str) -> list[Record]: ...


__all__ = ["CurrencyPort", "QueryCancelled", "Record"]
