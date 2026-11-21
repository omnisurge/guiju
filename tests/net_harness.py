"""Offline characterization harness: in-memory responses, no sockets."""

from __future__ import annotations

import io
import urllib.request
from typing import Any

from guiju.currency import httpx as _httpx
from guiju.net.body import BodyClient
from guiju.net.policy import BodyPolicy, MetadataPolicy
from guiju.net.redirect import RedirectGuard
from guiju.net.throttle import Throttle


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes = b"", *, status: int = 200, headers: dict[str, str] | None = None,
                 url: str = "http://127.0.0.1/f.pdf", length: int | None = None) -> None:
        super().__init__(body)
        self.status = status
        self.headers = headers or {"Content-Type": "application/pdf", "Content-Length": str(len(body))}
        self._url = url
        self.read_sizes: list[int] = []
        if length is None:
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = None
        self.length = length

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        data = super().read(size)
        if self.length is not None:
            self.length = max(0, self.length - len(data))
        return data

    def close(self) -> None:
        super().close()


class FakeTransport:
    def __init__(self, responses: list[Any] | None = None, *, probe_info=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[Any] = []
        self.probe_info = probe_info

    def open(self, req, timeout=0):
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("no fake response left")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def probe(self, url: str, *, timeout: float = 15.0):
        return self.probe_info




def metadata_redirect():
    """Test-only RedirectGuard construction; production has no factory."""
    return RedirectGuard(MetadataPolicy(), _httpx._CompatThrottle())


def build_metadata_opener(memory_transport, handler=None):
    """Test-only raw opener assembly kept out of production/test subject modules."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), handler or metadata_redirect(), memory_transport
    )

def body_client(transport: FakeTransport, **kwargs) -> BodyClient:
    return BodyClient(
        policy=BodyPolicy(check_url_fn=lambda url: url),
        throttle=Throttle(min_interval=0),
        transport=transport,
        **kwargs,
    )
