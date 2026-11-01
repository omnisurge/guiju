"""上海住建委唯一权威的原生时效入口：复用 domain.shzjw，不复制解析器。"""

from __future__ import annotations

from collections.abc import Callable

from ..domain import shzjw as official
from ..domain.shzjw import normalize_code
from ..errors import NeedsHuman
from .httpx import HttpClient, decode_json
from .model import QueryCancelled, Record

# DG/TJ08、DBJT08、DB31/T 有既有识别/样例；其余08形式按上海历史家族保守预留。
# 这是官方查询的归属边界，不是逐条收录清单；不泛化全国DBJ/DBJT或全部DB31标准。
SHANGHAI_PREFIXES = ("DG/TJ08", "DGJ08", "DBJT08", "DBTJ08", "DBJ08", "DB31/T")


def is_shanghai_code(code: str) -> bool:
    key = normalize_code(code)
    for prefix in SHANGHAI_PREFIXES:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        if prefix == "DB31/T":
            return not rest or "0" <= rest[0] <= "9"
        return not rest or rest.startswith("-")
    return False


def owns_shanghai_namespace(code: str) -> bool:
    """已确认前缀及保守预留的上海 DB31 空间不得落通用 DB 外源。"""
    return normalize_code(code).startswith((*SHANGHAI_PREFIXES, "DB31"))


def _valid_query(key: str) -> bool:
    if not is_shanghai_code(key):
        return False
    for prefix in SHANGHAI_PREFIXES:
        if key.startswith(prefix):
            rest = key[len(prefix):]
            if prefix != "DB31/T":
                rest = rest.removeprefix("-")
            return bool(rest) and "0" <= rest[0] <= "9"
    return False


def _explicit_version(key: str) -> bool:
    return key.count("-") >= (1 if key.startswith("DB31/T") else 2)


def matches(code: str, candidate: str) -> bool:
    """完整官方编号精确匹配，或已确认的「基础号-四位版年」扩展。

    不拆第一个横杠：DG/TJ08-2007 与 DG/TJ08-9999 绝非同号；不抹 /T。
    版年只用于识别编号扩展，不能用于推断现行性。
    """
    want, got = normalize_code(code), normalize_code(candidate)
    if not _valid_query(want) or not _valid_query(got):
        return False
    if got == want:
        return True
    # 已明确带版年/子段的完整输入不再追加“新年份”，只能全相等。
    if _explicit_version(want):
        return False
    if got.startswith(want + "-"):
        suffix = got[len(want) + 1:]
        return len(suffix) == 4 and all("0" <= char <= "9" for char in suffix)
    return False


class ShzjwCurrency:
    name = "shzjw"
    url = official.QUERY_URL
    source_names = ("shzjw",)
    cache_namespace = "shzjw:official-single-v2:strict-code-all-base-versions"
    matches = staticmethod(matches)

    def __init__(self, client: HttpClient | None = None, *, cancelled: Callable[[], bool] | None = None) -> None:
        self.client = client if client is not None else HttpClient()
        self.cancelled = cancelled

    def _check(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise QueryCancelled("上海查询已取消，不请求其他分栏")

    def _fetch(self, payload: dict) -> dict:
        self._check()
        raw = self.client.post_json(self.url, payload)
        self._check()
        return decode_json(self.name, raw)

    def _search(self, ref: str, *, abolished: bool = False, standard_type: int = 0) -> list[dict]:
        rows = official.search(ref, fetch=self._fetch, page_size=100, abolished=abolished,
                               standard_type=standard_type, all_pages=True)
        accepted = []
        want = normalize_code(ref)
        for row in rows:
            if matches(want, row["code"]):
                accepted.append(row)
            elif normalize_code(row["code"]).startswith(want + "-"):
                # 未确认的子册/修订后缀不当作无记录，更不能猜成同一版本。
                raise NeedsHuman("上海官方返回暂不能安全比对的编号后缀", hint=f"请人工确认 {row['code']} 与 {ref} 的关系")
        return accepted

    def lookup(self, code: str) -> list[Record]:
        self._check()
        key = normalize_code(code)
        if not _valid_query(key):
            return []
        rows = official.find(key, searcher=self._search, limit=None, prefer_exact=_explicit_version(key))
        self._check()
        return [Record(code=row["code"], name=row["name"], status=row["status"],
                       publish_date=row["published_on"], implement_date=row["implement_on"], source=self.name)
                for row in rows]

    @staticmethod
    def accepts_record(code: str, row: Record) -> bool:
        return (row.source == "shzjw" and not row.fallback and row.status in {"current", "withdrawn"}
                and matches(code, row.code))
