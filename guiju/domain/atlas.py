"""工程建设标准图集（国标电子书库）纯领域逻辑：编号判定、防串号匹配与 HTML 解析。

本模块严守纯领域边界：零网络 I/O、零文件落盘、零标准库传输导入。
图集号年份在前（如 22G101-1、粤25G/T116），与 guiju/currency/codes.py（年份在尾）
完全正交，因此单独成模块，严防把 B16G101-1 双语版或 23G101-11 混成正本。
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass

from ..errors import NeedsHuman

BASE_URL = "https://ebook.chinabuilding.com.cn"
SEARCH_URL = f"{BASE_URL}/zbooklib/search"
DETAIL_URL = f"{BASE_URL}/zbooklib/book/detail/show"
CONCISE_CATALOGUE_URL = (
    f"{BASE_URL}/zbooklib/conciseCatalogueList?SiteID=1&sublibID=2118&currentSublibInnerCode=0029"
)
SEARCH_PAGE_SIZE = 20

_DASHES = str.maketrans({"－": "-", "—": "-", "–": "-", "−": "-", "／": "/", "（": "(", "）": ")", "～": "~"})
_TAG_RE = re.compile(r"<[^>]+>")
_FULLWIDTH_OFFSET = 0xFEE0

_SHANGHAI_PREFIXES = ("DG/TJ08", "DGJ08", "DBJT08", "DBTJ08", "DBJ08", "DB31")
_STANDARD_EXCLUDED_PREFIXES = frozenset({
    "GB", "GJB", "JGJ", "CJJ", "JTG", "YB", "JC", "JG", "YD", "DL", "SL", "SY",
    "HJ", "AQ", "GA", "NY", "LY", "YY", "MH", "MT", "QB", "SH", "HG", "WB", "TB",
    "QX", "WH", "CY", "CH", "DZ", "ISO", "IEC", "ASTM", "CJ", "JT", "DB", "DG", "DBJ",
})
_STANDARD_ONLY_MULTI = frozenset({"GB", "GJB", "JGJ", "CJJ", "JTG"})
_STANDARD_YEAR_TAIL = re.compile(r"-(?:19|20)\d{2}$")

_PROVINCE_TOKENS = (
    "中南", "西南", "西北", "华北", "华东", "东北", "长三角",
    "京", "津", "沪", "渝", "冀", "晋", "蒙", "辽", "吉", "黑", "苏", "浙", "皖", "闽", "赣",
    "鲁", "豫", "鄂", "湘", "粤", "桂", "琼", "川", "蜀", "贵", "黔", "云", "滇", "藏", "陕",
    "秦", "甘", "陇", "青", "宁", "新", "台", "港", "澳", "协", "院", "长", "西",
)
_PROV_PATTERN = "|".join(sorted(_PROVINCE_TOKENS, key=len, reverse=True))

_LANG_SUFFIX_RE = re.compile(r"(?:英文版|中文版)$")
_ATLAS_TAIL = (
    r"(?:/[A-Z]-?|/|-)?[0-9Ⅰ-Ⅻ](?:[\dA-ZⅠ-Ⅻ\-/~]|\([^()]+\))*"
    r"(?:附[\u4e00-\u9fff0-9A-Z\-~Ⅰ-Ⅻ()]*)?改?(?:英文版|中文版)?"
)
_BILINGUAL_RE = re.compile(
    rf"^B\d{{2}}(?:\(\d{{2}}\))?[A-Z]{{1,5}}{_ATLAS_TAIL}$"
)
_YEAR_PREFIX_ATLAS_RE = re.compile(
    rf"^\d{{2}}(?:\(\d{{2}}\))?-?[A-Z]{{1,5}}{_ATLAS_TAIL}$"
)
_YEAR4_PREFIX_ATLAS_RE = re.compile(
    rf"^(?:19|20)\d{{2}}[A-Z]{{1,6}}{_ATLAS_TAIL}$"
)
_YEAR4_PROV_ATLAS_RE = re.compile(
    rf"^(?:\d{{2}}|(?:19|20)\d{{2}})(?:{_PROV_PATTERN}){{1,2}}[-/]?[A-Z]{{1,5}}{_ATLAS_TAIL}$"
)
_PROV_BODY = (
    r"(?:T-[A-Z]-?\d[\dA-Z\-~()]*"
    rf"|[A-Z]{{1,5}}(?:{_ATLAS_TAIL}|-[A-Z]{{1,4}}(?:\([A-Z]+\))?|合订本(?:\([^()]+\))?))"
)
_PROV_ATLAS_RE = re.compile(
    rf"^(?:{_PROV_PATTERN})(?:\d{{2}}|(?:19|20)\d{{2}})?(?:{_PROV_PATTERN})?[-/]?{_PROV_BODY}$"
)
_PROV_HAS_YEAR_RE = re.compile(
    rf"^(?:(?:\d{{2}}|(?:19|20)\d{{2}})(?:{_PROV_PATTERN})|(?:{_PROV_PATTERN})(?:\d{{2}}|(?:19|20)\d{{2}})|"
    rf"(?:{_PROV_PATTERN})[A-Z]{{1,5}}(?:(?:19|20)\d{{2}}-.*|.*(?:-(?:19|20)\d{{2}}|\((?:19|20)\d{{2}}年版?\))))$"
)
_LETTER_PROV_YEAR_RE = re.compile(
    rf"^(?:(?:L|J|M|SDCT|XB)\d{{2}}-?[A-Z]{{1,5}}{_ATLAS_TAIL}"
    rf"|L\d{{2}}[JGSND](?:\([^()]+\))?"
    rf"|SX-\d{{2}}[A-Z]{{1,3}}\d[\dA-Z\-]*)$"
)
_PROV_STRIP_YEAR_RE = re.compile(
    rf"^({_PROV_PATTERN})(?:\d{{2}}|(?:19|20)\d{{2}})[-/]?((?:{_PROV_PATTERN})?-?[A-Z].*)$"
)
_YEAR4_PROV_STRIP_RE = re.compile(
    rf"^(?:\d{{2}}|(?:19|20)\d{{2}})((?:{_PROV_PATTERN}).*)$"
)
_LETTER_PROV_STRIP_RE = re.compile(r"^(L|J|M|SDCT|XB|SX-)\d{2}-?([A-Z].*)$")
_DBJ_ATLAS_RE = re.compile(
    rf"^DBJ(?:T|/T|/Z)?(?!08)\d{{2}}-\d+-\d{{4}}"
    rf"(?:-\d{{2}}[A-Z]{{1,5}}{_ATLAS_TAIL}|[A-Z]{{2,6}}\d*)$"
)
_YEARLESS_DISCIPLINE_PREFIXES = (
    # 单字母建标/图集专业类代号（避免把 M1、CECS1、XX999 等非图集误判为无年份图集号）
    "J", "G", "S", "K", "D", "X", "R", "L", "F", "M", "C", "N", "H", "Q", "T",
    # 国标/中南/人防/省市图集常见双三字母专题代号
    "GJ", "SG", "SS", "SJ", "DX", "FD", "FJ", "FG", "FK", "FS", "FZ", "RF", "MR",
    "CS", "CD", "CG", "MS", "MD", "MJ", "MG", "MK", "ZJ", "ZG", "ZS", "ZD", "ZK",
    "ZR", "ZX", "ZN", "TJ", "TG", "TS", "TD", "TK", "TX", "TR", "BJ", "BG", "BS",
    "BD", "BK", "BR", "BX", "DQ", "DJ", "JS", "GT", "GX", "SZ", "SSB",
    "EJ", "QG", "LJ", "QJ", "XA", "LS", "GG",
)
_YEARLESS_PREFIX_PATTERN = "|".join(sorted(_YEARLESS_DISCIPLINE_PREFIXES, key=len, reverse=True))
_SINGLE_DIGIT_ATLAS_RE = re.compile(
    r"^(?:(?:S|K|R|MR)\d(?:/\d+)?(?:-\d+)?(?:\([^()]+\))*(?:附[\u4e00-\u9fff0-9A-Z\-~Ⅰ-Ⅻ()]*)?改?|XBD-\d+-\d{2,4}|XBD-\d+-\(\d{2}\))$"
)
_BASE_OR_BINDING_RE = re.compile(
    rf"^(?:{_YEARLESS_PREFIX_PATTERN})"
    r"(?:\d{2,5}(?:-\d{1,4})?(?:/\d{1,4})?(?:~[A-Z]{0,4}\d{1,5}(?:-\d{1,4})?(?:[A-Z]{1,4}\d{1,5}(?:-\d{1,4})?~\d{1,4})?)?"
    r"|\d(?:-\d{1,4})?~(?:[A-Z]{0,4}\d{1,5}(?:-\d{1,4})?))"
    r"(?:\([^()]+\))*(?:附[\u4e00-\u9fff0-9A-Z\-~Ⅰ-Ⅻ()]*)?改?$"
)
_ATLAS_CONT_RE = re.compile(r"^-?\d{1,3}(?:~\d{1,3})?(?:\([^()]+\))?$")

_STATUS_MAP = {
    "现行": "current",
    "即将实施": "upcoming",
    "废止": "withdrawn",
    "作废": "withdrawn",
}


@dataclass(frozen=True)
class AtlasSearchItem:
    book_id: str
    code: str
    name: str
    status: str
    publish_date: str
    implement_date: str
    abolish_date: str
    url: str


@dataclass(frozen=True)
class AtlasSearchPage:
    items: tuple[AtlasSearchItem, ...]
    total: int
    empty_authoritative: bool
    has_more: bool
    page_size: int = SEARCH_PAGE_SIZE


@dataclass(frozen=True)
class AtlasDetail:
    book_id: str
    code: str
    name: str
    status: str
    resource_type: str
    publish_date: str
    implement_date: str
    abolish_date: str
    replaces: str
    replaced_by: str
    editor: str
    url: str


@dataclass(frozen=True)
class AtlasCatalogEntry:
    book_id: str
    code: str
    code_key: str
    name: str
    status: str
    replaces: str
    replaced_by: str
    url: str


def _error(detail: str) -> NeedsHuman:
    return NeedsHuman(
        f"国标电子书库图集页面结构异常：{detail}",
        hint="国标电子书库页面结构可能已改版，请携带本次错误输出上报维护者",
    )


def detail_url(book_id: str) -> str:
    return f"{DETAIL_URL}?SiteID=1&bookID={urllib.parse.quote(str(book_id).strip())}"


def search_url(query: str, *, page: int = 1, field_type: str = "") -> str:
    params: dict[str, str] = {
        "SiteID": "1",
        "contentType": "book",
        "SearchKey": query.strip(),
    }
    if field_type:
        params["fieldType"] = field_type
    if page > 1:
        params["PageIndex"] = str(page)
    return f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"


def _has_control(text: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") and not ch.isspace() for ch in text)


def _to_halfwidth_ascii(text: str) -> str:
    chars = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - _FULLWIDTH_OFFSET))
        else:
            chars.append(ch)
    return "".join(chars)


def normalize_code(code: str) -> str:
    """图集号归一化：转半角/ASCII大写、统一横杠波浪号、去空白，保留 B 前缀与语言后缀。"""
    if not isinstance(code, str) or not code.strip() or _has_control(code):
        return ""
    half = _to_halfwidth_ascii(code.strip()).translate(_DASHES)
    return "".join(half.split()).upper()


def _starts_with_shanghai(norm: str) -> bool:
    return norm.startswith(_SHANGHAI_PREFIXES)


def _looks_like_standard(norm: str) -> bool:
    if norm.startswith(("T/", "Q/")):
        return True
    if _starts_with_shanghai(norm):
        return True
    if _DBJ_ATLAS_RE.match(norm):
        return False
    if re.match(r"^DB(?:\d{2}|J)", norm):
        return True
    m_head = re.match(r"^([A-Z]+)(/[TZ])?(?=[-0-9]|$)", norm)
    if not m_head:
        return False
    prefix, slash_mod = m_head.group(1), m_head.group(2)
    if prefix in _STANDARD_ONLY_MULTI:
        return True
    if prefix in _STANDARD_EXCLUDED_PREFIXES:
        if slash_mod or _STANDARD_YEAR_TAIL.search(norm):
            return True
        if prefix in {"CJ", "JT", "YB", "JC", "JG", "YD", "DL", "SL", "SY", "HJ", "AQ", "GA",
                      "NY", "LY", "YY", "MH", "MT", "QB", "SH", "HG", "WB", "TB", "QX", "WH",
                      "CY", "CH", "DZ", "ISO", "IEC", "ASTM"}:
            rest = norm[len(prefix):].lstrip("-")
            if rest and ("0" <= rest[0] <= "9" or (prefix in {"JC", "DL", "SL", "YB"} and len(rest) >= 2)):
                return True
    if _LETTER_PROV_YEAR_RE.match(norm):
        return False
    return bool(_STANDARD_YEAR_TAIL.search(norm))


def _is_single_atlas_code(norm: str) -> bool:
    if not norm or norm.isdigit() or norm.startswith("HTTP"):
        return False
    if re.match(r"^\d{4}[-/]", norm):
        return False
    if _looks_like_standard(norm):
        return False
    return bool(
        _DBJ_ATLAS_RE.match(norm)
        or _BILINGUAL_RE.match(norm)
        or _YEAR_PREFIX_ATLAS_RE.match(norm)
        or _YEAR4_PREFIX_ATLAS_RE.match(norm)
        or _YEAR4_PROV_ATLAS_RE.match(norm)
        or _PROV_ATLAS_RE.match(norm)
        or _LETTER_PROV_YEAR_RE.match(norm)
        or _SINGLE_DIGIT_ATLAS_RE.match(norm)
        or _BASE_OR_BINDING_RE.match(norm)
    )


def is_atlas_code(code: str) -> bool:
    """判定是否为工程建设标准图集编号（严格互斥上海地标、国行标与中文书名）。"""
    norm = normalize_code(code)
    if not norm:
        return False
    if "、" in norm:
        parts = norm.split("、")
        if len(parts) < 2 or any(not p for p in parts):
            return False
        if not _is_single_atlas_code(parts[0]):
            return False
        return all(_is_single_atlas_code(p) or bool(_ATLAS_CONT_RE.match(p)) for p in parts[1:])
    return _is_single_atlas_code(norm)


def _is_bilingual(norm: str) -> bool:
    return bool(_BILINGUAL_RE.match(norm))


def _has_lang_suffix(norm: str) -> bool:
    return bool(_LANG_SUFFIX_RE.search(norm))


def has_edition_year(code: str) -> bool:
    """图集号是否明确指定了版本/年份/双语/合订本（无年份基础号返回 False）。"""
    norm = normalize_code(code)
    if not is_atlas_code(norm):
        return False
    if "、" in norm:
        return has_edition_year(norm.split("、", 1)[0])
    if _is_bilingual(norm) or _has_lang_suffix(norm):
        return True
    if "~" in norm or _DBJ_ATLAS_RE.match(norm):
        return True
    if re.search(r"(?:\((?:19|20)\d{2}|-\(\d{2}\)$)", norm):
        return True
    return bool(
        _YEAR_PREFIX_ATLAS_RE.match(norm)
        or _YEAR4_PREFIX_ATLAS_RE.match(norm)
        or _YEAR4_PROV_ATLAS_RE.match(norm)
        or _LETTER_PROV_YEAR_RE.match(norm)
        or _PROV_HAS_YEAR_RE.match(norm)
    )


def atlas_family(code: str) -> str:
    """提取同族基号（去前导年份；保留省/字母区域前缀，双语/英文版原样保留）。"""
    norm = normalize_code(code)
    if not norm or "、" in norm or _is_bilingual(norm) or _has_lang_suffix(norm):
        return norm
    m_y4_prov = _YEAR4_PROV_STRIP_RE.match(norm)
    if m_y4_prov:
        return m_y4_prov.group(1)
    m_prov = _PROV_STRIP_YEAR_RE.match(norm)
    if m_prov:
        prov, rest = m_prov.group(1), m_prov.group(2).lstrip("-")
        return rest if rest.startswith(prov) else f"{prov}{rest}"
    m_lprov = _LETTER_PROV_STRIP_RE.match(norm)
    if m_lprov:
        return f"{m_lprov.group(1)}{m_lprov.group(2)}"
    m_yr = re.match(r"^(?:\d{2}(?:\(\d{2}\))?|(?:19|20)\d{2})-?([A-Z].*)$", norm)
    if m_yr:
        return m_yr.group(1)
    return norm


def matches(query: str, candidate: str) -> bool:
    """严格图集号匹配：防 B 双语/英文版/兄弟子册（23G101-11）串号。"""
    want = normalize_code(query)
    got = normalize_code(candidate)
    if not want or not got:
        return False
    if not is_atlas_code(want) or not is_atlas_code(got):
        return False
    if want == got:
        return True
    if has_edition_year(want):
        return False
    if _is_bilingual(got) or _has_lang_suffix(got):
        return False
    fam = atlas_family(got)
    if fam == want:
        return True
    return "-" not in want and fam.startswith(want + "-")


def _plain_inline(raw_html: str) -> str:
    text = html.unescape(_TAG_RE.sub("", raw_html))
    return re.sub(r"\s+", " ", text).strip()


def _clean_date(raw: str) -> str:
    val = _plain_inline(raw)
    if not val or val == "-":
        return ""
    m = re.search(r"\d{4}-\d{2}-\d{2}", val)
    return m.group(0) if m else val


def _extract_var_data(html_text: str) -> dict:
    m = re.search(r"\bvar\s+data\s*=\s*(?=\{)", html_text)
    if not m:
        raise _error("搜索页缺少 var data 统计对象")
    try:
        obj, _ = json.JSONDecoder().raw_decode(html_text, m.end())
    except ValueError as exc:
        raise _error(f"var data JSON 无法解析：{exc}") from exc
    if not isinstance(obj, dict):
        raise _error("var data 必须是 JSON object")
    return obj


def _extract_search_total(data: dict) -> int:
    for key in ("zylxInnercode", "abolish"):
        facets = data.get(key)
        if isinstance(facets, list) and facets:
            preferred = next((x for x in facets if isinstance(x, dict) and x.get("value") == ""), facets[0])
            if isinstance(preferred, dict) and type(preferred.get("count")) is int and preferred["count"] >= 0:
                return preferred["count"]
    raise _error("var data 缺少有效的总数统计 count")


def _extract_book_id(href: str) -> str:
    m = re.search(r"[?&]bookID=(\d+)", html.unescape(href))
    return m.group(1) if m else ""


def _split_code_name(text: str) -> tuple[str, str]:
    for sep in ("：", ":"):
        if sep in text:
            left, right = text.split(sep, 1)
            return re.sub(r"\s+", " ", left).strip(), re.sub(r"\s+", " ", right).strip()
    return re.sub(r"\s+", " ", text).strip(), ""


def _normalize_status(raw_status: str, replaced_by: str = "") -> str:
    mapped = _STATUS_MAP.get(raw_status.strip(), "unknown")
    if mapped == "withdrawn" and replaced_by.strip():
        return "superseded"
    return mapped


def parse_search_page(html_text: str, *, page: int = 1) -> AtlasSearchPage:
    """解析 /zbooklib/search 结果页；任何结构缺损一律抛 NeedsHuman(50)。"""
    if not isinstance(html_text, str) or not html_text.strip():
        raise _error("搜索页响应为空")
    if type(page) is not int or page <= 0:
        raise _error(f"页码必须是正整数：{page!r}")

    data = _extract_var_data(html_text)
    h4_matches = list(
        re.finditer(r'(?is)<h4[^>]*class="[^"]*search-tit[^"]*"[^>]*>(.*?)</h4>', html_text)
    )
    # 左侧分类栏也可能带 h4.search-tit（不含 bookID=）；真正结果标题一定含 <a ... bookID=...
    card_headers = [m for m in h4_matches if "bookID=" in m.group(1)]

    if data.get("showStat") == "false":
        if card_headers:
            raise _error("showStat=false 与存在结果卡片矛盾")
        return AtlasSearchPage(items=(), total=0, empty_authoritative=True, has_more=False)

    total = _extract_search_total(data)
    page_ex = html_text.find('id="pageEx"')
    items: list[AtlasSearchItem] = []

    for idx, hm in enumerate(card_headers):
        end = card_headers[idx + 1].start() if idx + 1 < len(card_headers) else (
            page_ex if page_ex > hm.end() else len(html_text)
        )
        block = html_text[hm.end():end]
        header_html = hm.group(1)

        m_link = re.search(r'(?is)<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', header_html)
        if not m_link:
            raise _error("结果卡片缺少详情链接")
        book_id = _extract_book_id(m_link.group(1))
        if not book_id:
            raise _error("结果卡片链接缺少 bookID")
        code, name = _split_code_name(_plain_inline(m_link.group(2)))
        if not code:
            raise _error(f"bookID={book_id} 卡片缺少图集编号")

        m_stat = re.search(r'(?is)<span[^>]*class="[^"]*(?:active|abolish|unenforced)[^"]*"[^>]*>([^<]+)</span>', header_html)
        if not m_stat:
            raise _error(f"bookID={book_id} 卡片缺少现行/废止状态标记")
        status = _normalize_status(_plain_inline(m_stat.group(1)))

        m_pub = re.search(r"发布日期[：:]\s*([^<\r\n]*)", block)
        m_imp = re.search(r"实施日期[：:]\s*([^<\r\n]*)", block)
        m_abo = re.search(r"废止日期[：:]\s*([^<\r\n]*)", block)

        items.append(
            AtlasSearchItem(
                book_id=book_id,
                code=code,
                name=name,
                status=status,
                publish_date=_clean_date(m_pub.group(1)) if m_pub else "",
                implement_date=_clean_date(m_imp.group(1)) if m_imp else "",
                abolish_date=_clean_date(m_abo.group(1)) if m_abo else "",
                url=detail_url(book_id),
            )
        )

    offset = (page - 1) * SEARCH_PAGE_SIZE
    if total == 0:
        if items:
            raise _error("统计总数 total=0 却解析出结果卡片")
        return AtlasSearchPage(items=(), total=0, empty_authoritative=True, has_more=False)
    expected_size = min(SEARCH_PAGE_SIZE, total - offset)
    if expected_size <= 0 or len(items) != expected_size:
        raise _error(f"第 {page} 页卡片数量与总数不符：expect={expected_size} got={len(items)} total={total}")
    seen_so_far = offset + len(items)

    return AtlasSearchPage(
        items=tuple(items),
        total=total,
        empty_authoritative=False,
        has_more=(seen_so_far < total),
    )


def _extract_attr_or_inline(fragment: str, label: str) -> str:
    m = re.search(rf'(?is){re.escape(label)}[：:]\s*<span([^>]*)>(.*?)</span>', fragment)
    if not m:
        return ""
    m_title = re.search(r'title="([^"]*)"', m.group(1))
    if m_title:
        return html.unescape(m_title.group(1)).strip()
    return _plain_inline(m.group(2))


def _extract_relation_codes(fragment: str, label: str) -> str:
    m = re.search(rf'(?is)<div[^>]*>\s*{re.escape(label)}[：:](.*?)</div>', fragment)
    if not m:
        return ""
    inner = m.group(1)
    links = [_plain_inline(a) for a in re.findall(r"(?is)<a[^>]*>(.*?)</a>", inner)]
    links = [x for x in links if x and x != "-"]
    if links:
        return ", ".join(links)
    plain = _plain_inline(inner)
    return "" if plain in ("", "-") else plain


def parse_detail_page(html_text: str, *, book_id: str = "") -> AtlasDetail:
    """解析 /zbooklib/book/detail/show 详情页（方案 A 提取代替/被代替图集号）。"""
    if not isinstance(html_text, str) or not html_text.strip():
        raise _error("详情页响应为空")
    m_tit = re.search(r'(?is)<h4[^>]*class="[^"]*detailTit[^"]*"[^>]*>(.*?)</h4>', html_text)
    m_sub = re.search(r'(?is)<div[^>]*class="[^"]*bookDet_sub[^"]*"[^>]*>(.*)', html_text)
    if not m_tit or not m_sub:
        raise _error("详情页缺少 detailTit 或 bookDet_sub 元数据块")

    tit_html = m_tit.group(1)
    # 剔除 HTML 注释避免干扰标题/状态匹配（站点原页含 JSP 注释模板）。
    tit_clean = re.sub(r"(?s)<!--.*?-->", "", tit_html)
    sub_clean = re.sub(r"(?s)<!--.*?-->", "", m_sub.group(1))

    resolved_id = str(book_id).strip()
    if not resolved_id:
        m_id = re.search(r'(?is)id="bookDetailId"[^>]*value="(\d+)"|value="(\d+)"[^>]*id="bookDetailId"', html_text)
        if m_id:
            resolved_id = m_id.group(1) or m_id.group(2)

    m_unit = re.search(r'(?is)<span[^>]*id="bookUnit"[^>]*>([^<]+)</span>', tit_clean)
    if not m_unit:
        m_unit = re.search(r'(?is)<span[^>]*class="[^"]*(?:active|abolish|unenforced)[^"]*"[^>]*>([^<]+)</span>', tit_clean)
    if not m_unit:
        raise _error("详情页缺少现行/废止状态标记")
    raw_status = _plain_inline(m_unit.group(1))

    code = _extract_attr_or_inline(sub_clean, "编号")
    name = _extract_attr_or_inline(sub_clean, "名称")

    m_hidden_title = re.search(r'(?is)id="bookTitle"[^>]*value="([^"]*)"', html_text)
    if m_hidden_title and m_hidden_title.group(1).strip():
        name = html.unescape(m_hidden_title.group(1)).strip()

    m_title_span = re.search(r'(?is)<span[^>]*id="bookDetailTitle"[^>]*>(.*?)</span>', tit_clean)
    if m_title_span and (not code or not name):
        t_code, t_name = _split_code_name(_plain_inline(m_title_span.group(1)))
        code = code or t_code
        name = name or t_name

    if not code and not name:
        raise _error("详情页缺少图集编号与名称")

    m_rtype = re.search(r"资源类型[：:]\s*([^<\r\n]*)", sub_clean)
    m_pub = re.search(r"发布日期[：:]\s*([^<\r\n]*)", sub_clean)
    m_imp = re.search(r"实施日期[：:]\s*([^<\r\n]*)", sub_clean)
    m_abo = re.search(r"废止日期[：:]\s*([^<\r\n]*)", sub_clean)
    m_edt = re.search(r"主编单位[：:]\s*([^<\r\n]*)", sub_clean)

    replaced_by = _extract_relation_codes(sub_clean, "代替标准号")
    replaces = _extract_relation_codes(sub_clean, "被代替标准号")
    status = _normalize_status(raw_status, replaced_by)

    return AtlasDetail(
        book_id=resolved_id,
        code=code,
        name=name,
        status=status,
        resource_type=_plain_inline(m_rtype.group(1)) if m_rtype else "",
        publish_date=_clean_date(m_pub.group(1)) if m_pub else "",
        implement_date=_clean_date(m_imp.group(1)) if m_imp else "",
        abolish_date=_clean_date(m_abo.group(1)) if m_abo else "",
        replaces=replaces,
        replaced_by=replaced_by,
        editor=_plain_inline(m_edt.group(1)) if m_edt else "",
        url=detail_url(resolved_id) if resolved_id else "",
    )


def _cell_codes(cell_html: str) -> str:
    links = []
    for attrs, inner in re.findall(r"(?is)<a([^>]*)>(.*?)</a>", cell_html):
        m_title = re.search(r'title="([^"]*)"', attrs)
        val = html.unescape(m_title.group(1)).strip() if m_title else _plain_inline(inner)
        val = re.sub(r"\s+", " ", val).strip()
        if val:
            links.append(val)
    if links:
        return ", ".join(links)
    return _plain_inline(cell_html)


def _merge_codes(existing: str, incoming: str) -> str:
    if not incoming:
        return existing
    if not existing:
        return incoming
    items = list(dict.fromkeys([*(x.strip() for x in existing.split(",") if x.strip()),
                                *(x.strip() for x in incoming.split(",") if x.strip())]))
    return ", ".join(items)


def parse_concise_catalogue(html_text: str) -> tuple[AtlasCatalogEntry, ...]:
    """解析 /zbooklib/conciseCatalogueList（sublibID=2118）全库图集目录并按 bookID 去重。"""
    if not isinstance(html_text, str) or "menuTd" not in html_text:
        raise _error("简明目录页缺少 menuTd 数据表")

    m_table = re.search(r'(?is)<table[^>]*class="[^"]*menuTd[^"]*"[^>]*>(.*?)</table>', html_text)
    if not m_table:
        raise _error("简明目录页 menuTd 表体不完整")

    rows_by_id: dict[str, AtlasCatalogEntry] = {}
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", m_table.group(1)):
        parsed_cells: dict[str, tuple[str, str]] = {}
        for m_td in re.finditer(r'(?is)<td([^>]*)class="[^"]*\b(td[1-6])\b[^"]*"([^>]*)>(.*?)</td>', tr):
            attrs = m_td.group(1) + " " + m_td.group(3)
            parsed_cells[m_td.group(2).lower()] = (attrs, m_td.group(4))
        if not parsed_cells:
            continue

        td2_attrs, td2_inner = parsed_cells.get("td2", ("", ""))
        m_td2_title = re.search(r'title="([^"]*)"', td2_attrs)
        code = re.sub(
            r"\s+",
            " ",
            html.unescape(m_td2_title.group(1)).strip() if m_td2_title else _plain_inline(td2_inner),
        ).strip()

        _, td3_inner = parsed_cells.get("td3", ("", ""))
        m_a = re.search(r'(?is)<a([^>]*)href="([^"]*)"([^>]*)>(.*?)</a>', td3_inner)
        if not m_a:
            continue
        book_id = _extract_book_id(m_a.group(2))
        if not book_id or not code:
            continue
        a_attrs = m_a.group(1) + " " + m_a.group(3)
        m_name_title = re.search(r'title="([^"]*)"', a_attrs)
        name = re.sub(
            r"\s+",
            " ",
            html.unescape(m_name_title.group(1)).strip() if m_name_title else _plain_inline(m_a.group(4)),
        ).strip()

        _, td4_inner = parsed_cells.get("td4", ("", ""))
        raw_status = _plain_inline(td4_inner)
        replaces = _cell_codes(parsed_cells.get("td5", ("", ""))[1])
        replaced_by = _cell_codes(parsed_cells.get("td6", ("", ""))[1])
        status = _normalize_status(raw_status, replaced_by)
        key = normalize_code(code)

        prev = rows_by_id.get(book_id)
        if prev is None:
            rows_by_id[book_id] = AtlasCatalogEntry(
                book_id=book_id,
                code=code,
                code_key=key,
                name=name,
                status=status,
                replaces=replaces,
                replaced_by=replaced_by,
                url=detail_url(book_id),
            )
        else:
            merged_replaces = _merge_codes(prev.replaces, replaces)
            merged_replaced_by = _merge_codes(prev.replaced_by, replaced_by)
            merged_status = "superseded" if (prev.status in ("withdrawn", "superseded") and merged_replaced_by) else prev.status
            rows_by_id[book_id] = AtlasCatalogEntry(
                book_id=prev.book_id,
                code=prev.code,
                code_key=prev.code_key,
                name=prev.name or name,
                status=merged_status,
                replaces=merged_replaces,
                replaced_by=merged_replaced_by,
                url=prev.url,
            )

    if not rows_by_id:
        raise _error("简明目录表未解析出任何有效图集记录")
    return tuple(rows_by_id.values())
