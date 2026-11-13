"""时效端口与统一元数据模型，不含网络或存储。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Record:
    code: str
    status: str          # current / superseded / upcoming / withdrawn / unknown
    name: str = ""
    implement_date: str = ""
    source: str = ""
    replaces: str = ""
    # 原生解析保留 Go golden 的证据；不改变既有 CLI / TSV 五字段投影。
    publish_date: str = ""
    raw: str = ""
    fallback: str = ""  # 备源名；缓存重放也不能丢掉这条来源告警

    @property
    def is_current(self) -> bool:
        return self.status == "current"

    def as_payload(self) -> dict:
        # CLI/TSV 五字段投影（which/cite/lookup 的 currency[]），契约不变。
        return {
            "code": self.code,
            "status": self.status,
            "name": self.name,
            "implement_date": self.implement_date,
            "source": self.source,
        }

    def cache_payload(self) -> dict:
        # 缓存落库投影（M2）：在 CLI 投影之上补 replaces/fallback。
        # 备源应答重放时不许伪装成主源——fallback 告警必须随记录存活。
        return {
            "code": self.code,
            "status": self.status,
            "name": self.name,
            "implement_date": self.implement_date,
            "source": self.source,
            "replaces": self.replaces,
            "fallback": self.fallback,
        }


class QueryCancelled(Exception):
    """调用方取消：不是上游故障，不能被备源或陈旧缓存恢复。"""


class Currency(Protocol):
    name: str

    def lookup(self, code: str) -> list[Record]: ...
