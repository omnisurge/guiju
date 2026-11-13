"""jtst 交通运输标准元数据；仅解析搜索 HTML，不读取详情或正文。

保留 Go 的「每个含 en-code 的 tr」口径，包括外层布局行的重复记录。
"""

from __future__ import annotations

import urllib.parse
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser

from ..errors import NeedsHuman
from .codes import query_with_base_fallback
from .httpx import HttpClient
from .model import Record

SEARCH_URL = "https://jtst.mot.gov.cn/search/stdPage"
_STATUS = {"现行": "current", "即将实施": "upcoming", "废止": "withdrawn"}
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    parent: _Node | None = None
    children: list[_Node | str] = field(default_factory=list)


class _TreeParser(HTMLParser):
    """局部极小树，保留父链、文档顺序与嵌套 tr；不做全页选择器猜测。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("")
        self.stack = [self.root]

    def _last(self, tags: tuple[str, ...]) -> int:
        return next((i for i in range(len(self.stack) - 1, 0, -1) if self.stack[i].tag in tags), -1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 表格允许省略闭合；只闭合同一 table 的行/同一 tr 的单元格。
        if tag == "tr":
            last_row = self._last(("tr",))
            if last_row > self._last(("table",)):
                del self.stack[last_row:]
        elif tag in ("td", "th"):
            last_cell = self._last(("td", "th"))
            if last_cell > self._last(("tr",)):
                del self.stack[last_cell:]
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        index = self._last((tag,))
        if index >= 0:
            del self.stack[index:]

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _walk(node: _Node) -> Iterator[_Node | str]:
    stack: list[_Node | str] = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, _Node):
            stack.extend(reversed(current.children))


def _text(node: _Node) -> str:
    return "".join(part for part in _walk(node) if isinstance(part, str)).strip()


def _find(node: _Node, predicate: Callable[[_Node], bool]) -> _Node | None:
    return next((part for part in _walk(node) if isinstance(part, _Node) and predicate(part)), None)


def _span(node: _Node, class_name: str) -> bool:
    return node.tag == "span" and class_name in (node.attrs.get("class") or "").split()


def _row(tr: _Node) -> Record | None:
    code_span = _find(tr, lambda node: _span(node, "en-code"))
    if code_span is None or not (code := _text(code_span)):
        return None
    name = ""
    parent = code_span.parent
    while parent is not None:
        if parent.tag == "a":
            anchor_text = _text(parent)
            # Go strings.TrimPrefix：只去掉开头的一次，不全局替换编号。
            name = (anchor_text[len(code):] if anchor_text.startswith(code) else anchor_text).strip()
            break
        parent = parent.parent
    status_span = _find(tr, lambda node: _span(node, "s-status") and bool(_text(node)))
    return Record(
        code=code, name=name, source="jtst",
        status=_STATUS.get(_text(status_span), "unknown") if status_span is not None else "unknown",
    )


def parse_jtst(raw: bytes) -> list[Record]:
    """UTF-8 原始字节的纯解析；只认源站状态，不推断历史旧号。

    页面含表格行但一条都解析不出来时发 RuntimeWarning：空结果页合法、
    解析失败不许静默成"无命中"。
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NeedsHuman("jtst UTF-8 解码失败", hint="检查 jtst 响应编码；不要把乱码当成无命中") from exc
    parser = _TreeParser()
    parser.feed(text.replace("\r\n", "\n").replace("\r", "\n"))
    parser.close()
    rows = [node for node in _walk(parser.root)
            if isinstance(node, _Node) and node.tag == "tr"]
    records = [record for node in rows if (record := _row(node)) is not None]
    if rows and not records:
        warnings.warn(
            f"jtst 页面含 {len(rows)} 个 tr 但 0 条可解析记录：可能是站点改版或布局行误判",
            RuntimeWarning, stacklevel=2,
        )
    return records


class JtstCurrency:
    name = "jtst"
    url = SEARCH_URL

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        # HTML 列表页无总数字段：has_more=None 表示"无法判定"，不猜 False。
        self.pagination: dict = {"fetched_pages": 0, "total": None, "has_more": None}

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> JtstCurrency:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lookup(self, code: str) -> list[Record]:
        if not code.strip():
            return []

        def fetch(term: str) -> list[Record]:
            query = urllib.parse.urlencode({"tid": "", "op": "", "q": term})
            records = parse_jtst(self.client.get(f"{SEARCH_URL}?{query}", accept="text/html"))
            self.pagination = {"fetched_pages": 1, "total": None, "has_more": None}
            return records

        return query_with_base_fallback(code, fetch)
