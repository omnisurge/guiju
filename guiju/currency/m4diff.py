"""M4 只读逐字段对比：MCP 结果 vs 原生结果。

纯内存逻辑，不访问网络、不读写缓存；脚本 scripts/m4_currency_diff.py 负责采集。
对比字段以 ADR 0002 期 1 的 M4 门禁为准：status / name / implement_date。
编号键只用于对齐，不改变任何对外输出。
"""

from __future__ import annotations

from collections.abc import Iterable

from ..domain.shzjw import normalize_code
from .model import Record

FIELDS = ("status", "name", "implement_date")


def record_view(record: Record) -> dict[str, str]:
    return {"code": record.code, "source": record.source,
            **{field: (getattr(record, field) or "").strip() for field in FIELDS}}


def _index(rows: Iterable[Record]) -> tuple[dict[str, Record], list[str]]:
    indexed: dict[str, Record] = {}
    duplicates: list[str] = []
    for row in rows:
        key = normalize_code(row.code)
        if key in indexed:
            duplicates.append(key)
        else:
            indexed[key] = row
    return indexed, duplicates


def compare_rows(mcp_rows: Iterable[Record], native_rows: Iterable[Record]) -> dict:
    """返回可直接序列化的差异；空对空是合法一致，不能当缺失。"""
    mcp_index, mcp_duplicates = _index(mcp_rows)
    native_index, native_duplicates = _index(native_rows)
    differences: list[dict] = []

    for key in sorted(set(mcp_index) | set(native_index)):
        mcp_row = mcp_index.get(key)
        native_row = native_index.get(key)
        if mcp_row is None:
            differences.append({"kind": "missing_mcp", "code": key, "native": record_view(native_row)})
            continue
        if native_row is None:
            differences.append({"kind": "missing_native", "code": key, "mcp": record_view(mcp_row)})
            continue
        mcp_view = record_view(mcp_row)
        native_view = record_view(native_row)
        for field in FIELDS:
            if mcp_view[field] != native_view[field]:
                differences.append({
                    "kind": "field",
                    "code": key,
                    "field": field,
                    "mcp": mcp_view[field],
                    "native": native_view[field],
                })

    for side, duplicates in (("mcp", mcp_duplicates), ("native", native_duplicates)):
        for key in sorted(set(duplicates)):
            differences.append({"kind": "duplicate_code", "side": side, "code": key})

    return {
        "match": not differences,
        "mcp_count": len(mcp_index),
        "native_count": len(native_index),
        "both_empty": not mcp_index and not native_index,
        "differences": differences,
    }
