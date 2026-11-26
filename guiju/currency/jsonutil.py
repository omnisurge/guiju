"""Pure strict JSON decoding and shape validation helpers for currency adapters."""

from __future__ import annotations

import json

from .contract import json_error as _json_error

__all__ = ["decode_json", "json_flag", "json_int", "json_rows", "json_string"]


def decode_json(source: str, raw: bytes) -> dict:
    def reject_constant(value):
        raise ValueError(f"非法 JSON 数值 {value}")

    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        snippet = raw[:160].decode("utf-8", "replace").strip().replace("\r", " ").replace("\n", " ")
        raise _json_error(source, f"不是有效 UTF-8 JSON；响应开头：{snippet}") from exc
    if not isinstance(payload, dict):
        raise _json_error(source, "顶层必须为对象")
    return payload


def json_rows(payload: dict, key: str, source: str) -> list[dict]:
    rows = payload.get(key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise _json_error(source, f"{key} 必须为数组")
    if any(row is not None and not isinstance(row, dict) for row in rows):
        raise _json_error(source, f"{key} 的条目必须为对象")
    return [row if row is not None else {} for row in rows]


def json_string(item: dict, key: str, source: str) -> str:
    value = item.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _json_error(source, f"{key} 必须为字符串")
    return value


def json_int(item: dict, key: str, source: str) -> int:
    value = item.get(key)
    if value is None:
        return 0
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise _json_error(source, f"{key} 必须为 int64 整数")
    return value


def json_flag(item: dict, key: str, source: str) -> bool | None:
    """Boolean flag (e.g. ccsn HasNextPage); null means unknown; 0/1 accepted."""
    value = item.get(key)
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise _json_error(source, f"{key} 必须为布尔值")
