"""ccsn 工程建设标准元数据：只查查询接口，不读取正文或推断旧版状态。"""

from __future__ import annotations

import warnings

from .codes import code_num_key, date_only, query_with_base_fallback
from .contract import Completeness, CompletenessMixin, json_error, parse_or_fail, require_keys
from .httpx import HttpClient, decode_json, json_flag, json_int, json_rows, json_string
from .model import Record

# .assx 疑似站方把 .ashx 打错的端点；但白名单（httpx._ENDPOINTS）与历史可用性都以
# 这个字面量为准，贸然"修正"反而会断开服务 —— 保持原样，仅留此注释。
SEARCH_URL = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
# 「在编」是编制中，不是「即将实施」（W3/P2-18）：只能判 unknown。
_STATUS = {"现行": "current", "废止": "withdrawn", "即将实施": "upcoming"}
# 明确但不可判定现行性的状态词：即便响应里带了 AbolishDate 也不改判（不猜）。
_UNKNOWN_WORDS = frozenset({"在编", "局修完成"})


def status_ccsn(state: str, abolish_date: str, replaces: str = "") -> str:
    """照录源站证据；「局修完成」本身不足以判断现行或已替代。

    废止证据 = AbolishDate：带 ReplaceStandardCode 才是「被替代」(superseded)，
    没有替代编号只能记「废止」(withdrawn)。
    """
    word = state.strip()
    status = _STATUS.get(word)
    if status is not None:
        return status
    if word in _UNKNOWN_WORDS:
        # 「在编」是编制中（不是即将实施）、「局修完成」只说明一次局部修订流程结束：
        # 既不能证明被代替，也不能证明仍现行。
        return "unknown"
    if abolish_date.strip():
        return "superseded" if replaces.strip() else "withdrawn"
    return "unknown"


def _parse_page(payload: dict) -> tuple[list[Record], int, int]:
    """校验一页响应结构并解析；返回 (records, 原始行数, Total)。"""
    require_keys(payload, ("Total", "List"), "ccsn")
    total = json_int(payload, "Total", "ccsn")  # Go 响应字段为 int，虽不参与筛选也须校验类型。
    rows = json_rows(payload, "List", "ccsn")
    records = []
    for item in rows:
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
            status=status_ccsn(state, abolish_date, replaces),
            publish_date=publish_date,
            implement_date=implement_date,
            replaces=replaces,
            source="ccsn",
            raw=state,
        ))
    return parse_or_fail(rows, records, "ccsn", where="List"), len(rows), total


def parse_ccsn(raw: bytes) -> list[Record]:
    """纯解析上游 JSON；保留原状态词、替代关系及发布日期。"""
    records, _, _ = _parse_page(decode_json("ccsn", raw))
    return records


class CcsnCurrency(CompletenessMixin):
    name = "ccsn"
    url = SEARCH_URL

    #: 一次 lookup 允许的翻页上限。ccsn 的 gjz 只按数字段检索，宽泛词（如 "500"）
    #: 可把 Total 拉到上千，翻 80+ 页既慢又浪费——超过上限直接记 truncated 交人复核。
    MAX_PAGES = 5

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        # 分页契约（2026-11-13 实测 .assx 站）：pageIndex 1-based，返回 PageInfo
        # {Total, List, PageNum, Pages, HasNextPage}；HasNextPage 为真时按 pageIndex+1
        # 续页，直到无下页或 MAX_PAGES 封顶。truncated=True 表示被封顶截断（结果不完整，
        # 不写缓存、调用方应把人召回来）。
        self._completeness = Completeness()

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
            # 请求前短路：这是"本源不适用"的确定性结论，不是上游给的负结果。
            self._record_completeness(Completeness(has_more=False, reason="编号没有数字段，ccsn 不适用"))
            return []

        def fetch(term: str) -> list[Record]:
            key = code_num_key(term)
            if not key:
                self._record_completeness(Completeness(has_more=False))
                return []
            records: list[Record] = []
            rows_seen = 0
            total = 0
            page = 1
            while True:
                payload = decode_json("ccsn", self.client.post_form(
                    SEARCH_URL,
                    {"pageIndex": str(page), "pageSize": "20", "gjz": key},
                    accept="application/json",
                ))
                page_records, page_rows, page_total = _parse_page(payload)
                if page == 1:
                    total = page_total
                elif page_total != total:
                    # 翻页期间总数变化 = 结果集在动，混合两页会得到"看似完整"的假结论。
                    raise json_error("ccsn", f"翻页期间 Total 从 {total} 变为 {page_total}（第 {page} 页）")
                has_next = json_flag(payload, "HasNextPage", "ccsn") is True
                records.extend(page_records)
                rows_seen += page_rows
                if not has_next or page >= self.MAX_PAGES:
                    truncated = has_next
                    has_more = has_next or total > rows_seen
                    if truncated:
                        reason = f"翻到第 {page} 页上限（{self.MAX_PAGES} 页），结果被截断"
                        warnings.warn(
                            f"ccsn 返回 {total} 条、翻到第 {page} 页截断（上限 {self.MAX_PAGES} 页）："
                            f"结果不完整，建议按名称复核",
                            stacklevel=2,
                        )
                    elif has_more:
                        reason = f"源声称共有 {total} 条，实际取回 {rows_seen} 条，未取全"
                    else:
                        reason = ""
                    self._record_completeness(Completeness(
                        fetched_pages=page, total=total, has_more=has_more,
                        truncated=truncated, reason=reason,
                    ))
                    return records
                page += 1

        # 先过滤同号族，确实无命中后才按基础号再查一次；不补造旧版本结论。
        return query_with_base_fallback(code, fetch)
