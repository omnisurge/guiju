"""csres（工标网）元数据：GBK HTML → 统一 Record，保留 Go golden 口径。

只有显式构造 CsresCurrency 才使用此源；M1 不接入默认路由。
不读取链接、不取正文、不落文件、不推断替代关系或现行性。

传输（W3/P1-4）：默认只走 https。站点若不支持 HTTPS，需要显式构造
CsresCurrency(..., plaintext_fallback=True) 才会回退明文 http —— 回退会留下
RuntimeWarning 与 plaintext_risk，并把结果标记为"不可作为权威答案"（不写缓存）。
明文通道仅用于这份只读元数据，绝不用于正文下载。
"""

from __future__ import annotations

import re
import urllib.parse
import warnings
from dataclasses import dataclass, field
from html.parser import HTMLParser

from ..errors import NeedsHuman, SourceUnreachable
from .codes import filter_by_code
from .contract import Completeness, CompletenessMixin, page_error
from .httpx import HttpClient
from .model import Record

SEARCH_URL = "https://www.csres.com/s.jsp"
PLAINTEXT_SEARCH_URL = "http://www.csres.com/s.jsp"
PLAINTEXT_RISK = "csres 查询显式回退明文 HTTP：结果未经传输保护，仅用于元数据，绝不用作正文来源"
_STATUS = {"现行": "current", "作废": "superseded", "即将实施": "upcoming", "废止": "withdrawn"}
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})

# 列表页把"共有 N 条…共 M 页"和 25 行/页写在页面里（2026-11-18 fixture 实测）。
_TOTAL = re.compile(r"共有\s*(\d+)\s*条")
_FOUND = re.compile(r"共找到\s*(\d+)\s*条")
_PAGES = re.compile(r"共\s*(\d+)\s*页")
_PAGE_SIZE = re.compile(r'name\s*=\s*["\']?pageSize["\']?[^>]*?value\s*=\s*["\']?(\d+)', re.IGNORECASE)
_PAGE_NO = re.compile(r'name\s*=\s*["\']?pageNum["\']?[^>]*?value\s*=\s*["\']?(\d+)', re.IGNORECASE)


def extract_title_field(title: str, field_name: str) -> str:
    start = title.find(field_name)
    if start < 0:
        return ""
    rest = title[start + len(field_name):]
    # 属性实体已解码为真换行；兼容旧实现也认字面量 &#xA;，取最早分隔符。
    cut = min((rest.find(sep) for sep in ("\n", "\r", "&#xA;") if sep in rest), default=len(rest))
    return rest[:cut].strip()


def _int_or_none(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return int(match[1]) if match else None


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
        if not cells[0]:
            # 空编号行不是记录；parse_csres_page 会把它判成坏行/结构异常。
            return None
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


@dataclass(frozen=True)
class CsresPage:
    """一页列表页的解析结果 + 页面自己声明的分页证据（缺失即 None，不猜）。"""

    records: list[Record]
    total: int | None = None
    pages: int | None = None
    page: int | None = None
    page_size: int | None = None
    #: 原始 pageNum 明确为 0 且无记录 = 站点"无结果页"标记；page 字段会把它
    #: 投影成 None，这里保留区分（missing pageNum 不是同一个事实）。
    page_zero: bool = False


def _has_data_traces(rows: list[_Row]) -> bool:
    return any(row.title.startswith("编号：") or len(row.cells) >= 4 for row in rows)


def parse_csres_page(raw: bytes) -> CsresPage:
    """原始 GBK 字节 → 记录 + 页数证据；有数据痕迹但 0 条记录 = 改版，抛 50。"""
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError as exc:
        raise NeedsHuman("csres GBK 解码失败", hint="检查 csres 响应编码；不要把乱码当成无命中") from exc
    parser = _TableParser()
    parser.feed(text.replace("\r\n", "\n").replace("\r", "\n"))
    parser.close()
    records = []
    bad_rows: list[_Row] = []
    for row in parser.rows:
        record = row.record()
        if record is not None:
            records.append(record)
        elif row.title.startswith("编号：") and len(row.cells) >= 4:
            # 有编号行但首列编号为空：这是坏行，不能静默丢掉后报"完整"。
            bad_rows.append(row)
    if bad_rows:
        raise page_error("csres", f"有 {len(bad_rows)} 个编号行为空/不可解析")
    if not records and _has_data_traces(parser.rows):
        # 数据行痕迹在（编号：前缀或 >=4 单元格）却零记录：改版/解析失败，不许静默成无命中。
        raise page_error("csres", f"页面含 {len(parser.rows)} 个 tr 但 0 条可解析记录（有数据痕迹）")
    total = _int_or_none(_TOTAL, text)
    if total is None:
        total = _int_or_none(_FOUND, text)
    page = _int_or_none(_PAGE_NO, text)
    # csres 对“0 条命中”返回 pageNum=0 且 pageSize=25；0 不是第 0 页，
    # 而是站点表示“没有结果页”。把它投影成 None，不能让 lookup 误判为
    # “请求第 1 页、服务端返回第 0 页”的结构异常（真实未知编号会因此 50）。
    page_zero = page == 0 and not records
    if page_zero:
        page = None
    return CsresPage(
        records=records,
        total=total,
        pages=_int_or_none(_PAGES, text),
        page=page,
        page_size=_int_or_none(_PAGE_SIZE, text),
        page_zero=page_zero,
    )


def parse_csres(raw: bytes) -> list[Record]:
    """兼容入口：只要记录（发布日期取首个 title 字段，实施日期取第 4 列）。"""
    return parse_csres_page(raw).records


class CsresCurrency(CompletenessMixin):
    name = "csres"
    url = SEARCH_URL
    MAX_PAGES = 5

    def __init__(self, client: HttpClient | None = None, *, plaintext_fallback: bool = False) -> None:
        self.client = client if client is not None else HttpClient()
        self.plaintext_fallback = plaintext_fallback
        self.transport = "https"
        self.plaintext_risk = ""
        self._completeness = Completeness()

    @property
    def plaintext(self) -> bool:
        return self.transport == "http"

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> CsresCurrency:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _enter_plaintext(self) -> None:
        if self.transport == "http":
            return
        self.transport = "http"
        self.plaintext_risk = PLAINTEXT_RISK
        warnings.warn(PLAINTEXT_RISK, RuntimeWarning, stacklevel=3)

    def _request_page(self, code: str, page: int) -> bytes:
        query = urllib.parse.urlencode({"keyword": code, "pageNum": str(page)})
        if self.transport == "http":
            return self.client.get(f"{PLAINTEXT_SEARCH_URL}?{query}")
        try:
            return self.client.get(f"{SEARCH_URL}?{query}")
        except SourceUnreachable:
            if not self.plaintext_fallback:
                # 不自动降级：能阻断 TLS 的人不该顺带拿到明文通道（W3/P1-4）。
                raise
            self._enter_plaintext()
            return self.client.get(f"{PLAINTEXT_SEARCH_URL}?{query}")

    def _finish(self, fetched_pages: int, total: int | None, has_more: bool | None,
                *, truncated: bool = False, reason: str = "",
                empty_authoritative: bool = False) -> None:
        if self.plaintext:
            reason = f"{self.plaintext_risk}；{reason}" if reason else self.plaintext_risk
        self._record_completeness(Completeness(
            fetched_pages=fetched_pages, total=total, has_more=has_more, truncated=truncated,
            reason=reason, insecure_transport=self.plaintext,
            empty_authoritative=empty_authoritative,
        ))

    def lookup(self, code: str) -> list[Record]:
        if not code.strip():
            self._record_completeness(Completeness(has_more=False, reason="空查询，未访问上游"))
            return []
        collected: list[Record] = []
        total: int | None = None
        pages: int | None = None
        page = 1
        while True:
            parsed = parse_csres_page(self._request_page(code, page))
            if parsed.page is not None and parsed.page != page:
                # 服务端忽略 pageNum（重复返回同一页）时不能把重复记录当完整结果。
                raise page_error("csres", f"请求第 {page} 页，服务端返回第 {parsed.page} 页")
            # pageNum=0 + 0 条 = 站点明确的"无结果页"（0 不是第 0 页）。
            # 只有 total/pages 也为 0/None 才是权威空；正数 total/pages 与空记录
            # 自相矛盾，必须 50，不能悄悄当作"没有这本标准"。
            empty_answer = (
                page == 1
                and not parsed.records
                and parsed.page_zero
                and (parsed.total is None or parsed.total == 0)
                and (parsed.pages is None or parsed.pages == 0)
            )
            if page == 1 and not parsed.records and parsed.page_zero and not empty_answer:
                raise page_error(
                    "csres",
                    f"pageNum=0 但源声称共有 {parsed.total} 条/{parsed.pages} 页：响应自相矛盾",
                )
            collected.extend(parsed.records)
            if page == 1:
                total, pages = parsed.total, parsed.pages
                if pages is None and total is not None and parsed.page_size:
                    pages = max(1, -(-total // parsed.page_size))
            else:
                # 翻页期间总条数/总页数变化 = 结果集在动：混页会得到"看似完整"的假结论。
                if parsed.total is not None and total is not None and parsed.total != total:
                    raise page_error("csres", f"翻页期间总条数从 {total} 变为 {parsed.total}（第 {page} 页）")
                if parsed.pages is not None and pages is not None and parsed.pages != pages:
                    raise page_error("csres", f"翻页期间总页数从 {pages} 变为 {parsed.pages}（第 {page} 页）")
            if pages is None:
                if empty_answer:
                    # 源明确回答无结果：显式 has_more=False，不让"缺总页数"把权威空
                    # 说成"没取全"（明文下 complete 仍 False，但 empty_authoritative
                    # 让调用方区分 31 与 50）。
                    self._finish(page, total, False, empty_authoritative=True)
                    return filter_by_code(collected, code)
                # 没有总页数证据：只取第 1 页；has_more 保持 None，不把部分当完整。
                has_more = None if total is None else total > len(collected)
                self._finish(page, total, has_more,
                             reason="" if has_more is False else "页面未提供总页数，无法证明结果已取全")
                return filter_by_code(collected, code)
            if page >= pages:
                # 末页也要对账：源声称 total=N 但实际少行/末页为空 → 不完整，不写缓存。
                if total is not None and len(collected) > total:
                    reason = f"源声称共 {total} 条，实际取回 {len(collected)} 条（多余/重复行）"
                    self._finish(page, total, True, reason=reason)
                elif total is not None and len(collected) < total:
                    reason = f"源声称共 {total} 条，实际取回 {len(collected)} 条（末页不完整）"
                    self._finish(page, total, True, reason=reason)
                elif total is None and page > 1 and not parsed.records:
                    # 没有 total、不是第一页却给空页：无法证明前面已取全。
                    self._finish(page, total, True, reason="末页为空且总条数未知")
                else:
                    self._finish(page, total, False, empty_authoritative=empty_answer)
                return filter_by_code(collected, code)
            if page >= self.MAX_PAGES:
                reason = f"翻到第 {page} 页上限（{self.MAX_PAGES} 页），共 {pages} 页，结果被截断"
                self._finish(page, total, True, truncated=True, reason=reason)
                warnings.warn(f"csres {reason}：结果不完整，建议按名称复核", RuntimeWarning, stacklevel=2)
                return filter_by_code(collected, code)
            if not parsed.records:
                raise page_error("csres", f"第 {page} 页没有数据行但源声称共 {pages} 页：无法继续取全")
            page += 1
