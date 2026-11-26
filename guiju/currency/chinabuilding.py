"""国标电子书库唯一官方图集时效适配器（方案 A：列表检索 + 精确废止追查替代链）。

严守合规与领域隔离：
- 仅通过 MetadataClient 访问已批准的只读检索/详情 HTML 端点，绝不触碰正文下载平面；
- 复用 guiju/domain/atlas.py 的严格编号匹配与页面解析，不复制逻辑；
- 绝不回 fallback 第三方外源（与 shzjw 同级单源权威策略）。
"""

from __future__ import annotations

import re
from collections.abc import Callable

from ..domain import atlas
from ..errors import NeedsHuman
from ..net.metadata import MetadataClient
from .contract import Completeness, CompletenessMixin
from .model import QueryCancelled, Record

_LANG_SUFFIX_RE = re.compile(r"(?:英文版|中文版)$")


def _decode_utf8(raw: bytes, *, where: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NeedsHuman(
            f"国标电子书库{where}编码异常，无法按 UTF-8 解码",
            hint="请检查 ebook.chinabuilding.com.cn 响应编码",
        ) from exc


def _search_key(code: str) -> str:
    norm = atlas.normalize_code(code)
    return _LANG_SUFFIX_RE.sub("", norm).strip() or code.strip()


def _format_raw(book_id: str, abolish_date: str = "") -> str:
    base = f"bookID={book_id}"
    return f"{base}; abolish_date={abolish_date}" if abolish_date else base


class ChinaBuildingCurrency(CompletenessMixin):
    name = "chinabuilding"
    url = atlas.SEARCH_URL
    source_names = ("chinabuilding",)
    cache_namespace = "chinabuilding:official-single-v1:scheme-a"
    matches = staticmethod(atlas.matches)
    MAX_PAGES = 10
    MAX_DETAIL_CHASE = 2

    def __init__(self, client: MetadataClient | None = None, *, cancelled: Callable[[], bool] | None = None) -> None:
        self.client = client if client is not None else MetadataClient()
        self.cancelled = cancelled

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> ChinaBuildingCurrency:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _check(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise QueryCancelled("图集查询已取消，不继续请求国标电子书库")

    def _chase_detail(self, code: str, item: atlas.AtlasSearchItem) -> Record:
        self._check()
        raw = self.client.get(atlas.detail_url(item.book_id))
        self._check()
        detail = atlas.parse_detail_page(_decode_utf8(raw, where="详情页"), book_id=item.book_id)
        if detail.code and not atlas.matches(code, detail.code):
            raise NeedsHuman(
                f"国标电子书库详情页编号不匹配：期望 {code}，实得 {detail.code}",
                hint="请检查 bookID 与图集详情页映射",
            )
        return Record(
            code=detail.code or item.code,
            status=detail.status,
            name=detail.name or item.name,
            implement_date=detail.implement_date or item.implement_date,
            source=self.name,
            replaces=detail.replaces,
            publish_date=detail.publish_date or item.publish_date,
            raw=_format_raw(item.book_id, detail.abolish_date or item.abolish_date),
            replaced_by=detail.replaced_by,
        )

    def lookup(self, code: str) -> list[Record]:
        self._completeness = Completeness()
        self._check()
        if not atlas.is_atlas_code(code):
            self._record_completeness(
                Completeness(fetched_pages=0, total=0, has_more=False, empty_authoritative=True)
            )
            return []

        explicit = atlas.has_edition_year(code)
        query_key = _search_key(code)

        matched: list[atlas.AtlasSearchItem] = []
        seen_ids: set[str] = set()
        fetched_pages = 0
        total: int | None = None
        has_more: bool | None = None
        truncated = False

        for page_num in range(1, self.MAX_PAGES + 1):
            self._check()
            raw = self.client.get(atlas.search_url(query_key, page=page_num))
            self._check()
            parsed = atlas.parse_search_page(_decode_utf8(raw, where="搜索页"), page=page_num)
            for item in parsed.items:
                if item.book_id not in seen_ids and atlas.matches(code, item.code):
                    seen_ids.add(item.book_id)
                    matched.append(item)
            fetched_pages = page_num
            total = parsed.total
            has_more = parsed.has_more
            if not parsed.has_more:
                break
            if page_num >= self.MAX_PAGES:
                truncated = True
                break

        records: list[Record] = []
        detail_fetches = 0
        for item in matched:
            if explicit and item.status == "withdrawn" and detail_fetches < self.MAX_DETAIL_CHASE:
                detail_fetches += 1
                records.append(self._chase_detail(code, item))
            else:
                records.append(
                    Record(
                        code=item.code,
                        status=item.status,
                        name=item.name,
                        implement_date=item.implement_date,
                        source=self.name,
                        publish_date=item.publish_date,
                        raw=_format_raw(item.book_id, item.abolish_date),
                    )
                )

        self._record_completeness(
            Completeness(
                fetched_pages=fetched_pages,
                total=total,
                has_more=has_more,
                truncated=truncated,
                reason=(
                    f"国标电子书库分页超过上限（已取 {fetched_pages} 页，共 {total} 条）"
                    if truncated
                    else ""
                ),
                empty_authoritative=(not records and not truncated and has_more is False),
            )
        )
        return records

    @staticmethod
    def accepts_record(code: str, row: Record) -> bool:
        return (
            row.source == "chinabuilding"
            and not row.fallback
            and row.status in {"current", "superseded", "upcoming", "withdrawn"}
            and atlas.matches(code, row.code)
        )
