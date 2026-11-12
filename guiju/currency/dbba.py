"""dbba 地方标准备案元数据；POST form，无正文下载或旧号状态推断。"""

from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone

from ..errors import NeedsHuman
from .codes import query_with_base_fallback
from .httpx import HttpClient, decode_json, json_int, json_rows, json_string
from .model import Record

SEARCH_URL = "https://dbba.sacinfo.org.cn/stdQueryList"
_CN_ZONE = timezone(timedelta(hours=8))
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_STATUS = {"现行": "current", "废止": "withdrawn", "有更新版": "superseded"}


def _ms_date(milliseconds: int) -> str:
    if milliseconds <= 0:
        return ""
    try:
        # 用整数拆分避免浮点精度损失；北京时间零点不能按 UTC 还原成前一天。
        seconds, remainder = divmod(milliseconds, 1000)
        instant = _EPOCH + timedelta(seconds=seconds, milliseconds=remainder)
        return instant.astimezone(_CN_ZONE).date().isoformat()
    except (OverflowError, ValueError) as exc:
        raise NeedsHuman("dbba 毫秒时间戳超出可解析日期范围", hint="检查 dbba 日期字段，不要把异常值当成空日期") from exc


def parse_dbba(raw: bytes) -> list[Record]:
    """JSON 原始字节的纯解析；字段类型异常由严格解码器报告 NeedsHuman。"""
    payload = decode_json("dbba", raw)
    json_int(payload, "total", "dbba")  # Go 的响应模型也校验此字段，即使不用于分页。
    out = []
    for item in json_rows(payload, "records", "dbba"):
        code = json_string(item, "code", "dbba")
        name = json_string(item, "chName", "dbba")
        raw_status = json_string(item, "status", "dbba")
        replaces = json_string(item, "reviseStdCodes", "dbba")
        act_date = json_int(item, "actDate", "dbba")
        issue_date = json_int(item, "issueDate", "dbba")
        fz_date = json_int(item, "fzDate", "dbba")
        status = _STATUS.get(raw_status.strip())
        if status is None:
            status = "withdrawn" if fz_date > 0 else "unknown"
        out.append(Record(
            code=code.strip(), name=name.strip(), status=status, source="dbba",
            publish_date=_ms_date(issue_date), implement_date=_ms_date(act_date),
            replaces=replaces.strip(), raw=raw_status,
        ))
    return out


class DbbaCurrency:
    name = "dbba"
    url = SEARCH_URL

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        self.pagination: dict = {"fetched_pages": 0, "total": 0, "has_more": False}

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "DbbaCurrency":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def lookup(self, code: str) -> list[Record]:
        if not code.strip():
            return []

        def fetch(term: str) -> list[Record]:
            # dbba 见到 Accept: application/json 会返回 HTML 跳转页，不能补上。
            raw = self.client.post_form(SEARCH_URL, {"current": "1", "size": "20", "key": term}, accept=None)
            # total 在响应里但分页契约未确认：如实报告只取了第 1 页（size=20）。
            total = json_int(decode_json(self.name, raw), "total", self.name) or 0
            records = parse_dbba(raw)
            self.pagination = {"fetched_pages": 1, "total": total, "has_more": total > len(records)}
            if total > len(records):
                warnings.warn(
                    f"dbba 返回 total={total} 条、仅取回第 1 页 {len(records)} 条：结果可能不完整",
                    RuntimeWarning, stacklevel=2,
                )
            elif total > 0 and not records:
                warnings.warn(
                    f"dbba 报 total={total} 但 0 条记录：可能是响应结构改版",
                    RuntimeWarning, stacklevel=2,
                )
            return records

        return query_with_base_fallback(code, fetch)
