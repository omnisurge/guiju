"""时效端口：application 只依赖这里的协议与领域值对象。"""

from __future__ import annotations

from typing import Any, Protocol

from ..domain.currency import QueryCancelled, Record


class CurrencyPort(Protocol):
    name: str

    def lookup(self, code: str) -> list[Record]: ...


class McpCurrencyPort(Protocol):
    """MCP 服务面要求的并发安全时效端口：必须提供 query(code) -> Outcome。"""

    name: str

    def query(self, code: str) -> Any: ...

    def lookup(self, code: str) -> list[Record]: ...


__all__ = ["CurrencyPort", "McpCurrencyPort", "QueryCancelled", "Record"]
