"""The only place that constructs urllib openers and executes requests."""

from __future__ import annotations

import http.client
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .block import BlockDetector
from .headers import header_int
from .redirect import RedirectGuard
from .request import ProbeInfo
from .useragent import BODY_USER_AGENT

UA = BODY_USER_AGENT


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

    def probe(self, url: str, *, timeout: float = 15.0, referer: str | None = None) -> ProbeInfo:
        """Best-effort HEAD through the same policy/throttle/header path as GET.

        BodyPolicy.check_url returns a canonical URL; MetadataPolicy.check_url
        returns the approved host.  Only replace the request target when the
        policy actually returned a URL.
        """
        checked = self.policy.check_url(url)
        request_url = checked if urllib.parse.urlsplit(checked).scheme in ("http", "https") else url
        headers = {"User-Agent": BODY_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
        if referer:
            headers["Referer"] = referer
        self.throttle.wait(urllib.parse.urlsplit(request_url).hostname or "")
        try:
            req = urllib.request.Request(request_url, method="HEAD", headers=headers)
            with self.open(req, timeout=timeout) as resp:
                response_headers = resp.headers
                return ProbeInfo(
                    status=getattr(resp, "status", None),
                    content_length=header_int(response_headers.get("Content-Length")),
                    content_type=response_headers.get("Content-Type", "").split(";", 1)[0].strip().lower(),
                    etag=response_headers.get("ETag", ""),
                    last_modified=response_headers.get("Last-Modified", ""),
                )
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            return ProbeInfo()
