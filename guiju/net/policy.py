"""Pure policy decisions; no I/O and no urllib openers belong here."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Mapping

from ..errors import ComplianceDenied, SourceUnreachable
from .endpoints import DEFAULT_METADATA_ENDPOINTS, SHANGHAI_HOST


class BodyPolicy:
    """Policy for body/page requests: gov.cn only, bounded page and file sizes."""

    ALLOWED_SUFFIX = ".gov.cn"

    def __init__(
        self,
        max_page_bytes: int = 8 * 1024 * 1024,
        max_download_bytes: int = 512 * 1024 * 1024,
        *,
        check_url_fn: Callable[[str], str] | None = None,
    ) -> None:
        self.max_page_bytes = int(max_page_bytes)
        self.max_download_bytes = int(max_download_bytes)
        self._check_url_fn = check_url_fn

    def check_url(self, url: str) -> str:
        if self._check_url_fn is not None:
            return self._check_url_fn(url)
        if any(ord(ch) < 32 for ch in url):
            raise ComplianceDenied("URL 含控制字符，拒绝访问")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("https", "http"):
            raise ComplianceDenied(f"仅允许 http(s)：{url}")
        if parts.username is not None or parts.password is not None:
            raise ComplianceDenied("URL 不得携带用户名或密码")
        try:
            port = parts.port
        except ValueError as exc:
            raise ComplianceDenied(f"URL 端口无效：{url}") from exc
        if port not in (None, 80, 443):
            raise ComplianceDenied(f"URL 端口不允许：{url}")
        from .headers import canonical_host
        host = canonical_host(parts.hostname or "")
        if not host:
            raise ComplianceDenied(f"非法 host，拒绝访问：{parts.hostname or '(空)'}")
        if not (host == "gov.cn" or host.endswith(self.ALLOWED_SUFFIX)):
            raise ComplianceDenied(f"非政府域名，拒绝访问：{host or '(空)'}")
        netloc = host if port is None else f"{host}:{port}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

    def check_redirect(self, req, target: str) -> str:
        self.check_url(target)
        from .headers import canonical_host
        host = canonical_host(urllib.parse.urlsplit(target).hostname or "")
        if not host:
            raise ComplianceDenied(f"重定向目标缺少 host：{target}")
        return host

    @classmethod
    def default(cls) -> BodyPolicy:
        return cls()


def normalize_path(path: str) -> str:
    """Equivalent path before allow-list comparison; never a wildcard."""
    segments = (segment.split(";", 1)[0] for segment in path.split("/"))
    return re.sub(r"/{2,}", "/", "/".join(segments)) or "/"


class MetadataPolicy:
    """Policy for approved metadata endpoints only (exact host + normalized path)."""

    def __init__(
        self,
        endpoints: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
        *,
        max_bytes: int = 2_000_000,
        form_hosts: frozenset[str] | None = None,
    ) -> None:
        self.endpoints: dict[str, dict[str, frozenset[str]]] = {
            host: dict(paths) for host, paths in (endpoints or DEFAULT_METADATA_ENDPOINTS).items()
        }
        self.max_bytes = int(max_bytes)
        self.form_hosts = form_hosts

    def check_url(self, url: str) -> str:
        try:
            parts = urllib.parse.urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise ComplianceDenied("元数据 URL 端口无效") from exc
        host = (parts.hostname or "").lower()
        if any(ord(ch) < 32 for ch in url):
            raise ComplianceDenied("元数据 URL 含控制字符")
        if (
            parts.scheme not in ("http", "https")
            or host not in self.endpoints
            or normalize_path(parts.path) not in self.endpoints[host]
            or parts.username is not None
            or parts.password is not None
            or (host == SHANGHAI_HOST and (parts.scheme != "https" or port != 9175))
            or (host != SHANGHAI_HOST and port not in (None, 80 if parts.scheme == "http" else 443))
            or parts.fragment
        ):
            raise ComplianceDenied(f"非已批准元数据查询端点，拒绝访问：{url}")
        return host

    def check_headers(self, headers, url: str, *, redirect: bool = False) -> None:
        ctype = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        disposition = headers.get("Content-Disposition", "").lower()
        parts = urllib.parse.urlsplit(url)
        allowed = self.endpoints[parts.hostname.lower()][normalize_path(parts.path)]
        if "attachment" in disposition or (ctype not in allowed and not (redirect and not ctype)):
            raise ComplianceDenied(f"元数据出口拒绝非查询文本或附件响应：{url}")

    def check_redirect(self, req, target: str) -> str:
        try:
            host = self.check_url(target)
        except ComplianceDenied as exc:
            parts = urllib.parse.urlsplit(target)
            current = urllib.parse.urlsplit(req.full_url)
            if (
                (parts.hostname or "").lower() == (current.hostname or "").lower()
                and normalize_path(parts.path) == "/error/noright.html"
            ):
                raise SourceUnreachable(f"元数据源返回错误页，不跟随：{target}") from exc
            raise
        current_host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
        if host != current_host:
            raise ComplianceDenied("元数据重定向不得跨源，即使目标也是已批准查询端点")
        return host

    @classmethod
    def default(cls) -> MetadataPolicy:
        return cls()
