"""Metadata-only HTTP client. This is the unique owner of JSON/form POST."""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from .._version import __version__
from ..errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from .block import BlockDetector
from .endpoints import SHANGHAI_HOST, SHANGHAI_ORIGIN, SHANGHAI_REFERER
from .policy import MetadataPolicy
from .read import read_prefix
from .throttle import Throttle, default_throttle
from .transport import Transport

DEFAULT_TIMEOUT = 25.0
MAX_ATTEMPTS = 3
BACKOFF = (0.5, 1.5)
MAX_RESPONSE_BYTES = 2_000_000
UA = f"guiju/{__version__} (metadata-only; standards currency)"


def _looks_like_html(body: bytes) -> bool:
    """A JSON endpoint answered with a web page, tolerating BOM and whitespace."""
    view = body
    while view.startswith(b"\xef\xbb\xbf"):
        view = view[3:]
    return view.lstrip()[:1] == b"<"


class MetadataClient:
    """Return raw metadata bytes; adapters own encoding and parsing."""

    def __init__(
        self,
        policy: MetadataPolicy | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        attempts: int = MAX_ATTEMPTS,
        backoff: tuple[float, ...] = BACKOFF,
        throttle: Throttle | None = None,
        detector: BlockDetector | None = None,
        transport=None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("元数据请求超时必须为正数")
        self.policy = policy or MetadataPolicy()
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        self.backoff = tuple(backoff)
        self.throttle = throttle or default_throttle()
        self.detector = detector or BlockDetector()
        self.transport = transport or Transport(self.policy, self.throttle, self.detector)

    def close(self) -> None:
        """Compatibility hook; MetadataClient owns no persistent connection."""
        return None

    def get(self, url: str, *, accept: str | None = "text/html,application/xhtml+xml") -> bytes:
        return self._request(url, method="GET", accept=accept)

    def post_form(self, url: str, fields: dict[str, str], *, accept: str | None = "application/json") -> bytes:
        self.policy.check_url(url)
        data = urllib.parse.urlencode(fields).encode("ascii")
        return self._request(url, method="POST", data=data, accept=accept)

    def post_json(self, url: str, payload: dict, *, referer: str | None = None) -> bytes:
        host = self.policy.check_url(url)
        if host != SHANGHAI_HOST:
            raise ComplianceDenied("JSON POST 只允许上海固定元数据查询端点")
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        return self._request(url, method="POST", data=data, accept="application/json", json_body=True,
                             referer=referer, expect_json=True)

    def post_json_dict(self, url: str, payload: dict, *, referer: str | None = None) -> dict:
        raw = self.post_json(url, payload, referer=referer)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ValueError(f"元数据 JSON 响应无法解析：{raw[:160]!r}") from exc
        if not isinstance(value, dict):
            raise ValueError("元数据 JSON 顶层必须为对象")
        return value

    def _request(
        self,
        url: str,
        *,
        method: str,
        data: bytes | None = None,
        accept: str | None,
        json_body: bool = False,
        referer: str | None = None,
        expect_json: bool = False,
    ) -> bytes:
        host = self.policy.check_url(url)
        if host == SHANGHAI_HOST and method != "POST":
            raise ComplianceDenied("上海元数据查询只接受 POST JSON，不跟随改为 GET 的跳转")
        if accept not in (None, "application/json", "text/html", "text/html,application/xhtml+xml"):
            raise ValueError("元数据 Accept 只允许已知查询文本类型")
        last_error: Exception | None = None
        for attempt in range(self.attempts):
            if attempt:
                time.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
            self.throttle.wait(host)
            headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
            if referer:
                headers["Referer"] = referer
            if accept is not None:
                headers["Accept"] = accept
            if data is not None:
                headers["Content-Type"] = "application/json" if json_body else "application/x-www-form-urlencoded"
            if host == SHANGHAI_HOST:
                headers["Origin"] = SHANGHAI_ORIGIN
                headers["Referer"] = SHANGHAI_REFERER
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self.transport.open(req, timeout=self.timeout) as resp:
                    status = getattr(resp, "status", 200)
                    if status != 200:
                        raise SourceUnreachable(f"元数据源返回 HTTP {status}：{url}")
                    final_url = resp.geturl() if hasattr(resp, "geturl") else url
                    self.policy.check_redirect(req, final_url)
                    self.policy.check_headers(resp.headers, url)
                    chunks: list[bytes] = []
                    remaining = self.policy.max_bytes + 1
                    content_type = resp.headers.get("Content-Type", "")
                    try:
                        while remaining > 0:
                            try:
                                chunk = resp.read(remaining)
                            except (http.client.IncompleteRead, ConnectionResetError) as exc:
                                partial = getattr(exc, "partial", b"") or b""
                                if partial:
                                    chunks.append(partial[: max(0, remaining)])
                                raise
                            if not chunk:
                                break
                            chunks.append(chunk)
                            remaining -= len(chunk)
                    except (OSError, http.client.HTTPException) as exc:
                        self.detector.check(b"".join(chunks), content_type, reject_binary=True)
                        raise SourceUnreachable(f"元数据响应读取失败：{url}：{exc}") from exc
                    body = b"".join(chunks)
                    missing = getattr(resp, "length", None)
                    if len(body) <= self.policy.max_bytes and missing is not None and missing > 0:
                        raise SourceUnreachable(f"元数据响应读取失败（响应截断，尚缺 {missing} 字节）：{url}")
                    self.detector.check(body, content_type, reject_binary=True)
                    if expect_json and (
                        content_type.split(";", 1)[0].strip().lower() == "text/html"
                        or _looks_like_html(body)
                    ):
                        raise ComplianceDenied("元数据 JSON 端点返回网页错误页")
                    if len(body) > self.policy.max_bytes:
                        raise NeedsHuman("元数据响应过大，停止解析", hint="检查时效查询端点与响应结构")
                    return body
            except urllib.error.HTTPError as exc:
                try:
                    if exc.fp is not None and not getattr(exc.fp, "closed", False):
                        prefix, _read_error = read_prefix(exc, 65536)
                        self.detector.check(prefix, "text/html", reject_binary=True)
                except (OSError, http.client.HTTPException, ValueError):
                    pass
                finally:
                    with contextlib.suppress(Exception):
                        exc.close()
                retryable = 500 <= exc.code <= 599 or exc.code == 429
                if not retryable or attempt == self.attempts - 1:
                    raise SourceUnreachable(f"元数据源返回 HTTP {exc.code}：{url}") from exc
                last_error = exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                last_error = exc
                if attempt == self.attempts - 1:
                    raise SourceUnreachable(
                        f"元数据源不可达（{url}）：{type(exc).__name__}：{exc}"
                    ) from exc
        if last_error is not None:
            raise SourceUnreachable(f"元数据源不可达（{url}）：{last_error}") from last_error
        raise AssertionError("元数据请求重试循环不应落空")  # pragma: no cover
