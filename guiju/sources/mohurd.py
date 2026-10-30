"""住建部适配器。

这是全套里唯一"脆弱"的部分 —— 站点改版就会失效。所以所有站点相关的
魔术值都集中在本文件顶部，改版时只改这里，不碰其它模块。

已踩过并记录在案的三个坑：
  1. 旧链接 /gongkai/zhengce/zhengcefilelib/YYYYMM/YYYYMMDD_ID.html 已 100% 失效，
     需映射为 /gongkai/zc/wjk/art/YYYY/art_17339_ID.html
  2. 附件常常有两个，第一个是「废止的强制性条文」(.doc)，第二个才是规范本身
  3. 列表接口的分页参数必须嵌在 paramJson 里，且 queryData 六项一个不能多、
     一个不能少（多传 editType 会导致返回空 data）
"""

from __future__ import annotations

import html as htmllib
import json
import re
import urllib.parse

from .. import compliance

BASE = "https://www.mohurd.gov.cn"
LIST_PAGE = BASE + "/gongkai/fdzdgknr/bzgg/index.html"
UNIT_API = BASE + "/api-gateway/jpaas-publish-server/front/page/build/unit"

#: 列表组件的固定参数（取自页面 .pagination 的 queryData 属性）
QUERY_DATA = {
    "webId": "86ca573ec4df405db627fdc2493677f3",
    "pageId": "Y53uSscFTKvrbHxaF9Cvp",
    "parseType": "bulidstatic",
    "pageType": "column",
    "tagId": "栏目-list",
    "tplSetId": "fc259c381af3496d85e61997ea7771cb",
}

ITEM_RE = re.compile(
    r'<a[^>]+href="(/gongkai/zc/wjk/art/[^"]+)"[^>]*title="([^"]*)"', re.S
)
DATE_INFO_RE = re.compile(r'<span class="date-info">\s*([0-9]{4}-[0-9]{2}-[0-9]{2})')
ATTACH_RE = re.compile(
    r'<a[^>]+href="([^"]*(?:api-gateway|\.pdf|\.docx?|\.zip)[^"]*)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
CODE_RE = re.compile(
    r"(GB/T|GB|JGJ/T|JGJ|CJJ/T|CJJ|CJ/T|DG/TJ|DGJ|DB\d{2}/T|T/CECS)"
    r"\s*(\d{2,6})\s*[-—–－]\s*(\d{4})"
)


class AdapterError(RuntimeError):
    pass


# ---------------------------------------------------------------- catalog


def fetch_catalog_page(page_no: int, page_size: int = 20) -> tuple[list[dict], int]:
    """取一页目录。返回 (条目列表, 总条数)。"""
    params = dict(QUERY_DATA)
    params["paramJson"] = json.dumps({"pageNo": page_no, "pageSize": page_size})
    url = UNIT_API + "?" + urllib.parse.urlencode(params)
    body, _ = compliance.get(url, referer=LIST_PAGE)
    try:
        payload = json.loads(body.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        raise AdapterError("列表接口返回的不是 JSON，站点结构可能已改版") from exc
    if not payload.get("success"):
        raise AdapterError(f"列表接口报错：{payload.get('message') or '空响应'}")

    fragment = payload.get("data", {}).get("html", "")
    if not fragment:
        return [], 0

    total = 0
    m = re.search(r'count="(\d+)"', fragment)
    if m:
        total = int(m.group(1))

    items: list[dict] = []
    # 每条记录形如：
    #   <li class="date">
    #     <a class="fl" href="..." title="标题">标题</a>
    #     <span class="date-info">2026-09-23</span>
    #   </li>
    for block in re.split(r'<li class="date">', fragment)[1:]:
        am = ITEM_RE.search(block)
        dm = DATE_INFO_RE.search(block)
        if not (am and dm):
            continue
        items.append(
            {
                "date": dm.group(1),
                "url": BASE + am.group(1),
                "title": htmllib.unescape(am.group(2)).strip(),
            }
        )
    return items, total


# ---------------------------------------------------------------- detail


def fetch_detail(url: str) -> str:
    body, _ = compliance.get(url, referer=LIST_PAGE)
    return body.decode("utf-8", "ignore")


def extract_codes(page_html: str) -> list[str]:
    """从详情页正文提取标准编号。返回展示用字符串（含空格），已去重保序。"""
    seen: dict[str, str] = {}
    for prefix, num, year in CODE_RE.findall(page_html):
        prefix = prefix.upper().replace(" ", "")
        display = f"{prefix} {num}-{year}"
        seen.setdefault(_key(display), display)
    return list(seen.values())


def _key(display_code: str) -> str:
    return display_code.replace(" ", "").upper()


def normalize_key(user_input: str) -> str:
    """把用户输入归一化成 codes.key 的形式。"""
    s = user_input.strip().upper().replace("－", "-").replace("—", "-")
    s = re.sub(r"\s+", "", s)
    return s


def attachments(page_html: str) -> list[dict]:
    """列出详情页的所有附件。"""
    out: list[dict] = []
    for href, inner in ATTACH_RE.findall(page_html):
        label = re.sub(r"<[^>]+>", "", inner)
        label = htmllib.unescape(label).strip()
        url = urllib.parse.urljoin(BASE, href.replace("&amp;", "&"))
        out.append({"url": url, "label": label})
    # 去重（同 URL 可能出现多次）
    uniq: dict[str, dict] = {}
    for att in out:
        uniq.setdefault(att["url"], att)
    return list(uniq.values())


DEPRECATED = ("废止", "作废", "条文废止")


def pick_attachment(atts: list[dict], name: str = "") -> dict | None:
    """挑出真正的规范正文。

    策略（按优先级）：
      1. 标签里带规范名 且 非"废止"
      2. 非"废止"的 PDF
      3. 非"废止"的任意附件
    """
    if not atts:
        return None

    def is_pdf(a: dict) -> bool:
        return ".pdf" in a["url"].lower() or ".pdf" in a["label"].lower()

    def is_deprecated(a: dict) -> bool:
        return any(word in a["label"] for word in DEPRECATED)

    alive = [a for a in atts if not is_deprecated(a)]

    core = name.replace("《", "").replace("》", "")[:8]
    if core:
        for a in alive:
            if core in a["label"] and is_pdf(a):
                return a
        for a in alive:
            if core in a["label"]:
                return a
    for a in alive:
        if is_pdf(a):
            return a
    return alive[0] if alive else None


# ---------------------------------------------------------------- download


def download(att_url: str, referer: str) -> bytes:
    body, ctype = compliance.get(att_url, referer=referer, timeout=180)
    if body.startswith(b"%PDF"):
        return body
    lowered = (ctype or "").lower()
    if "pdf" in lowered and body.startswith(b"%PDF"):
        return body
    head = body[:400].decode("utf-8", "ignore")
    if "<html" in head.lower() or "<!doctype" in head.lower():
        raise AdapterError(
            f"附件返回的是 HTML 而非 PDF（{len(body)} 字节），可能是错误的附件或需要重新解析详情页"
        )
    raise AdapterError(f"附件不是 PDF（Content-Type={ctype}, {len(body)} 字节）")


def migrate_legacy_url(old_url: str) -> str | None:
    """旧版 zhengcefilelib 链接 → 现行详情页。已失效的长尾靠这条兜底。"""
    m = re.match(
        r".*/zhengcefilelib/(\d{4})\d{2}/\d{8}_(\d+)\.html", old_url
    )
    if not m:
        return None
    return f"{BASE}/gongkai/zc/wjk/art/{m.group(1)}/art_17339_{m.group(2)}.html"
