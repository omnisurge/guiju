"""Native currency metadata HTTP facade (not third-party httpx)."""

from __future__ import annotations

import json
import math
import time  # noqa: F401  # tests patch facade.time.sleep for metadata backoff

from .._version import __version__
from ..errors import ComplianceDenied
from ..net.block import BlockDetector
from ..net.endpoints import DEFAULT_METADATA_ENDPOINTS
from ..net.metadata import MetadataClient
from ..net.policy import MetadataPolicy, normalize_path
from ..net.redirect import RedirectGuard
from ..net.throttle import default_throttle
from .contract import json_error as _json_error

DEFAULT_TIMEOUT = 25.0
MAX_ATTEMPTS = 3
BACKOFF = (0.5, 1.5)
MIN_INTERVAL = 1.2
MAX_RESPONSE_BYTES = 2_000_000
UA = f"guiju/{__version__} (metadata-only; standards currency)"

_ENDPOINTS = DEFAULT_METADATA_ENDPOINTS


def _normalize_path(path: str) -> str:
    return normalize_path(path)


def _check_url(url: str) -> str:
    return MetadataPolicy().check_url(url)


def _check_redirect_target(req, target: str) -> str:
    return MetadataPolicy().check_redirect(req, target)


def _throttle(host: str) -> None:
    default_throttle().wait(host)


class _CompatThrottle:
    def wait(self, host: str) -> None:
        _throttle(host)


def _check_headers(headers, url: str, *, redirect: bool = False) -> None:
    MetadataPolicy().check_headers(headers, url, redirect=redirect)


def _detect_block(body: bytes) -> None:
    BlockDetector().check(body, "text/html", reject_binary=True)


def _MetadataRedirect():
    """Compatibility factory for tests that compose a raw urllib opener."""
    return RedirectGuard(MetadataPolicy(), _CompatThrottle())


class HttpClient:
    """Only returns raw metadata bytes; adapters own parsing."""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("元数据请求超时必须为正数")
        self.timeout = timeout
        self._client = MetadataClient(timeout=timeout, throttle=_CompatThrottle())
        self.transport = self._client.transport

    def get(self, url: str, *, accept: str | None = "text/html,application/xhtml+xml") -> bytes:
        self._client.transport = self.transport
        return self._client.get(url, accept=accept)

    def post_form(self, url: str, fields: dict[str, str], *, accept: str | None = "application/json") -> bytes:
        self._client.transport = self.transport
        host = self._client.policy.check_url(url)
        from .registry import default_registry
        if not default_registry().form_post_for_host(host):
            raise ComplianceDenied(f"此元数据端点不接受查询表单：{url}")
        return self._client.post_form(url, fields, accept=accept)

    def post_json(self, url: str, payload: dict) -> bytes:
        self._client.transport = self.transport
        return self._client.post_json(url, payload)


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


def json_flag(item: dict, key: str, source: str) -> bool | None:
    """Boolean flag (e.g. ccsn HasNextPage); null means unknown; 0/1 accepted."""
    value = item.get(key)
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise _json_error(source, f"{key} 必须为布尔值")
