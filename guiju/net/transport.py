"""The only place that constructs urllib openers and executes requests."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request

from .block import BlockDetector
from .headers import header_int
from .redirect import RedirectGuard
from .request import ProbeInfo


class _NoProxyHandler(urllib.request.ProxyHandler):
    """Explicit empty proxy handler that also stays visible in opener.handlers."""

    def http_request(self, req):
        return req


def build_opener(policy, throttle, detector: BlockDetector | None = None) -> urllib.request.OpenerDirector:
    """Build an opener that never reads system/environment proxy settings."""
    return urllib.request.build_opener(
        _NoProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        RedirectGuard(policy, throttle, detector),
    )


class Transport:
    def __init__(self, policy, throttle, detector: BlockDetector | None = None) -> None:
        self.policy = policy
        self.throttle = throttle
        self.detector = detector or BlockDetector()
        self.proxy_handler = _NoProxyHandler({})
        self.opener = urllib.request.build_opener(
            self.proxy_handler,
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            RedirectGuard(policy, throttle, self.detector),
        )

    def open(self, req: urllib.request.Request, timeout: float):
        return self.opener.open(req, timeout=timeout)

    def probe(self, url: str, *, timeout: float = 15.0) -> ProbeInfo:
        """Best-effort HEAD. Any failure is unknown, never an exception."""
        try:
            req = urllib.request.Request(url, method="HEAD")
            with self.open(req, timeout=timeout) as resp:
                headers = resp.headers
                return ProbeInfo(
                    status=getattr(resp, "status", None),
                    content_length=header_int(headers.get("Content-Length")),
                    content_type=headers.get("Content-Type", "").split(";", 1)[0].strip().lower(),
                    etag=headers.get("ETag", ""),
                    last_modified=headers.get("Last-Modified", ""),
                )
        except Exception:
            return ProbeInfo()
