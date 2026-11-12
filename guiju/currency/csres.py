"""csres（工标网）元数据：GBK HTML → 统一 Record，保留 Go golden 口径。

只有显式构造 CsresCurrency 才使用此源；M1 不接入默认路由。
不读取链接、不取正文、不落文件、不推断替代关系或现行性。
"""

from __future__ import annotations

import urllib.parse
import warnings
from dataclasses import dataclass, field
from html.parser import HTMLParser

from ..errors import NeedsHuman
from .codes import filter_by_code
from .httpx import HttpClient
from .model import Record

SEARCH_URL = "http://www.csres.com/s.jsp"
_STATUS = {"现行": "current", "作废": "superseded", "即将实施": "upcoming", "废止": "withdrawn"}
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


def extract_title_field(title: str, field_name: str) -> str:
    start = title.find(field_name)
    if start < 0:
        return ""
    rest = title[start + len(field_name):]
    # 属性实体已解码为真换行；兼容旧实现也认字面量 &#xA;，取最早分隔符。
    cut = min((rest.find(sep) for sep in ("\n", "\r", "&#xA;") if sep in rest), default=len(rest))
    return rest[:cut].strip()


@dataclass
class _Row:
    depth: int
    title: str
    cells: list[list[str]] = field(default_factory=list)
    cell_depth: int | None = None

    def record(self) -> Record | None:
        if not self.title.startswith("编号：") or len(self.cells) < 4:
            return None
        cells = ["".join(parts).strip() for parts in self.cells]
        return Record(
            code=cells[0], name=cells[1], implement_date=cells[3],
            status=_STATUS.get(cells[4], "unknown") if len(cells) >= 5 else "unknown",
            publish_date=extract_title_field(self.title, "发布日期："),
            source="csres", raw=self.title,
        )


class _TableParser(HTMLParser):
    """收集 tr 的直接 td，并串接子元素文本；不把外层布局表算成数据行。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.rows: list[_Row] = []
        self.active: list[_Row] = []

    def _last(self, tags: tuple[str, ...]) -> int:
        return next((i for i in range(len(self.tags) - 1, -1, -1) if self.tags[i] in tags), -1)

    def _close(self, index: int) -> None:
        for row in self.active:
            if row.cell_depth is not None and row.cell_depth >= index:
                row.cell_depth = None
        self.active = [row for row in self.active if row.depth < index]
        del self.tags[index:]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # HTML 允许省略 </td> / </tr>；模拟表格最小隐式闭合，保留 Go HTML parser 的容错。
        if tag == "tr":
            last_row = self._last(("tr",))
            if last_row > self._last(("table",)):
                self._close(last_row)
        elif tag in ("td", "th"):
            last_cell = self._last(("td", "th"))
            if last_cell > self._last(("tr",)):
                self._close(last_cell)
        depth = len(self.tags)
        if tag == "tr":
            row = _Row(depth=depth, title=dict(attrs).get("title") or "")
            self.rows.append(row)
            self.active.append(row)
        elif tag == "td" and self.active and depth == self.active[-1].depth + 1:
            row = self.active[-1]
            row.cells.append([])
            row.cell_depth = depth
        if tag not in _VOID:
            self.tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        index = self._last((tag,))
        if index >= 0:
            self._close(index)

    def handle_data(self, data: str) -> None:
        for row in self.active:
            if row.cell_depth is not None:
                row.cells[-1].append(data)


def parse_csres(raw: bytes) -> list[Record]:
    """原始 GBK 字节的纯解析；发布日期取首个 title 字段，实施日期取第 4 列。"""
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError as exc:
        raise NeedsHuman("csres GBK 解码失败", hint="检查 csres 响应编码；不要把乱码当成无命中") from exc
    parser = _TableParser()
    parser.feed(text.replace("\r\n", "\n").replace("\r", "\n"))
    parser.close()
    records = [record for row in parser.rows if (record := row.record()) is not None]
    if not records and (
        any(row.title.startswith("编号：") for row in parser.rows)
        or any(len(row.cells) >= 4 for row in parser.rows)
    ):
        # 数据行痕迹在（编号：前缀或 >=4 单元格）却零记录：改版/解析失败，不许静默成无命中。
        warnings.warn(
            f"csres 页面含 {len(parser.rows)} 个 tr 但 0 条可解析记录：可能是站点改版",
            RuntimeWarning, stacklevel=2,
        )
    return records


class CsresCurrency:
    name = "csres"
    url = SEARCH_URL

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        # HTML 列表页无总数字段：has_more=None 表示"无法判定"，不猜 False。
        self.pagination: dict = {"fetched_pages": 0, "total": None, "has_more": None}

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "CsresCurrency":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lookup(self, code: str) -> list[Record]:
        query = urllib.parse.urlencode({"keyword": code, "pageNum": "1"})
        raw = self.client.get(f"{SEARCH_URL}?{query}")
        records = filter_by_code(parse_csres(raw), code)
        self.pagination = {"fetched_pages": 1, "total": None, "has_more": None}
        return records
