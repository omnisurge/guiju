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

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        # 查询标注：只取第 1 页（pageSize 20）；Total 超过已取条数时 has_more=True，
        # 翻页契约（排序/续页字段）未经确认，由调用方决定是否人工复核。
        self.pagination: dict = {"fetched_pages": 0, "total": 0, "has_more": False}

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
            raw = self.client.post_form(
                SEARCH_URL,
                {"pageIndex": "1", "pageSize": "20", "gjz": key},
                accept="application/json",
            )
            payload = decode_json("ccsn", raw)
            total = json_int(payload, "Total", "ccsn")
            records = parse_ccsn(raw)
            self.pagination = {
                "fetched_pages": 1,
                "total": total,
                "has_more": total > len(records),
            }
            if self.pagination["has_more"]:
                warnings.warn(
                    f"ccsn 返回 {total} 条、仅取回第 1 页 {len(records)} 条："
                    "分页契约未经确认，结果可能不完整",
                    stacklevel=2,
                )
            return records

        # 先过滤同号族，确实无命中后才按基础号再查一次；不补造旧版本结论。
        return query_with_base_fallback(code, fetch)
