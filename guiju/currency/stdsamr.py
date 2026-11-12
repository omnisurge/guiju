"""全国标准信息公共服务平台元数据（std_samr），不读取正文链接。"""

from __future__ import annotations

import html
import re
import urllib.parse
import warnings

from .codes import query_with_base_fallback
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


def parse_stdsamr(raw: bytes) -> list[Record]:
    """纯解析 JSON；保留源站记录顺序、日期文本及原状态证据。"""
    payload = decode_json("std_samr", raw)
    json_int(payload, "total", "std_samr")  # Go 响应字段为 int，虽不参与筛选也须校验类型。
    records = []
    for item in json_rows(payload, "rows", "std_samr"):
        state = json_string(item, "STATE", "std_samr").strip()
        records.append(Record(
            code=strip_tags(json_string(item, "C_STD_CODE", "std_samr")),
            name=strip_tags(json_string(item, "C_C_NAME", "std_samr")),
            status=status_stdsamr(state),
            publish_date=json_string(item, "ISSUE_DATE", "std_samr").strip(),
            implement_date=json_string(item, "ACT_DATE", "std_samr").strip(),
            source="std_samr",
            raw=state,
        ))
    return records


class StdSamrCurrency:
    name = "std_samr"
    url = SEARCH_URL

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        self.pagination: dict = {"fetched_pages": 0, "total": 0, "has_more": False}

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "StdSamrCurrency":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lookup(self, code: str) -> list[Record]:
        if not code.strip():
            return []

        def fetch(term: str) -> list[Record]:
            query = urllib.parse.urlencode({
                "searchText": term,
                "ics": "",
                "state": "",
                "ISSUE_DATE": "",
            })
            raw = self.client.get(f"{SEARCH_URL}?{query}", accept="application/json")
            # total 在响应里但没分页契约：如实报告只取了第 1 页，超出部分告警。
            total = json_int(decode_json(self.name, raw), "total", self.name) or 0
            records = parse_stdsamr(raw)
            self.pagination = {"fetched_pages": 1, "total": total, "has_more": total > len(records)}
            if total > len(records):
                warnings.warn(
                    f"std_samr 返回 total={total} 条、仅取回第 1 页 {len(records)} 条：结果可能不完整",
                    RuntimeWarning, stacklevel=2,
                )
            elif total > 0 and not records:
                warnings.warn(
                    f"std_samr 报 total={total} 但 0 条记录：可能是响应结构改版",
                    RuntimeWarning, stacklevel=2,
                )
            return records

        return query_with_base_fallback(code, fetch)
