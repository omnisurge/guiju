"""住建部适配器 —— 全仓唯一"脆弱"的部分。

站点相关魔术值全部集中在本文件顶部：改版时只改这里，不碰其它模块。

已踩过的坑（勿重犯）：
  1. 旧链 /gongkai/zhengce/zhengcefilelib/YYYYMM/YYYYMMDD_ID.html 已 100% 失效
     → 映射为 /gongkai/zc/wjk/art/YYYY/art_17339_ID.html
  2. 附件常有两个，第一个常是「废止的强制性条文」(.doc)，第二个才是规范
  3. 列表分页参数必须嵌进 paramJson 的 JSON 字符串；queryData 六项不多不少
  4. UA 必须纯 ASCII（HTTP 头 latin-1），写中文会 UnicodeEncodeError
  5. 缺 Referer 会被 403
  6. 单页渲染上限 99 条（实测 pageSize=100 返回 99），但总量 3254 条无缺失 ——
     33 页全量拉完与历史目录 CSV 双向零差异，所以 pageSize=100 可以安全使用
"""

from __future__ import annotations

import html as htmllib
import json
import re
import urllib.parse
from pathlib import Path

from .. import compliance
from ..domain.announcement import classify_title
from ..domain.attachment import Attachment
from ..domain.codes import html_to_text
from ..errors import BadAttachment, NeedsHuman

BASE = "https://www.mohurd.gov.cn"
LIST_PAGE = f"{BASE}/gongkai/fdzdgknr/bzgg/index.html"
UNIT_API = f"{BASE}/api-gateway/jpaas-publish-server/front/page/build/unit"

#: 列表组件固定参数（取自页面 .pagination 的 queryData 属性）
QUERY_DATA = {
    "webId": "86ca573ec4df405db627fdc2493677f3",
    "pageId": "Y53uSscFTKvrbHxaF9Cvp",
    "parseType": "bulidstatic",
    "pageType": "column",
    "tagId": "栏目-list",
    "tplSetId": "fc259c381af3496d85e61997ea7771cb",
}

_ITEM_RE = re.compile(r'<a[^>]+href="(/gongkai/zc/wjk/art/[^"]+)"[^>]*title="([^"]*)"', re.S)
_DATE_RE = re.compile(r'<span class="date-info">\s*([0-9]{4}-[0-9]{2}-[0-9]{2})')
_ATTACH_RE = re.compile(
    r'<a[^>]+href="([^"]*(?:api-gateway|\.pdf|\.docx?|\.zip)[^"]*)"[^>]*>(.*?)</a>', re.S | re.I
)
_COUNT_RE = re.compile(r'count="(\d+)"')
# 公告标题的规范名/分类判定统一在 domain.announcement：此前这里复制了一份
# _CATEGORY_RULES + classify_title，两份独立演进曾把分类结论改岔。

MAGIC_PDF = "pdf"
MAGIC_DOC = "doc"
MAGIC_ZIP = "zip"
MAGIC_HTML = "html"
MAGIC_UNKNOWN = "unknown"


def catalog_page(page_no: int, page_size: int = 100) -> tuple[list[dict], int]:
    """取一页目录。返回 (条目, 总数)。"""
    params = dict(QUERY_DATA)
    params["paramJson"] = json.dumps({"pageNo": page_no, "pageSize": page_size})
    resp = compliance.get(f"{UNIT_API}?{urllib.parse.urlencode(params)}", referer=LIST_PAGE)
    try:
        payload = json.loads(resp.text)
    except json.JSONDecodeError as exc:
        raise NeedsHuman("列表接口返回的不是 JSON，站点结构可能已改版") from exc
    if not payload.get("success"):
        raise NeedsHuman(f"列表接口报错：{payload.get('message') or '空响应'}")

    fragment = (payload.get("data") or {}).get("html") or ""
    if not fragment:
        return [], 0

    total = int(_COUNT_RE.search(fragment).group(1)) if _COUNT_RE.search(fragment) else 0
    items: list[dict] = []
    for block in re.split(r'<li class="date">', fragment)[1:]:
        link = _ITEM_RE.search(block)
        date = _DATE_RE.search(block)
        if not (link and date):
            continue
        items.append(
            {
                "date": date.group(1),
                "url": BASE + link.group(1),
                "title": htmllib.unescape(link.group(2)).strip(),
            }
        )
    return items, total


def detail(url: str) -> tuple[str, str]:
    """取详情页，返回 (HTML, 纯文本)。"""
    resp = compliance.get(url, referer=LIST_PAGE)
    html = resp.text
    return html, html_to_text(html)


def attachments(html: str) -> list[Attachment]:
    """列出详情页附件（按 URL 去重，保序）。"""
    seen: dict[str, Attachment] = {}
    for href, inner in _ATTACH_RE.findall(html):
        label = htmllib.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        url = urllib.parse.urljoin(BASE, href.replace("&amp;", "&"))
        seen.setdefault(url, Attachment(url=url, label=label))
    return list(seen.values())


def magic_of(body: bytes) -> str:
    if body.startswith(b"%PDF"):
        return MAGIC_PDF
    if body.startswith(b"D0CF11E0"):
        return MAGIC_DOC
    if body.startswith(b"PK\x03\x04"):
        return MAGIC_ZIP
    head = body[:512].lower()
    if b"<html" in head or b"<!doctype" in head:
        return MAGIC_HTML
    return MAGIC_UNKNOWN


def download(file_url: str, referer: str) -> tuple[bytes, str]:
    """下载附件并做魔数判定。非 PDF 一律报错，绝不落盘。"""
    resp = compliance.get(file_url, referer=referer, timeout=180)
    magic = magic_of(resp.body)
    if magic == MAGIC_PDF:
        return resp.body, magic
    detail_msg = {
        MAGIC_DOC: "附件是 .doc（多为「废止的强制性条文」，不是规范正文）",
        MAGIC_ZIP: "附件是压缩包，需解压后人工确认内容",
        MAGIC_HTML: "附件返回 HTML 错误页，可能链接失效或需重新解析详情页",
    }.get(magic, f"附件不是 PDF（Content-Type={resp.content_type}, {len(resp.body)} 字节）")
    raise BadAttachment(detail_msg)


def download_to(file_url: str, part_path: str, referer: str) -> tuple[int, str, int]:
    """流式下载到 `part_path`（断点续传），返回 (bytes, magic, resumed_from)。"""
    info = compliance.download(file_url, part_path, referer=referer, timeout=180)
    with open(part_path, "rb") as fp:
        head = fp.read(64)
    magic = magic_of(head)
    if magic == MAGIC_PDF:
        return int(info["bytes"]), magic, int(info["resumed_from"])

    Path(part_path).unlink(missing_ok=True)   # 错误页不能当续传底子，否则永远拼不出 PDF
    detail_msg = {
        MAGIC_DOC: "附件是 .doc（多为「废止的强制性条文」，不是规范正文）",
        MAGIC_ZIP: "附件是压缩包，需解压后人工确认内容",
        MAGIC_HTML: "附件返回 HTML 错误页，可能链接失效或需重新解析详情页",
    }.get(magic, f"附件不是 PDF（{info['bytes']} 字节）")
    raise BadAttachment(detail_msg)


def migrate_legacy_url(old_url: str) -> str | None:
    """旧版 zhengcefilelib 链接 → 现行详情页。"""
    m = re.match(r".*/zhengcefilelib/(\d{4})\d{2}/\d{8}_(\d+)\.html", old_url)
    if not m:
        return None
    return f"{BASE}/gongkai/zc/wjk/art/{m.group(1)}/art_17339_{m.group(2)}.html"


class Mohurd:
    id = "mohurd"
    display_name = "住房和城乡建设部"

    catalog_page = staticmethod(catalog_page)
    detail = staticmethod(detail)
    attachments = staticmethod(attachments)
    download = staticmethod(download)
    download_to = staticmethod(download_to)


SOURCE = Mohurd()
