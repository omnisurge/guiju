"""Approved metadata endpoints. This is data, not policy code."""

from __future__ import annotations

HTML = frozenset({"text/html", "application/xhtml+xml"})
JSON = frozenset({"text/html", "application/xhtml+xml", "application/json", "text/json", "text/plain"})

CHINABUILDING_HOST = "ebook.chinabuilding.com.cn"
CHINABUILDING_CONCISE_PATH = "/zbooklib/conciseCatalogueList"

DEFAULT_METADATA_ENDPOINTS: dict[str, dict[str, frozenset[str]]] = {
    "www.csres.com": {"/s.jsp": HTML},
    "www.ccsn.org.cn": {"/newweb/api/StandardQuery.assx/queryStandard": JSON},
    "std.samr.gov.cn": {"/gb/search/gbQueryPage": JSON},
    "jtst.mot.gov.cn": {"/search/stdPage": HTML},
    "dbba.sacinfo.org.cn": {"/stdQueryList": JSON},
    "bzjg.zjw.sh.gov.cn": {"/JGBStandardWebApi/external/localStandard/list": JSON},
    CHINABUILDING_HOST: {
        "/zbooklib/search": HTML,
        "/zbooklib/book/detail/show": HTML,
        CHINABUILDING_CONCISE_PATH: HTML,
    },
}

DEFAULT_METADATA_MAX_BYTES: dict[tuple[str, str], int] = {
    (CHINABUILDING_HOST, CHINABUILDING_CONCISE_PATH): 8_000_000,
}

DEFAULT_METADATA_FORM_HOSTS: frozenset[str] = frozenset({
    "www.ccsn.org.cn",
    "dbba.sacinfo.org.cn",
})

SHANGHAI_HOST = "bzjg.zjw.sh.gov.cn"
SHANGHAI_PORT = 9175
SHANGHAI_ORIGIN = f"https://{SHANGHAI_HOST}:{SHANGHAI_PORT}"
SHANGHAI_REFERER = SHANGHAI_ORIGIN + "/JGBStandardWeb/pc/"
