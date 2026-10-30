"""合规护栏 —— 全仓唯一出站闸门。

四条铁律（刻意不提供 override 开关：靠自觉的合规，三个月内必然失效）：

1. 域名白名单：只允许 *.gov.cn，且**每一次重定向**都要重新校验
2. 限速：按 host 令牌间隔，默认最小 1.2 秒，宁可慢，不可打
3. 诚实 UA：声明身份，不伪装浏览器（纯 ASCII，HTTP 头只接受 latin-1）
4. 命中人机校验立即放弃 —— 不尝试绕过，放弃比绕过正确

历史教训（2026-10-30 复核）：原实现只在发请求前校验白名单，而 urllib 默认自动跟随
30x 重定向，一次跳转即可绕过白名单。现由 _GuardedRedirect 逐跳校验。
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from ._version import __version__
from .errors import ComplianceDenied, SourceUnreachable

#: 诚实 UA：声明身份，不伪装浏览器。必须纯 ASCII。
UA = f"guiju/{__version__} (compliance-aware; official code fetcher)"
ALLOWED_SUFFIX = ".gov.cn"
DEFAULT_MIN_INTERVAL = 1.2

#: 命中这些就认定被反爬拦下，直接放弃（不重试、不换 UA、不接代理）
BLOCK_MARKERS = (
    "验证码",
    "人机验证",
    "滑动验证",
    "安全验证",
    "访问过于频繁",
    "请求过于频繁",
)

#: 只对这些类型的响应做拦截检测（PDF 与二进制不检测，避免误伤与额外开销）
_INSPECTABLE = ("text/html", "text/plain", "application/json")
_MAX_INSPECT_BYTES = 2_000_000
_INSPECT_HEAD = 8192


@dataclass(frozen=True)
class Response:
    url: str          # 跟随重定向后的最终 URL
    body: bytes
    content_type: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "ignore")


def check_url(url: str) -> str:
    """白名单校验。任何出站 URL（含重定向目标）都必须过这里。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ComplianceDenied(f"仅允许 http(s)：{url}")
    host = (parts.hostname or "").lower()
    if not (host == "gov.cn" or host.endswith(ALLOWED_SUFFIX)):
        raise ComplianceDenied(f"非政府域名，拒绝访问：{host or '(空)'}")
    return url


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """把白名单钉死在每一次跳转上 —— 这是本产品唯一的红线级校验。"""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urllib.parse.urljoin(req.full_url, newurl)
        check_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


#: 自定义 opener：重定向走白名单闸门，HTTPS 用默认 CA 上下文
#: （注意：OpenerDirector.open() 不接受 context 参数，必须在 handler 上配置）
_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    _GuardedRedirect(),
)
_last_call: dict[str, float] = {}


def _throttle(host: str) -> None:
    last = _last_call.get(host, 0.0)
    gap = time.monotonic() - last
    if gap < DEFAULT_MIN_INTERVAL:
        time.sleep(DEFAULT_MIN_INTERVAL - gap)
    _last_call[host] = time.monotonic()


def _detect_block(resp: Response) -> None:
    ctype = resp.content_type.lower()
    if not any(t in ctype for t in _INSPECTABLE):
        return
    if len(resp.body) > _MAX_INSPECT_BYTES:
        return
    head = resp.body[:_INSPECT_HEAD].decode("utf-8", "ignore")
    for marker in BLOCK_MARKERS:
        if marker in head:
            raise ComplianceDenied(
                f"目标站点返回人机校验（命中「{marker}」）。按要求放弃，不做绕过。"
            )


def get(url: str, referer: str | None = None, timeout: int = 60, attempts: int = 2) -> Response:
    """发一次 GET，返回 Response。这是全仓唯一允许发起网络请求的函数。

    弱网容错：只对**可重试**的失败（超时/连接错误/5xx）做有界重试，默认 2 次、退避 1s。
    合规拒绝（40）与 4xx 一律不重试 —— 重试它们只会变成攻击。
    """
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return _get_once(url, referer=referer, timeout=timeout)
        except _Retryable as exc:
            if attempt >= attempts:
                raise SourceUnreachable(str(exc)) from None
            time.sleep(1.0 * attempt)
    raise SourceUnreachable(f"重试耗尽：{url}")  # pragma: no cover


class _Retryable(Exception):
    """内部标记：这类失败值得再试一次。"""


def _get_once(url: str, referer: str | None = None, timeout: int = 60) -> Response:
    check_url(url)
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    _throttle(host)

    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)

    try:
        with _opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            final_url = resp.geturl() or url
            ctype = resp.headers.get("Content-Type", "") or ""
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise _Retryable(f"HTTP {exc.code}：{url}") from exc
        raise SourceUnreachable(f"HTTP {exc.code}：{url}") from exc
    except ComplianceDenied:
        raise
    except ssl.SSLError as exc:
        raise _Retryable(f"TLS 失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 - 超时/连接错误等，值得再试
        raise _Retryable(f"{type(exc).__name__}：{exc}［{url}］") from exc

    out = Response(url=final_url, body=body, content_type=ctype)
    _detect_block(out)
    return out
