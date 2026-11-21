"""Compliance facade over the single outbound layer (net/).

The policy constants and public names stay here for compatibility; all real
transport, redirect, throttle, block and body logic lives in guiju.net.
"""

from __future__ import annotations

import time  # noqa: F401  # tests patch facade.time.sleep for legacy download tests
from pathlib import Path

from ._version import __version__
from .net.block import BLOCK_MARKERS, BlockDetector
from .net.body import BodyClient
from .net.headers import header_int as _header_int
from .net.headers import parse_content_range as _parse_content_range
from .net.metadata import MetadataClient
from .net.policy import BodyPolicy
from .net.request import Response
from .net.throttle import default_throttle

UA = f"guiju/{__version__} (compliance-aware; official code fetcher)"
ALLOWED_SUFFIX = ".gov.cn"
DEFAULT_MIN_INTERVAL = 1.2


def check_url(url: str) -> str:
    """Whitelist check used by every body URL, including redirect targets."""
    return BodyPolicy().check_url(url)


_throttle = default_throttle().wait


class _CompatThrottle:
    """Adapter so existing test patches of compliance._throttle keep working."""

    def wait(self, host: str) -> None:
        _throttle(host)


def _body_client() -> BodyClient:
    return BodyClient(
        policy=BodyPolicy(check_url_fn=check_url),
        throttle=_CompatThrottle(),
        detector=BlockDetector(),
    )


def get(url: str, referer: str | None = None, timeout: int = 60, attempts: int = 2) -> Response:
    return _body_client().get_page(url, referer=referer, timeout=timeout, attempts=attempts)


def post_json(
    url: str,
    body: dict,
    *,
    referer: str | None = None,
    timeout: int = 60,
    attempts: int = 2,
) -> dict:
    client = MetadataClient(
        throttle=_CompatThrottle(),
        detector=BlockDetector(),
        timeout=timeout,
        attempts=attempts,
    )
    return client.post_json_dict(url, body, referer=referer)


def download(
    url: str,
    part_path: str | Path,
    *,
    referer: str | None = None,
    timeout: int = 180,
    attempts: int = 2,
    chunk_size: int = 1 << 16,
) -> dict:
    result = _body_client().download(
        url, part_path, referer=referer, timeout=timeout, attempts=attempts, chunk_size=chunk_size
    )
    return {
        "url": result.url,
        "bytes": result.bytes,
        "resumed_from": result.resumed_from,
        "status": result.status,
        "etag": result.etag,
        "last_modified": result.last_modified,
    }


def download_bytes(
    url: str,
    *,
    referer: str | None = None,
    timeout: int = 180,
    max_bytes: int = 128 * 1024 * 1024,
) -> tuple[bytes, str]:
    return _body_client().download_bytes(url, referer=referer, timeout=timeout, max_bytes=max_bytes)



__all__ = [
    "ALLOWED_SUFFIX",
    "BLOCK_MARKERS",
    "DEFAULT_MIN_INTERVAL",
    "UA",
    "Response",
    "_header_int",
    "_parse_content_range",
    "check_url",
    "download",
    "download_bytes",
    "get",
    "post_json",
]
