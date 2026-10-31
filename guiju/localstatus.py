"""地方标准的时效兜底 —— 上海源本身就分「现行 / 废止」，不必依赖外部时效服务。

为什么需要这一层：`which` / `cite` 的现行性判断原本只问时效服务（spec-currency）。
对 `DG/TJ`、`DBJT` 这类地方标准，上海官方查询站自己就有现行/废止分栏 ——
所以当外部时效服务不可达或没有这条记录时，直接读源站的结论，
比自己按年份猜要可靠得多。
"""

from __future__ import annotations

from .currency import Record
from .sources import shzjw

#: 地方标准编号前缀（上海口径：DG/TJ 标准、DBJT 设计标准图集、DB31/T 地方标准）
PREFIXES = ("DG/TJ", "DBJT", "DGJ", "DB31/T", "DBJ")


def is_local_code(ref: str) -> bool:
    text = (ref or "").strip().upper().replace(" ", "")
    return text.startswith(PREFIXES)


def records(ref: str) -> list[Record]:
    """用上海源回答"这个地标现在是什么状态"。网络失败会抛 GuijuError 子类。"""
    return [
        Record(
            code=hit["code"],
            status=hit["status"],          # current / withdrawn
            name=hit["name"],
            implement_date=hit.get("implement_on") or "",
            source="shzjw",
        )
        for hit in shzjw.find(ref)
    ]
