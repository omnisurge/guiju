"""ccsn 工程建设标准元数据：只查查询接口，不读取正文或推断旧版状态。"""

from __future__ import annotations

import warnings

from .codes import code_num_key, date_only, query_with_base_fallback
from .httpx import HttpClient, decode_json, json_int, json_rows, json_string
from .model import Record

# .assx 疑似站方把 .ashx 打错的端点；但白名单（httpx._ENDPOINTS）与历史可用性都以
# 这个字面量为准，贸然"修正"反而会断开服务 —— 保持原样，仅留此注释。
SEARCH_URL = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
_STATUS = {"现行": "current", "废止": "withdrawn", "即将实施": "upcoming", "在编": "upcoming"}


def status_ccsn(state: str, abolish_date: str) -> str:
    """照录源站证据；「局修完成」本身不足以判断现行或已替代。"""
    status = _STATUS.get(state.strip())
    if status is not None:
        return status
    if state.strip() == "局修完成":
        # 「局修完成」只说明一次局部修订流程结束：既不能证明被代替，也不能证明仍现行
        return "unknown"
    return "superseded" if abolish_date.strip() else "unknown"


def parse_ccsn(raw: bytes) -> list[Record]:
    """纯解析上游 JSON；保留原状态词、替代关系及发布日期。"""
    payload = decode_json("ccsn", raw)
    json_int(payload, "Total", "ccsn")  # Go 响应字段为 int，虽不参与筛选也须校验类型。
    records = []
    for item in json_rows(payload, "List", "ccsn"):
        code = json_string(item, "StandardCode", "ccsn").strip()
        name = json_string(item, "StandardCNName", "ccsn").strip()
        state = json_string(item, "StandardState", "ccsn")
        abolish_date = json_string(item, "AbolishDate", "ccsn")
        replaces = json_string(item, "ReplaceStandardCode", "ccsn").strip()
        publish_date = date_only(json_string(item, "PublishDate", "ccsn"))
        implement_date = date_only(json_string(item, "PerformDate", "ccsn"))
        if not code:
            continue
        records.append(Record(
            code=code,
            name=name,
            status=status_ccsn(state, abolish_date),
            publish_date=publish_date,
            implement_date=implement_date,
            replaces=replaces,
            source="ccsn",
            raw=state,
        ))
    return records


class CcsnCurrency:
    name = "ccsn"
    url = SEARCH_URL

    #: 一次 lookup 允许的翻页上限。ccsn 的 gjz 只按数字段检索，宽泛词（如 "500"）
    #: 可把 Total 拉到上千，翻 80+ 页既慢又浪费——超过上限直接记 truncated 交人复核。
    MAX_PAGES = 5

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        # 分页契约（2026-11-13 实测 .assx 站）：pageIndex 1-based，返回 PageInfo
        # {Total, List, PageNum, Pages, HasNextPage}；Total>len(已取) 时按
        # pageIndex+1 续页，直到无下页或 MAX_PAGES 封顶。truncated=True 表示被
        # 封顶截断（结果不完整），调用方应把人召回来，不要当下游真理。
        self.pagination: dict = {
            "fetched_pages": 0, "total": 0, "has_more": False, "truncated": False,
        }

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> CcsnCurrency:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def lookup(self, code: str) -> list[Record]:
        # gjz 不认字母前缀；没有数字段会查全库，故必须在请求前短路。
        if not code_num_key(code):
            return []

        def fetch(term: str) -> list[Record]:
            key = code_num_key(term)
            if not key:
                return []
            records: list[Record] = []
            total = 0
            page = 1
            while True:
                raw = self.client.post_form(
                    SEARCH_URL,
                    {"pageIndex": str(page), "pageSize": "20", "gjz": key},
                    accept="application/json",
                )
                payload = decode_json("ccsn", raw)
                if page == 1:
                    total = json_int(payload, "Total", "ccsn")
                records.extend(parse_ccsn(raw))
                has_next = bool(payload.get("HasNextPage"))
                if not has_next or page >= self.MAX_PAGES:
                    truncated = has_next and page >= self.MAX_PAGES
                    self.pagination = {
                        "fetched_pages": page,
                        "total": total,
                        "has_more": total > len(records),
                        "truncated": truncated,
                    }
                    if truncated:
                        warnings.warn(
                            f"ccsn 返回 {total} 条、翻到第 {page}/{page} 页截断"
                            f"（上限 {self.MAX_PAGES} 页）：结果不完整，建议按名称复核",
                            stacklevel=2,
                        )
                    return records
                page += 1

        # 先过滤同号族，确实无命中后才按基础号再查一次；不补造旧版本结论。
        return query_with_base_fallback(code, fetch)
