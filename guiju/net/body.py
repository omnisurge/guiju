"""The only writer of body bytes and the only download implementation."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .._version import __version__
from ..errors import NeedsHuman, SourceUnreachable
from .block import BlockDetector
from .headers import header_int, parse_content_range
from .policy import BodyPolicy
from .request import DownloadResult, ProbeInfo, ProbeRecord, Response
from .throttle import Throttle, default_throttle
from .transport import Transport

UA = f"guiju/{__version__} (compliance-aware; official code fetcher)"


def _strong(value: str) -> bool:
    if not value or value.strip().startswith(("W/", "w/")):
        return False
    return all(32 <= ord(ch) < 127 for ch in value)


def _validator(headers) -> tuple[str, str]:
    return headers.get("ETag", ""), headers.get("Last-Modified", "")


def _meta_path(part: Path) -> Path:
    return part.with_name(part.name + ".meta")


def _read_meta(part: Path) -> dict:
    path = _meta_path(part)
    try:
        raw = json.loads(path.read_text("utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_meta(part: Path, *, etag: str, last_modified: str, total: int | None, size: int) -> None:
    path = _meta_path(part)
    tmp = path.with_name(path.name + ".tmp")
    payload = {
        "etag": etag,
        "last_modified": last_modified,
        "total": total,
        "size": size,
        "updated_at": int(time.time()),
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _unlink_meta(part: Path) -> None:
    _meta_path(part).unlink(missing_ok=True)


def finalize(part_path: str | Path, dest_path: str | Path) -> None:
    """The sole body-file placement point: atomic rename after caller checks SHA."""
    os.replace(Path(part_path), Path(dest_path))


class BodyClient:
    def __init__(
        self,
        policy: BodyPolicy | None = None,
        *,
        throttle: Throttle | None = None,
        detector: BlockDetector | None = None,
        transport=None,
        probe_sink=None,
    ) -> None:
        self.policy = policy or BodyPolicy()
        self.throttle = throttle or default_throttle()
        self.detector = detector or BlockDetector()
        self.transport = transport or Transport(self.policy, self.throttle, self.detector)
        self.probe_sink = probe_sink or (lambda record: None)
        self._last_content_type = ""

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {"User-Agent": UA, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        return headers

    def _open(self, url: str, *, referer=None, headers=None, timeout=60):
        req = urllib.request.Request(url, headers=headers or self._headers(referer), method="GET")
        return self.transport.open(req, timeout=timeout)

    def get_page(
        self,
        url: str,
        *,
        referer: str | None = None,
        timeout: int = 60,
        attempts: int = 2,
        max_bytes: int | None = None,
    ) -> Response:
        self.policy.check_url(url)
        limit = self.policy.max_page_bytes if max_bytes is None else max_bytes
        for attempt in range(max(1, attempts)):
            self.throttle.wait(urllib.parse.urlsplit(url).hostname or "")
            try:
                with self._open(url, referer=referer, timeout=timeout) as resp:
                    status = getattr(resp, "status", 200)
                    if status != 200:
                        raise SourceUnreachable(f"源返回 HTTP {status}：{url}")
                    content_length = header_int(resp.headers.get("Content-Length"))
                    chunked = "chunked" in resp.headers.get("Transfer-Encoding", "").lower()
                    chunks: list[bytes] = []
                    remaining = limit + 1
                    checked = False
                    while remaining > 0:
                        chunk = resp.read(min(65536, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                        if not checked:
                            self.detector.check(b"".join(chunks)[:65536],
                                                resp.headers.get("Content-Type", ""))
                            checked = True
                    body = b"".join(chunks)
                    if len(body) > limit:
                        raise NeedsHuman("页面响应过大，停止解析", hint="检查页面是否被反爬替换")
                    if chunked and content_length is not None and len(body) != content_length:
                        raise SourceUnreachable(f"Content-Length 与 chunked 实际长度不一致：{url}")
                    if not checked:
                        self.detector.check(body[:65536], resp.headers.get("Content-Type", ""))
                    missing = getattr(resp, "length", None)
                    if missing is not None and missing > 0:
                        raise SourceUnreachable(f"响应读取失败（尚缺 {missing} 字节）：{url}")
                    return Response(
                        url=resp.geturl() if hasattr(resp, "geturl") else url,
                        body=body,
                        content_type=resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower(),
                        status=status,
                        headers=dict(resp.headers.items()) if hasattr(resp.headers, "items") else {},
                        probe=ProbeRecord(url=url, status=status, bytes=len(body), outcome="ok"),
                    )
            except urllib.error.HTTPError as exc:
                self._check_error_body(exc, url)
                if 500 <= exc.code <= 599 and attempt + 1 < attempts:
                    continue
                raise SourceUnreachable(f"源返回 HTTP {exc.code}：{url}") from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                if attempt + 1 >= attempts:
                    raise SourceUnreachable(f"源不可达（{url}）：{type(exc).__name__}：{exc}") from exc
        raise AssertionError("页面请求重试循环不应落空")  # pragma: no cover

    def download(
        self,
        url: str,
        part_path: str | Path,
        *,
        referer: str | None = None,
        timeout: int = 180,
        attempts: int = 2,
        chunk_size: int = 1 << 16,
    ) -> DownloadResult:
        self.policy.check_url(url)
        part = Path(part_path)
        part.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = self.policy.max_download_bytes
        sidecar = _read_meta(part)
        part_size = part.stat().st_size if part.exists() else 0
        if part_size > max_bytes:
            part.unlink(missing_ok=True)
            _unlink_meta(part)
            part_size = 0
        strong = _strong(sidecar.get("etag", "")) or _strong(sidecar.get("last_modified", ""))
        if part_size and not strong:
            part.unlink(missing_ok=True)
            _unlink_meta(part)
            part_size = 0
        resume = part_size > 0 and strong
        restarted = False
        attempt = 0
        while True:
            attempt += 1
            if attempt > max(1, attempts):
                raise SourceUnreachable(f"下载失败（{url}）")
            headers = self._headers(referer)
            if resume:
                validator = sidecar.get("etag") if _strong(sidecar.get("etag", "")) else sidecar.get("last_modified", "")
                headers["Range"] = f"bytes={part_size}-"
                if validator:
                    headers["If-Range"] = validator
            self.throttle.wait(urllib.parse.urlsplit(url).hostname or "")
            try:
                with self._open(url, referer=referer, headers=headers, timeout=timeout) as resp:
                    status = getattr(resp, "status", 200)
                    ctype = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    content_length = header_int(resp.headers.get("Content-Length"))
                    chunked = "chunked" in resp.headers.get("Transfer-Encoding", "").lower()
                    if status == 200:
                        self._last_content_type = ctype
                        resume = False
                        if content_length is not None and content_length > max_bytes:
                            raise NeedsHuman("正文超过 512MiB 上限", hint="拒绝超限正文")
                        if content_length is None and not chunked:
                            raise SourceUnreachable(f"响应既无 Content-Length 也无 chunked：{url}")
                        written = self._write_response(resp, part, resume=False, max_bytes=max_bytes)
                        if chunked and content_length is not None and written != content_length:
                            part.unlink(missing_ok=True)
                            _unlink_meta(part)
                            raise SourceUnreachable(f"Content-Length 与 chunked 实际长度不一致：{url}")
                        missing = getattr(resp, "length", None)
                        if missing is not None and missing > 0:
                            raise SourceUnreachable(f"响应读取失败（响应截断，尚缺 {missing} 字节）：{url}")
                        _write_meta(part, etag=resp.headers.get("ETag", ""),
                                    last_modified=resp.headers.get("Last-Modified", ""),
                                    total=written, size=written)
                        _unlink_meta(part)
                        return DownloadResult(url=url, bytes=written, resumed_from=0, status=200,
                                               etag=resp.headers.get("ETag", ""),
                                               last_modified=resp.headers.get("Last-Modified", ""))
                    if status == 206:
                        self._last_content_type = ctype
                        start, end, total = parse_content_range(resp.headers.get("Content-Range"))
                        if start is None or end is None:
                            raise SourceUnreachable(f"206 响应缺少有效 Content-Range：{url}")
                        if total is None:
                            raise SourceUnreachable(f"206 响应总长未知（*）：{url}")
                        if total > max_bytes:
                            raise NeedsHuman("正文超过 512MiB 上限", hint="拒绝超限正文")
                        if content_length is not None and content_length != end - start + 1:
                            raise SourceUnreachable(f"206 Content-Length 与 Content-Range 不一致：{url}")
                        if resume:
                            if start != part_size:
                                raise SourceUnreachable(f"206 起点与本地 .part 不一致：{start} != {part_size}")
                            sent_etag, sent_lm = _validator(resp.headers)
                            expected_etag = sidecar.get("etag", "")
                            expected_lm = sidecar.get("last_modified", "")
                            matches = bool(
                                (expected_etag and sent_etag and sent_etag == expected_etag)
                                or (expected_lm and sent_lm and sent_lm == expected_lm)
                            )
                            if not matches:
                                part.unlink(missing_ok=True)
                                _unlink_meta(part)
                                part_size = 0
                                resume = False
                                if not restarted:
                                    restarted = True
                                    continue
                                raise SourceUnreachable(f"206 验证器与 .part 不匹配：{url}")
                        elif start != 0:
                            raise SourceUnreachable(f"206 起点不为 0：{start}")
                        written = self._write_response(resp, part, resume=resume, max_bytes=max_bytes)
                        if written != end + 1 or (total is not None and written != total):
                            raise SourceUnreachable(f"206 响应长度不完整：{written} != {end + 1}/{total}")
                        _write_meta(part, etag=resp.headers.get("ETag", ""),
                                    last_modified=resp.headers.get("Last-Modified", ""),
                                    total=total, size=written)
                        _unlink_meta(part)
                        return DownloadResult(url=url, bytes=written, resumed_from=part_size if resume else 0,
                                               status=206, etag=resp.headers.get("ETag", ""),
                                               last_modified=resp.headers.get("Last-Modified", ""))
                    if status == 416:
                        if not resume or part_size <= 0:
                            raise SourceUnreachable(f"416 但本地没有可验证的 .part：{url}")
                        probe = self.transport.probe(url, timeout=min(float(timeout), 20.0)) or ProbeInfo()
                        probe_etag, probe_lm = probe.etag, probe.last_modified
                        valid = (
                            probe.content_length == part_size
                            and ((_strong(probe_etag) and _strong(sidecar.get("etag", "")) and probe_etag == sidecar.get("etag"))
                                 or (_strong(probe_lm) and _strong(sidecar.get("last_modified", "")) and probe_lm == sidecar.get("last_modified")))
                        )
                        if valid:
                            _unlink_meta(part)
                            return DownloadResult(url=url, bytes=part_size, resumed_from=part_size, status=416,
                                                   etag=probe_etag, last_modified=probe_lm)
                        part.unlink(missing_ok=True)
                        _unlink_meta(part)
                        part_size = 0
                        resume = False
                        if not restarted:
                            restarted = True
                            continue
                        raise SourceUnreachable(f"416 验证器或长度不匹配：{url}")
                    raise SourceUnreachable(f"下载返回 HTTP {status}：{url}")
            except urllib.error.HTTPError as exc:
                self._check_error_body(exc, url)
                if exc.code == 416:
                    if resume and part_size > 0:
                        probe = self.transport.probe(url, timeout=min(float(timeout), 20.0)) or ProbeInfo()
                        valid = (
                            probe.content_length == part_size
                            and ((_strong(probe.etag) and _strong(sidecar.get("etag", "")) and probe.etag == sidecar.get("etag"))
                                 or (_strong(probe.last_modified) and _strong(sidecar.get("last_modified", ""))
                                     and probe.last_modified == sidecar.get("last_modified")))
                        )
                        if valid:
                            _unlink_meta(part)
                            return DownloadResult(url=url, bytes=part_size, resumed_from=part_size, status=416,
                                                   etag=probe.etag, last_modified=probe.last_modified)
                        part.unlink(missing_ok=True)
                        _unlink_meta(part)
                        part_size = 0
                        resume = False
                        if not restarted:
                            restarted = True
                            continue
                    raise SourceUnreachable(f"下载返回 HTTP 416：{url}") from exc
                if 500 <= exc.code <= 599 and attempt < attempts:
                    continue
                raise SourceUnreachable(f"下载返回 HTTP {exc.code}：{url}") from exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                if attempt < attempts:
                    continue
                raise SourceUnreachable(f"下载不可达（{url}）：{type(exc).__name__}：{exc}") from exc

    def _check_error_body(self, exc: urllib.error.HTTPError, url: str) -> None:
        try:
            body = exc.read(65536)
        except (OSError, http.client.HTTPException, ValueError):
            return
        finally:
            with contextlib.suppress(Exception):
                exc.close()
        ctype = ""
        with contextlib.suppress(Exception):
            ctype = exc.headers.get("Content-Type", "")
        self.detector.check(body[:65536], ctype)

    def _write_response(self, resp, part: Path, *, resume: bool, max_bytes: int) -> int:
        mode = "ab" if resume else "wb"
        start_size = part.stat().st_size if resume and part.exists() else 0
        written = start_size
        pending = b""
        if not resume:
            # Check the first chunk before truncating an existing .part: a block
            # page must never destroy a previously valid partial body.
            pending = resp.read(1 << 16)
            if pending:
                self.detector.check(pending[:65536], resp.headers.get("Content-Type", ""))
        with open(part, mode) as fp:
            if pending:
                written += len(pending)
                if written > max_bytes:
                    fp.close()
                    part.unlink(missing_ok=True)
                    _unlink_meta(part)
                    raise NeedsHuman("正文超过 512MiB 上限", hint="拒绝超限正文")
                fp.write(pending)
            first = True
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                if first and resume:
                    self.detector.check(chunk[:65536], resp.headers.get("Content-Type", ""))
                    first = False
                written += len(chunk)
                if written > max_bytes:
                    if resume:
                        fp.truncate(start_size)
                    else:
                        fp.close()
                        part.unlink(missing_ok=True)
                        _unlink_meta(part)
                    raise NeedsHuman("正文超过 512MiB 上限", hint="拒绝超限正文")
                fp.write(chunk)
        return written

    def download_bytes(
        self,
        url: str,
        *,
        referer: str | None = None,
        timeout: int = 180,
        max_bytes: int = 128 * 1024 * 1024,
    ) -> tuple[bytes, str]:
        with tempfile.TemporaryDirectory() as tmp:
            part = Path(tmp) / "body.part"
            policy = BodyPolicy(
                max_page_bytes=self.policy.max_page_bytes,
                max_download_bytes=max_bytes,
                check_url_fn=self.policy._check_url_fn,
            )
            client = BodyClient(policy=policy, throttle=self.throttle, detector=self.detector,
                                transport=self.transport, probe_sink=self.probe_sink)
            client.download(url, part, referer=referer, timeout=timeout)
            return part.read_bytes(), client._last_content_type or "application/octet-stream"

    def finalize(self, part_path: str | Path, dest_path: str | Path) -> None:
        finalize(part_path, dest_path)
