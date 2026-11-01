"""上海工程建设标准（bzjg.zjw.sh.gov.cn）—— 地标正文源。

为什么这个源不像别的源：页面是 SPA，没有可抓的 HTML 详情页，数据全在 JSON API 里。
所以这里按 API 语义实现（检索 / 下载），不硬套"公告页 + 附件链接"的流程。

API（2026-10-31 实测）
--------------------
  POST {BASE}external/localStandard/list
       {pageNum, pageSize, isAbolish:false, standardType:0, condition:"<关键词或编号>",
        publishStartDate, publishEndDate, implementStartDate, implementEndDate}
       → data.total / data.list[]（standardNumber, standardProjectName, releaseTime,
         implementationTime, abolishTime, standardFileId, …）

  GET  {BASE}api/files/webDownload?fileId=&timestamp=&receivedToken=
       receivedToken = HMAC_SHA256("<fileId>:<timestamp ms>", "jkysh2jgbstandard")
       → application/pdf，Content-Disposition: attachment

关于令牌：密钥由站点自己写在公开的前端 bundle 里（不是我们在破解什么）。
本模块只做"它自己的页面会做的同一件事"，并沿用 1.2s/host 的限速。

编号口径：站点给的是含年版的编号（如 `DG/TJ08-2007-2026`），
直接采用、不做二次解析 —— 通用编号解析器会把 -2026 误当成年份并判成歧义。
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from pathlib import Path

from .. import compliance
from ..domain import shzjw as core
from ..domain.codes import normalize_key as normalize_key
from ..errors import BadAttachment, NeedsHuman, NotIndexed
from .mohurd import MAGIC_PDF, magic_of

BASE = core.BASE
PAGE = core.PAGE
TOKEN_KEY = b"jkysh2jgbstandard"
REFERER = core.REFERER
QUERY_URL = core.QUERY_URL

# 兼容旧调用方的导出；元数据映射与排序只有 domain 中这一份实现。
download_url = core.download_url
_record = core.record
_exact = core._exact
_rank = core._rank
_design_first = core._design_first


def _token(file_id: str, ts: int) -> str:
    return hmac.new(TOKEN_KEY, f"{file_id}:{ts}".encode(), hashlib.sha256).hexdigest()


def file_id_of(url: str) -> str:
    q = urllib.parse.urlsplit(url).query
    fid = urllib.parse.parse_qs(q).get("fileId", [""])[0]
    if not fid:
        raise NotIndexed(f"下载地址里没有 fileId：{url[:80]}")
    return fid


def _fetch(query: dict) -> dict:
    """内容源仍走原合规传输；无效 JSON 不再漏成 internal 或空命中。"""
    try:
        return compliance.post_json(QUERY_URL, query, referer=REFERER)
    except ValueError as exc:
        raise NeedsHuman(
            "上海标准接口未返回有效 JSON",
            hint="检查上海住建委查询响应；不要把解析失败当作无命中",
        ) from exc


def search(
    ref: str,
    *,
    page_size: int = 30,
    abolished: bool = False,
    standard_type: int = 0,
) -> list[dict]:
    """按编号或名称检索地方标准（沿用单页及四栏参数）。"""
    return core.search(
        ref, fetch=_fetch, page_size=page_size, abolished=abolished, standard_type=standard_type
    )


def find(ref: str) -> list[dict]:
    """沿用旧检索接口；在调用时注入 search，兼容调用方的 mock/覆写。"""
    return core.find(ref, searcher=search)


def download_to(file_url: str, part_path: str, referer: str = REFERER) -> tuple[int, str, int]:
    """流式下载（支持断点续传）。令牌按当前时间现算，所以归档的 URL 不会带过期令牌。"""
    fid = file_id_of(file_url)
    ts = int(time.time() * 1000)
    signed = f"{BASE}api/files/webDownload?" + urllib.parse.urlencode(
        {"fileId": fid, "timestamp": ts, "receivedToken": _token(fid, ts)}
    )
    info = compliance.download(signed, part_path, referer=referer, timeout=180)
    with open(part_path, "rb") as fp:
        head = fp.read(64)
    magic = magic_of(head)
    if magic != MAGIC_PDF:
        Path(part_path).unlink(missing_ok=True)
        raise BadAttachment("上海标准接口返回的不是 PDF（可能是错误页或文件已撤下）")
    return int(info["bytes"]), magic, int(info["resumed_from"])


def download(file_url: str, referer: str = REFERER) -> tuple[bytes, str]:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".part", delete=False) as fp:
        tmp = fp.name
    try:
        _n, magic, _r = download_to(file_url, tmp, referer)
        return Path(tmp).read_bytes(), magic
    finally:
        Path(tmp).unlink(missing_ok=True)


def catalog_page(page_no: int, page_size: int = 100) -> tuple[list[dict], int]:
    payload = _fetch(
        {
            "pageNum": page_no,
            "pageSize": page_size,
            "isAbolish": False,
            "standardType": 0,
            "condition": "",
            "publishStartDate": "",
            "publishEndDate": "",
            "implementStartDate": "",
            "implementEndDate": "",
        },
    )
    records, total = core.parse_page(payload)
    items = []
    for rec in records:
        items.append(
            {
                "date": rec["published_on"],
                "url": rec["page_url"],
                "title": rec["name"] or rec["code"],
            }
        )
    return items, total


class Shzjw:
    id = "shzjw"
    display_name = "上海市工程建设标准"
    api_mode = True     # pull 走"检索 + 直连下载"，没有公告页可抓

    catalog_page = staticmethod(catalog_page)
    find = staticmethod(find)
    search = staticmethod(search)
    download = staticmethod(download)
    download_to = staticmethod(download_to)


SOURCE = Shzjw()
