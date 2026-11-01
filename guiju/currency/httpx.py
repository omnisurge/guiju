"""原生时效元数据的 HTTP 出口（不是第三方 httpx，也不是正文下载器）。

仅放行已批准的时效查询端点，逐跳校验；响应仅在内存中，拒绝附件与二进制。
默认 25s / 最多 3 次 / 0.5s、1.5s 退避，按 host 至少间隔 1.2s。
不调用 compliance：正文出口与元数据出口的边界由测试分别钉住。
"""

from __future__ import annotations

import html
import http.client
import io
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .._version import __version__
from ..errors import ComplianceDenied, NeedsHuman, SourceUnreachable

DEFAULT_TIMEOUT = 25.0
MAX_ATTEMPTS = 3
BACKOFF = (0.5, 1.5)
MIN_INTERVAL = 1.2
MAX_RESPONSE_BYTES = 2_000_000
UA = f"guiju/{__version__} (metadata-only; standards currency)"

# 不是可配置白名单：不提供任意主机 / 路径 / 下载开关。
_HTML = frozenset({"text/html", "application/xhtml+xml"})
_JSON = _HTML | {"application/json", "text/json", "text/plain"}
_ENDPOINTS = {
    "www.csres.com": {"/s.jsp": _HTML},
    "www.ccsn.org.cn": {"/newweb/api/StandardQuery.assx/queryStandard": _JSON},
    "std.samr.gov.cn": {"/gb/search/gbQueryPage": _JSON},
    "jtst.mot.gov.cn": {"/search/stdPage": _HTML},
    "dbba.sacinfo.org.cn": {"/stdQueryList": _JSON},
    "bzjg.zjw.sh.gov.cn": {"/JGBStandardWebApi/external/localStandard/list": _JSON},
}
_FORM_HOSTS = frozenset({"www.ccsn.org.cn", "dbba.sacinfo.org.cn"})
_SHANGHAI_HOST = "bzjg.zjw.sh.gov.cn"
_SHANGHAI_ORIGIN = "https://bzjg.zjw.sh.gov.cn:9175"
_SHANGHAI_REFERER = _SHANGHAI_ORIGIN + "/JGBStandardWeb/pc/"
_BLOCK_MARKERS = (
    "验证码", "人机验证", "滑动验证", "安全验证", "访问过于频繁", "请求过于频繁",
    "verify you are human", "cf-chl-",
)
_last_call: dict[str, float] = {}


def _check_url(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ComplianceDenied("元数据 URL 端口无效") from exc
    host = (parts.hostname or "").lower()
    if (
        parts.scheme not in ("http", "https")
        or host not in _ENDPOINTS
        or parts.path not in _ENDPOINTS[host]
        or parts.username is not None
        or parts.password is not None
        or (host == _SHANGHAI_HOST and (parts.scheme != "https" or port != 9175))
        or (host != _SHANGHAI_HOST and port not in (None, 80 if parts.scheme == "http" else 443))
        or parts.fragment
    ):
        raise ComplianceDenied(f"非已批准元数据查询端点，拒绝访问：{url}")
    return host


def _check_redirect_target(req, target: str) -> str:
    try:
        host = _check_url(target)
    except ComplianceDenied as exc:
        parts = urllib.parse.urlsplit(target)
        current = urllib.parse.urlsplit(req.full_url)
        if ((parts.hostname or "").lower() == (current.hostname or "").lower()
                and parts.path.split(";", 1)[0] == "/error/noright.html"):
            # csres 用错误页代替 4xx/5xx；这是源故障，应允许 fallback，
            # 不能伪装成“命中合规护栏”而阻断备源。
            raise SourceUnreachable(f"元数据源返回错误页，不跟随：{target}") from exc
        raise
    if host != urllib.parse.urlsplit(req.full_url).hostname.lower():
        raise ComplianceDenied("元数据重定向不得跨源，即使目标也是已批准查询端点")
    return host


def _throttle(host: str) -> None:
    now = time.monotonic()
    last = _last_call.get(host)
    if last is not None and now - last < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - (now - last))
    _last_call[host] = time.monotonic()


def _check_headers(headers, url: str, *, redirect: bool = False) -> None:
    ctype = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    disposition = headers.get("Content-Disposition", "").lower()
    parts = urllib.parse.urlsplit(url)
    allowed = _ENDPOINTS[parts.hostname.lower()][parts.path]
    if "attachment" in disposition or (ctype not in allowed and not (redirect and not ctype)):
        raise ComplianceDenied(f"元数据出口拒绝非查询文本或附件响应：{url}")


class _MetadataRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def http_error_302(self, req, fp, code, msg, headers):
        # urllib 基类会 fp.read() 无界吞掉中间响应。这里只读有界 HTML 头
        # 检测人机校验/伪装二进制，随后关闭；给基类空流以保留其循环检测。
        try:
            target = headers.get("Location") or headers.get("URI")
            if not target:
                raise SourceUnreachable(f"元数据重定向未提供目标：{req.full_url}")
            _check_redirect_target(req, urllib.parse.urljoin(req.full_url, target))
            _check_headers(headers, req.full_url, redirect=True)
            try:
                head = fp.read(8192)
            except (OSError, http.client.HTTPException) as exc:
                raise SourceUnreachable(f"元数据重定向响应读取失败：{req.full_url}") from exc
            _detect_block(head)
            missing = getattr(fp, "length", None)
            if len(head) < 8192 and missing is not None and missing > 0:
                raise SourceUnreachable(f"元数据重定向响应读取失败（响应截断）：{req.full_url}")
        finally:
            fp.close()
        with io.BytesIO() as empty:
            return super().http_error_302(req, empty, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        host = _check_redirect_target(req, target)
        if host == _SHANGHAI_HOST and (req.get_method() != "POST" or code not in (307, 308)):
            raise ComplianceDenied("上海查询不跟随会丢失 JSON 条件的重定向")
        _throttle(host)
        if req.get_method() == "POST" and code in (307, 308):
            # 查询表单可重放；307/308 不得被 urllib 变成丢失参数的 GET。
            kept = {k: v for k, v in req.headers.items() if k.lower() != "content-length"}
            return urllib.request.Request(target, data=req.data, headers=kept, method="POST",
                                          origin_req_host=req.origin_req_host, unverifiable=True)
        # Python 3.10 基类未列 308；GET 按同样保留方法的 307 处理。
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, target)


def _detect_block(body: bytes) -> None:
    if body.lstrip().startswith((b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0")):
        raise ComplianceDenied("元数据出口拒绝伪装成 HTML 的正文文件")
    # 上游 GBK，错误页也可能是 UTF-8；识别任一编码的人机校验便立即放弃。
    for encoding in ("utf-8", "gbk"):
        head = body[:8192].decode(encoding, "ignore").lower()
        # JSON Unicode 转义与 HTML 实体不能把人机校验藏成“无命中”。
        head = html.unescape(re.sub(r"\\u([0-9a-f]{4})", lambda m: chr(int(m[1], 16)), head))
        for marker in _BLOCK_MARKERS:
            if marker in head:
                raise ComplianceDenied(f"元数据源返回人机校验（命中「{marker}」），停止，不重试")


def _json_error(source: str, detail: str) -> NeedsHuman:
    return NeedsHuman(f"{source} 元数据 JSON 结构异常：{detail}", hint=f"检查 {source} 响应结构，不要把解析失败当作无命中")


def decode_json(source: str, raw: bytes) -> dict:
    def reject_constant(value):
        raise ValueError(f"非法 JSON 数值 {value}")

    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        snippet = raw[:160].decode("utf-8", "replace").strip().replace("\r", " ").replace("\n", " ")
        raise _json_error(source, f"不是有效 UTF-8 JSON；响应开头：{snippet}") from exc
    if not isinstance(payload, dict):
        raise _json_error(source, "顶层必须为对象")
    return payload


def json_rows(payload: dict, key: str, source: str) -> list[dict]:
    rows = payload.get(key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise _json_error(source, f"{key} 必须为数组")
    if any(row is not None and not isinstance(row, dict) for row in rows):
        raise _json_error(source, f"{key} 的条目必须为对象")
    return [row if row is not None else {} for row in rows]


def json_string(item: dict, key: str, source: str) -> str:
    value = item.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _json_error(source, f"{key} 必须为字符串")
    return value


def json_int(item: dict, key: str, source: str) -> int:
    value = item.get(key)
    if value is None:
        return 0
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise _json_error(source, f"{key} 必须为 int64 整数")
    return value


class HttpClient:
    """仅返回原始元数据字节；编码与解析错误在适配器层处理，不重发请求。"""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("元数据请求超时必须为正数")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_MetadataRedirect())

    def get(self, url: str, *, accept: str | None = "text/html,application/xhtml+xml") -> bytes:
        return self._request(url, method="GET", accept=accept)

    def post_form(self, url: str, fields: dict[str, str], *, accept: str | None = "application/json") -> bytes:
        host = _check_url(url)
        if host not in _FORM_HOSTS:
            raise ComplianceDenied(f"此元数据端点不接受查询表单：{url}")
        data = urllib.parse.urlencode(fields).encode("ascii")
        return self._request(url, method="POST", data=data, accept=accept)

    def post_json(self, url: str, payload: dict) -> bytes:
        host = _check_url(url)
        if host != _SHANGHAI_HOST:
            raise ComplianceDenied("JSON POST 只允许上海固定元数据查询端点")
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        return self._request(url, method="POST", data=data, accept="application/json", json_body=True)

    def _request(self, url: str, *, method: str, data: bytes | None = None, accept: str | None,
                 json_body: bool = False) -> bytes:
        host = _check_url(url)
        if host == _SHANGHAI_HOST and method != "POST":
            raise ComplianceDenied("上海元数据查询只接受 POST JSON，不跟随改为 GET 的跳转")
        if accept not in (None, "application/json", "text/html", "text/html,application/xhtml+xml"):
            raise ValueError("元数据 Accept 只允许已知查询文本类型")
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                time.sleep(BACKOFF[attempt - 1])
            _throttle(host)
            headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
            if accept is not None:
                headers["Accept"] = accept
            if data is not None:
                headers["Content-Type"] = "application/json" if json_body else "application/x-www-form-urlencoded"
            if host == _SHANGHAI_HOST:
                headers["Origin"] = _SHANGHAI_ORIGIN
                headers["Referer"] = _SHANGHAI_REFERER
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    # urllib 对 204/206 不抛 HTTPError；与 Go 一样只认 200。
                    status = resp.status
                    if status != 200:
                        raise SourceUnreachable(f"元数据源返回 HTTP {status}：{url}")
                    _check_redirect_target(req, resp.geturl())
                    _check_headers(resp.headers, url)
                    try:
                        # read(amt) 遇 Content-Length 截断不会自动抛 IncompleteRead。
                        # 有界读到 EOF 后还须检查剩余长度，不能把半页伪装成无命中。
                        chunks: list[bytes] = []
                        remaining = MAX_RESPONSE_BYTES + 1
                        while remaining:
                            chunk = resp.read(remaining)
                            if not chunk:
                                break
                            chunks.append(chunk)
                            remaining -= len(chunk)
                        body = b"".join(chunks)
                        missing = getattr(resp, "length", None)
                        if len(body) <= MAX_RESPONSE_BYTES and missing is not None and missing > 0:
                            raise SourceUnreachable(f"元数据响应读取失败（响应截断，尚缺 {missing} 字节）：{url}")
                    except (OSError, http.client.HTTPException) as exc:
                        raise SourceUnreachable(f"元数据响应读取失败：{url}：{exc}") from exc
                    _detect_block(body)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise NeedsHuman("元数据响应过大，停止解析", hint="检查时效查询端点与响应结构")
                    return body
            except urllib.error.HTTPError as exc:
                try:
                    if exc.fp is not None and not getattr(exc.fp, "closed", False):
                        _detect_block(exc.read(8192))
                except (OSError, http.client.HTTPException, ValueError):
                    pass  # 错误页读失败/重定向循环的空流已关闭，都不改变 HTTP 状态分类。
                finally:
                    exc.close()
                retryable = 500 <= exc.code <= 599 or exc.code == 429
                if not retryable or attempt == MAX_ATTEMPTS - 1:
                    raise SourceUnreachable(f"元数据源返回 HTTP {exc.code}：{url}") from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    raise SourceUnreachable(f"元数据源不可达（{url}）：{type(exc).__name__}：{exc}") from exc
        raise AssertionError("元数据请求重试循环不应落空")  # pragma: no cover
