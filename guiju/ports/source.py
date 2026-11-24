"""源端口：目录 / 详情 / 附件 / 下载四件事，保持与既有 sources 协议同形。"""

from __future__ import annotations

from typing import Protocol

from ..domain.attachment import Attachment


class CatalogItem(Protocol):
    date: str
    url: str
    title: str


class SourcePort(Protocol):
    id: str
    display_name: str

    def catalog_page(self, page_no: int, page_size: int) -> tuple[list[dict], int | None]: ...

    def find(self, ref: str, *, prefer_exact: bool = True) -> list[dict]: ...

    def detail(self, url: str) -> tuple[str, str]: ...

    def attachments(self, html: str) -> list[Attachment]: ...

    def download(self, file_url: str, referer: str) -> tuple[bytes, str]: ...

    def download_to(self, file_url: str, part_path: str, referer: str = "", probe_sink=None) -> tuple[int, str, int]: ...
