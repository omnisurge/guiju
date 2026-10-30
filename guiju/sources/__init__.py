"""源适配器注册表。

每个源彼此隔离：坏了不影响别人，改版只改自己那一个文件。
协议故意做窄（目录 / 详情 / 附件 / 下载四件），因为多出来的能力迟早会变成
跨源的隐式耦合。
"""

from __future__ import annotations

from typing import Protocol

from ..domain.attachment import Attachment


class CatalogItem(Protocol):
    date: str
    url: str
    title: str


class Source(Protocol):
    """一个数据源必须能回答的四个问题。"""

    id: str
    display_name: str

    def catalog_page(self, page_no: int, page_size: int) -> tuple[list[dict], int]:
        """返回 (条目列表, 总数)。条目字段：date / url / title。"""

    def detail(self, url: str) -> tuple[str, str]:
        """返回 (原始 HTML, 纯文本)。"""

    def attachments(self, html: str) -> list[Attachment]:
        """列出公告页上的附件。"""

    def download(self, file_url: str, referer: str) -> tuple[bytes, str]:
        """取正文二进制，返回 (内容, 魔数标识)。"""


def registry() -> dict[str, Source]:
    from .mohurd import SOURCE as mohurd

    return {"mohurd": mohurd}
