"""地方标准的时效兜底 —— 上海源本身就分「现行 / 废止」，不必依赖外部时效服务。

为什么需要这一层：`which` / `cite` 的现行性判断原本只问时效服务（native 路由，旧 spec-currency 已退役）。
对 `DG/TJ`、`DBJT` 这类地方标准，上海官方查询站自己就有现行/废止分栏 ——
所以当外部时效服务不可达或没有这条记录时，直接读源站的结论，
比自己按年份猜要可靠得多。
"""

from __future__ import annotations

from .currency import Record
from .currency.shzjw import explicit_version, is_shanghai_code
from .sources import shzjw

#: 地方标准编号口径只认 currency.shzjw.is_shanghai_code（单一真相）：
#: 裸 DGJ/DBJ 不是上海专属（DBJ50 重庆、DGJ11 江苏），DB31/373 这类非 /T
#: 形式也不归上海路由。


def is_local_code(ref: str) -> bool:
    return is_shanghai_code(ref)


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
        # 基础号保留全部年版（current 优先）；显式版年仍精确匹配。
        for hit in shzjw.find(ref, prefer_exact=explicit_version(ref))
    ]
