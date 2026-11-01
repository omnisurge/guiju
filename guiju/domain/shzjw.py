"""上海住建委唯一元数据核心：从正文源机械提取，不负责 HTTP 或文件下载。

只消费已解码的 JSON；内容源与原生时效适配器分别注入自己的顺序 fetch。
归档链接沿用源站地址，但这里只构造字符串，不生成下载令牌、不执行下载。
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable

from ..errors import NeedsHuman
from .codes import normalize_key

BASE = "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWebApi/"
PAGE = "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWeb/pc/#/standardQuery"
REFERER = "https://bzjg.zjw.sh.gov.cn:9175/JGBStandardWeb/pc/"
QUERY_URL = f"{BASE}external/localStandard/list"
MAX_PAGES = 1000

_TEXT_FIELDS = (
    "standardNumber",
    "standardProjectName",
    "releaseTime",
    "implementationTime",
    "abolishTime",
    "standardFileId",
    "editorUnitName",
)


def _error(detail: str) -> NeedsHuman:
    return NeedsHuman(
        f"上海标准元数据结构异常：{detail}",
        hint="检查上海住建委查询响应及分页；不要把解析失败或不完整结果当作无命中",
    )


def normalize_code(ref: str) -> str:
    """沿用编号归一化，额外接受 U+2212；不改官方编号或拆解版本。"""
    return normalize_key(ref).replace("−", "-")


def download_url(file_id: str) -> str:
    """不带令牌的归档地址；真正下载时才由内容源签名。"""
    return f"{BASE}api/files/webDownload?fileId={file_id}"


def record(
    row: dict, *, abolished: bool | None = None, standard_type: int | None = None
) -> dict:
    """沿用旧源字段映射；请求分栏只补充上下文，不覆盖显式废止证据。"""
    if not isinstance(row, dict):
        raise _error("list 中的条目必须是 object")
    for key in _TEXT_FIELDS:
        if row.get(key) is not None and not isinstance(row[key], str):
            raise _error(f"{key} 必须是 string 或 null")
    if row.get("isAbolish") is not None and not isinstance(row["isAbolish"], bool):
        raise _error("isAbolish 必须是 boolean 或 null")
    row_type = row.get("standardType")
    if row_type is not None and (type(row_type) not in (int, str) or row_type not in (0, 1, "0", "1")):
        raise _error('standardType 必须是 0、1、"0"、"1" 或 null')
    if abolished is not None and not isinstance(abolished, bool):
        raise _error("废止分栏必须是 boolean")
    if standard_type is not None and (type(standard_type) is not int or standard_type not in (0, 1)):
        raise _error("标准分栏必须是 0 或 1")

    code = (row.get("standardNumber") or "").strip()
    name = (row.get("standardProjectName") or "").strip()
    published = (row.get("releaseTime") or "")[:10]
    implement = (row.get("implementationTime") or "")[:10]
    abolish = (row.get("abolishTime") or "")[:10]
    fid = row.get("standardFileId")
    # 站点自己分栏；废止栏即使缺少行级标记也不能变成现行。
    is_abolished = abolished is True or bool(row.get("abolishTime")) or row.get("isAbolish") is True
    is_design = row_type in (1, "1") if row_type is not None else standard_type == 1
    return {
        "code": code,
        "code_key": normalize_code(code) if code else None,
        "name": name,
        "status": "withdrawn" if is_abolished else "current",
        "kind": "design" if is_design else "standard",
        "published_on": published,
        "implement_on": implement,
        "abolish_on": abolish or None,
        "file_id": fid,
        "download_url": download_url(fid) if fid else None,
        "page_url": f"{PAGE}?keyword={urllib.parse.quote(code or name)}",
        "editor": (row.get("editorUnitName") or "").strip(),
    }


def parse_page(
    payload: dict, *, abolished: bool = False, standard_type: int = 0
) -> tuple[list[dict], int]:
    """解析查询页；只有有效空 list / total=0 才能代表空结果。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise _error("data 必须是 object")
    if "success" in payload and type(payload["success"]) is not bool:
        raise _error("success 存在时必须是 boolean")
    if payload.get("success") is False:
        raise _error("接口明确返回 success=false")
    data = payload["data"]
    rows = data.get("list")
    total = data.get("total")
    if not isinstance(rows, list):
        raise _error("data.list 必须是 array")
    if type(total) is not int or total < 0:
        raise _error("data.total 必须是非负 integer")
    if len(rows) > total:
        raise _error("条目数超过 data.total")
    records = [record(row, abolished=abolished, standard_type=standard_type) for row in rows]
    if any(not (rec["code"] or rec["name"]) for rec in records):
        raise _error("条目缺少编号和名称")
    return records, total


def search(
    ref: str,
    *,
    fetch: Callable[[dict], dict],
    page_size: int = 30,
    abolished: bool = False,
    standard_type: int = 0,
    all_pages: bool = False,
) -> list[dict]:
    """按四栏之一检索；完整模式顺序分页，任何不完整证据都返回 50。

    默认仍只读第一页，以保留正文源的旧接口行为。all_pages=True 的调用者
    不能把短页、重复页、变化的 total 或页数上限当作检索结束。
    """
    if type(page_size) is not int or page_size <= 0:
        raise _error("page_size 必须是正 integer")
    results: list[dict] = []
    seen: set[tuple] = set()
    expected_total: int | None = None
    for page_no in range(1, MAX_PAGES + 1):
        payload = fetch(
            {
                "pageNum": page_no,
                "pageSize": page_size,
                "isAbolish": abolished,
                "standardType": standard_type,
                "condition": ref,
                "publishStartDate": "",
                "publishEndDate": "",
                "implementStartDate": "",
                "implementEndDate": "",
            }
        )
        rows, total = parse_page(payload, abolished=abolished, standard_type=standard_type)
        if not all_pages:
            return rows
        if expected_total is None:
            expected_total = total
            if (total + page_size - 1) // page_size > MAX_PAGES:
                raise _error(f"分页超过 {MAX_PAGES} 页上限")
        elif total != expected_total:
            raise _error("分页 data.total 发生变化，结果不完整")
        expected_size = min(page_size, expected_total - len(results))
        if len(rows) != expected_size:
            raise _error(f"第 {page_no} 页条目数与 data.total 不符，结果不完整")
        for rec in rows:
            identity = tuple(rec.values())
            if identity in seen:
                raise _error("分页返回重复条目或重复页，结果不完整")
            seen.add(identity)
        results.extend(rows)
        if len(results) == expected_total:
            return results
    raise _error(f"分页超过 {MAX_PAGES} 页上限")


def _design_first(ref: str) -> bool:
    """图集/设计标准编号（DBJT…）先查 design 分栏，省一次请求。"""
    return ref.strip().upper().replace(" ", "").startswith("DBJT")


def find(
    ref: str,
    *,
    searcher: Callable[..., list[dict]],
    limit: int | None = 10,
    prefer_exact: bool = True,
) -> list[dict]:
    """沿用四栏顺序；关闭精确偏好时保留调用方已过滤的全部编号结果。"""
    types = [1, 0] if _design_first(ref) else [0, 1]
    hits: list[dict] = []
    for stype in types:
        hits.extend(searcher(ref, standard_type=stype))
        if prefer_exact and _exact(hits, ref):
            break
    if not prefer_exact or not _exact(hits, ref):
        for stype in types:
            hits.extend(searcher(ref, abolished=True, standard_type=stype))
    return _rank(hits, ref, limit=limit, prefer_exact=prefer_exact)


def _exact(hits: list[dict], ref: str) -> list[dict]:
    want = normalize_code(ref) if ref.strip() else ""
    by_code = [r for r in hits if want and r["code_key"] and normalize_code(r["code_key"]) == want]
    if by_code:
        return by_code
    return [r for r in hits if r["name"] == ref.strip()]


def _rank(
    hits: list[dict], ref: str, *, limit: int | None = 10, prefer_exact: bool = True
) -> list[dict]:
    """现行优先 + 编号去重；默认精确优先，可显式保留整个去重集合。"""
    seen: dict[str, dict] = {}
    for rec in hits:
        key = normalize_code(rec["code_key"]) if rec["code_key"] else rec["name"]
        old = seen.get(key)
        if old is None or (old["status"] != "current" and rec["status"] == "current"):
            seen[key] = rec
    unique = list(seen.values())
    exact = _exact(unique, ref) if prefer_exact else []
    return (exact or unique)[:limit]
