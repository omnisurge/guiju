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
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_DECORATION = re.compile(r"[（(][^（）()]*[）)]$")


def code_key(code: str) -> str:
    key = "".join(code.split()).translate(_DASHES).upper()
    return _NATURE.sub("", key)


def family_key(code: str) -> str:
    """完整基号：去尾部「（2024年版）」装饰与末尾 4 位年份，保留子册段。

    JTG/T 3360-01 与 JTG/T 3360-02 是两个标准，用"第一个横杠前的公共前缀"判同族
    会把兄弟子册混成一本（P2-17）；GB 50011-2010 与 GB 50011 只是同年不同年版。
    """
    key = code_key(code)
    while True:
        trimmed = _DECORATION.sub("", key)
        if trimmed == key:
            break
        key = trimmed
    segments = key.split("-")
    while len(segments) > 1 and _YEAR.match(segments[-1]):
        segments.pop()
    return "-".join(segments)


def same_family(left: str, right: str) -> bool:
    """left 是查询编号、right 是候选记录。

    相等即同族；查询只给到基号（没写子册段）时接受它的子册记录；
    反过来不接受——查询明确到 -01 时，-02 不能算命中。
    """
    query, candidate = family_key(left), family_key(right)
    if not query or not candidate:
        return False
    return query == candidate or candidate.startswith(query + "-")


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
