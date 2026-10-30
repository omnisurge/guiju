"""合规护栏 —— 所有出站请求的唯一出口。

设计原则（刻意不提供 override 开关，因为"开发者自觉"式的合规三个月内必然失效）：

1. 域名白名单：只允许 *.gov.cn，其他一律拒绝
2. 限速：默认最小间隔 1.2 秒，宁可慢，不可打
3. 诚实 UA：声明身份，不伪装浏览器
4. 命中人机校验立即放弃 —— 不尝试绕过，放弃比绕过正确
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

#: 诚实 UA：声明身份，不伪装浏览器。HTTP 头只接受 latin-1，故必须纯 ASCII。
UA = "guiju/0.1 (compliance-aware; official code fetcher)"
ALLOWED_SUFFIX = ".gov.cn"
MIN_INTERVAL = 1.2

#: 命中这些就认定被反爬拦下，直接放弃（不重试、不换 UA、不接代理）
BLOCK_MARKERS = (
    "验证码",
    "人机验证",
    "滑动验证",
    "安全验证",
    "访问过于频繁",
    "请求过于频繁",
    "Access Denied",
    "Forbidden",
)


class ComplianceError(RuntimeError):
    """触及合规护栏。调用方应停止，而不是重试。"""


class Unreachable(RuntimeError):
    """网络层不可达，可稍后重试。"""


_last_call = [0.0]


def check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http"):
        raise ComplianceError(f"仅允许 http(s)：{url}")
    if not (host == "gov.cn" or host.endswith(ALLOWED_SUFFIX)):
        raise ComplianceError(f"非政府域名，拒绝访问：{host}")
    return url


def _throttle() -> None:
    gap = time.monotonic() - _last_call[0]
    if gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_call[0] = time.monotonic()


def _inspect(data: bytes) -> None:
    """只对非 PDF 的小响应做拦截检测，避免误伤和额外开销。"""
    if data.startswith(b"%PDF"):
        return
    if len(data) > 200_000:
        return
    head = data[:6000].decode("utf-8", "ignore")
    for marker in BLOCK_MARKERS:
        if marker in head:
            raise ComplianceError(
                f"目标站点返回人机校验（命中「{marker}」）。按要求放弃，不做绕过。"
            )


def get(url: str, referer: str | None = None, timeout: int = 60) -> tuple[bytes, str]:
    """发一次 GET。返回 (body, content_type)。"""
    check_url(url)
    _throttle()
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise Unreachable(f"HTTP {exc.code}：{url}") from exc
    except ssl.SSLError as exc:
        raise Unreachable(f"TLS 失败：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一归为不可达
        raise Unreachable(f"{type(exc).__name__}：{exc}［{url}］") from exc
    _inspect(body)
    return body, ctype
