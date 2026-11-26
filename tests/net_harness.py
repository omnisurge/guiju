"""Offline characterization harness: in-memory responses, no sockets."""

from __future__ import annotations

import io
import urllib.request
from typing import Any

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
        self.probe_calls: list[dict[str, Any]] = []

    def open(self, req, timeout=0):
        self.calls.append(req)
        if not self.responses:
            raise AssertionError("no fake response left")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def probe(self, url: str, *, timeout: float = 15.0, referer: str | None = None):
        self.probe_calls.append({"url": url, "timeout": timeout, "referer": referer})
        return self.probe_info




def metadata_redirect(throttle=None):
    """Test-only RedirectGuard construction; production has no factory."""
    return RedirectGuard(MetadataPolicy(), throttle or Throttle(0))


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


class ShortRead:
    """Response-like object: controlled short reads, then an optional error."""

    def __init__(
        self,
        pieces=(),
        *,
        headers=None,
        error=None,
        url="https://www.mohurd.gov.cn/gongkai/a.pdf",
        status=200,
    ):
        self.pieces = [bytes(piece) for piece in pieces]
        self.headers = dict(headers or {})
        self.error = error
        self._url = url
        self.status = status
        self.closed = False

    def read(self, size=-1):
        if self.pieces:
            piece = self.pieces.pop(0)
            if size is not None and 0 <= size < len(piece):
                self.pieces.insert(0, piece[size:])
                piece = piece[:size]
            return piece
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return b""

    def geturl(self):
        return self._url

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
