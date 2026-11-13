"""地方标准的时效兜底 —— 上海源本身就分「现行 / 废止」，不必依赖外部时效服务。

为什么需要这一层：`which` / `cite` 的现行性判断原本只问时效服务（native 路由，旧 spec-currency 已退役）。
对 `DG/TJ`、`DBJT` 这类地方标准，上海官方查询站自己就有现行/废止分栏 ——
所以当外部时效服务不可达或没有这条记录时，直接读源站的结论，
比自己按年份猜要可靠得多。
"""

from __future__ import annotations

from .currency import Record
from .sources import shzjw

#: 地方标准编号前缀（上海口径）。
#: 注意裸 DGJ/DBJ 不是上海专属：DBJ50 是重庆、DGJ11 是江苏，不能宽匹配。
#: 上海 DGJ/DBJ 段编号以 08 打头（DGJ08 / DBJ08）；DB 系只认上海 DB31。
PREFIXES = ("DG/TJ", "DBJT", "DGJ08", "DB31", "DBJ08")


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
