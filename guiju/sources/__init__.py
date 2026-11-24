"""源适配器注册表。

每个源彼此隔离：坏了不影响别人，改版只改自己那一个文件。
协议故意做窄（目录 / 详情 / 附件 / 下载四件），因为多出来的能力迟早会变成
跨源的隐式耦合。
"""

from __future__ import annotations

from ..ports.source import CatalogItem as CatalogItem
from ..ports.source import SourcePort as Source

__all__ = ["CatalogItem", "Source", "registry"]


def registry() -> dict[str, Source]:
    from .mohurd import SOURCE as mohurd
    from .shzjw import SOURCE as shzjw

    return {"mohurd": mohurd, "shzjw": shzjw}
