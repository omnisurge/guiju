"""全国标准信息公共服务平台元数据（std_samr），不读取正文链接。"""

from __future__ import annotations

import html
import re
import urllib.parse
import warnings

from .codes import query_with_base_fallback
from .contract import Completeness, CompletenessMixin, json_error, parse_or_fail, require_keys
from .httpx import HttpClient, decode_json, json_int, json_rows, json_string
from .model import Record

SEARCH_URL = "https://std.samr.gov.cn/gb/search/gbQueryPage"
_TAGS = re.compile(r"<[^>]*>")
_STATUS = {"现行": "current", "被代替": "superseded", "被替代": "superseded", "废止": "withdrawn"}


def strip_tags(text: str) -> str:
    """按 Go 口径先去高亮标签，再反转义实体；不归一化编号或内部空白。"""
    return html.unescape(_TAGS.sub("", text)).strip()


def status_stdsamr(state: str) -> str:
    return _STATUS.get(state.strip(), "unknown")


def _parse_page(payload: dict) -> tuple[list[Record], int, int]:
    """校验响应结构并解析一页；返回 (records, 原始行数, total)。"""
    require_keys(payload, ("total", "rows"), "std_samr")
    total = json_int(payload, "total", "std_samr")  # Go 响应字段为 int，虽不参与筛选也须校验类型。
    rows = json_rows(payload, "rows", "std_samr")
    records = []
    for item in rows:
        state = json_string(item, "STATE", "std_samr").strip()
        code = strip_tags(json_string(item, "C_STD_CODE", "std_samr"))
        if not code:
            raise json_error("std_samr", "C_STD_CODE 为空，不能把坏行当成记录")
        records.append(Record(
            code=code,
            name=strip_tags(json_string(item, "C_C_NAME", "std_samr")),
            status=status_stdsamr(state),
            publish_date=json_string(item, "ISSUE_DATE", "std_samr").strip(),
            implement_date=json_string(item, "ACT_DATE", "std_samr").strip(),
            source="std_samr",
            raw=state,
        ))
    if not rows and total > 0:
        # 声称有 N 条却一行没给：结构不符，不是"没有这本标准"（W3/P1-4）。
        raise json_error("std_samr", f"total={total} 却返回 0 条记录")
    return parse_or_fail(rows, records, "std_samr", where="rows"), len(rows), total


def parse_stdsamr(raw: bytes) -> list[Record]:
    """纯解析 JSON；保留源站记录顺序、日期文本及原状态证据。"""
    records, _, _ = _parse_page(decode_json("std_samr", raw))
    return records


class StdSamrCurrency(CompletenessMixin):
    name = "std_samr"
    url = SEARCH_URL

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        self._completeness = Completeness()

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> StdSamrCurrency:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lookup(self, code: str) -> list[Record]:
        if not code.strip():
            self._record_completeness(Completeness(has_more=False, reason="空查询，未访问上游"))
            return []

        def fetch(term: str) -> list[Record]:
            query = urllib.parse.urlencode({
                "searchText": term,
                "ics": "",
                "state": "",
                "ISSUE_DATE": "",
            })
            raw = self.client.get(f"{SEARCH_URL}?{query}", accept="application/json")
            # total 在响应里但没分页契约：如实报告只取了第 1 页，超出部分告警且不写缓存。
            records, rows, total = _parse_page(decode_json(self.name, raw))
            has_more = total > rows
            if has_more:
                warnings.warn(
                    f"std_samr 返回 total={total} 条、仅取回第 1 页 {rows} 条：结果不完整",
                    RuntimeWarning, stacklevel=2,
                )
            self._record_completeness(Completeness(
                fetched_pages=1, total=total, has_more=has_more,
                reason=f"仅取回第 1 页 {rows} 条，源声称共 {total} 条" if has_more else "",
            ))
            return records

        return query_with_base_fallback(
            code,
            fetch,
            snapshot=lambda: self._completeness,
            restore=self._record_completeness,
        )
