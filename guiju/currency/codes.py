"""上游模糊搜索的号族过滤：比对键不改变输出编号。

照搬 Go adapter/codes.go 的语义，不复用 domain.codes.normalize_key：
后者用于精确版本身份，必须保留推荐性标记与年份。
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .model import Record

_DASHES = str.maketrans({c: "-" for c in "—–－−"})
_NATURE = re.compile(r"/[TZ](?=[0-9])")


def code_key(code: str) -> str:
    key = "".join(code.split()).translate(_DASHES).upper()
    return _NATURE.sub("", key)


def same_family(left: str, right: str) -> bool:
    left_key, right_key = code_key(left), code_key(right)
    return bool(left_key and right_key and left_key.partition("-")[0] == right_key.partition("-")[0])


def filter_by_code(rows: list[Record], code: str) -> list[Record]:
    return [row for row in rows if same_family(code, row.code)]


def base_code(code: str) -> str:
    if len(code) >= 5 and code[-5] == "-" and all("0" <= c <= "9" for c in code[-4:]):
        return code[:-5].strip()
    return code


def code_num_key(code: str) -> str:
    tail = code.rsplit("/", 1)[-1]
    start = next((i for i, char in enumerate(tail) if "0" <= char <= "9"), -1)
    if start < 0:
        return ""
    end = start
    while end < len(tail) and tail[end] in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-.":
        end += 1
    return tail[start:end]


def date_only(value: str) -> str:
    value = value.strip()
    if len(value) >= 10 and value[4] == "-" and value[7] == "-":
        return value[:10]
    return value


def query_with_base_fallback(code: str, fetch: Callable[[str], list[Record]]) -> list[Record]:
    rows = filter_by_code(fetch(code), code)
    if rows:
        return rows
    base = base_code(code)
    if not base or base == code:
        return rows
    return filter_by_code(fetch(base), code)
