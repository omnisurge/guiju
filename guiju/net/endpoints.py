"""Approved metadata endpoints. This is data, not policy code."""

from __future__ import annotations

HTML = frozenset({"text/html", "application/xhtml+xml"})
JSON = frozenset({"text/html", "application/xhtml+xml", "application/json", "text/json", "text/plain"})

DEFAULT_METADATA_ENDPOINTS: dict[str, dict[str, frozenset[str]]] = {
    "www.csres.com": {"/s.jsp": HTML},
    "www.ccsn.org.cn": {"/newweb/api/StandardQuery.assx/queryStandard": JSON},
    "std.samr.gov.cn": {"/gb/search/gbQueryPage": JSON},
    "jtst.mot.gov.cn": {"/search/stdPage": HTML},
    "dbba.sacinfo.org.cn": {"/stdQueryList": JSON},
    "bzjg.zjw.sh.gov.cn": {"/JGBStandardWebApi/external/localStandard/list": JSON},
}

SHANGHAI_HOST = "bzjg.zjw.sh.gov.cn"
SHANGHAI_ORIGIN = "https://bzjg.zjw.sh.gov.cn:9175"
SHANGHAI_REFERER = SHANGHAI_ORIGIN + "/JGBStandardWeb/pc/"
