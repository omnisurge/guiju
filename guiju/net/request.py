"""Pure data structures for the single outbound layer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Request:
    url: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout: float = 60.0
    attempts: int = 2
    max_bytes: int | None = None


@dataclass(frozen=True)
class ProbeRecord:
    url: str
    status: int | None
    bytes: int | None
    outcome: str
    attempts: int = 1
    redirects: tuple[str, ...] = ()
    elapsed_ms: int = 0


@dataclass(frozen=True)
class ProbeInfo:
    status: int | None = None
    content_length: int | None = None
    content_type: str = ""
    etag: str = ""
    last_modified: str = ""


@dataclass(frozen=True)
class Response:
    url: str
    body: bytes
    content_type: str
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    probe: ProbeRecord | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "ignore")


@dataclass(frozen=True)
class DownloadResult:
    url: str
    bytes: int
    resumed_from: int
    status: int = 200
    etag: str = ""
    last_modified: str = ""
    probe: ProbeRecord | None = None

    def __post_init__(self) -> None:
        if self.probe is None:
            object.__setattr__(
                self, "probe", ProbeRecord(url=self.url, status=self.status, bytes=self.bytes, outcome="ok")
            )
