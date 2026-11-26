"""Offline real-HTTP fixtures for the net characterization target & regression suites.

This module intentionally depends only on the standard library so socket-backed
test servers stay decoupled from guiju.net implementation details.
"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import socket
import threading
from dataclasses import dataclass, field


@dataclass
class RequestRecord:
    method: str
    path: str
    headers: dict[str, str]


@dataclass
class ServerState:
    mode: str
    body: bytes = b""
    content_type: str = "application/pdf"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    requests: list[RequestRecord] = field(default_factory=list)
    first_sent: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    chunk_size: int = 5


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GuijuCharacterization/1"

    def log_message(self, fmt, *args):  # silence test output
        return

    @property
    def state(self) -> ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def _record(self) -> None:
        self.state.requests.append(
            RequestRecord(
                method=self.command,
                path=self.path,
                headers={k.lower(): v for k, v in self.headers.items()},
            )
        )

    def _send_headers(self, status: int, headers: dict[str, str]) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()

    def do_HEAD(self) -> None:
        self._record()
        st = self.state
        if st.mode in ("head_405", "http_416_head_405"):
            self.send_response(405)
            self.end_headers()
            return
        if st.mode == "head_500":
            self.send_response(500)
            self.end_headers()
            return
        if st.mode in ("head_redirect", "head_loop"):
            self.send_response(302)
            self.send_header("Location", "/loop" if st.mode == "head_loop" else "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        headers = {"Content-Type": st.content_type, **st.headers}
        if st.mode in ("head_ok", "ok", "truncated", "range_ok", "http_416", "http_416_head_405", "slow_stream"):
            headers.setdefault("Content-Length", str(len(st.body)))
        self._send_headers(200, headers)

    def do_GET(self) -> None:
        self._record()
        st = self.state
        mode = st.mode
        if mode == "redirect":
            if self.path.endswith("/final"):
                headers = {"Content-Type": st.content_type, **st.headers, "Content-Length": str(len(st.body))}
                self._send_headers(200, headers)
                self.wfile.write(st.body)
                return
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "redirect_loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "two_stage_no_validator":
            if len(st.requests) == 1:
                self.send_response(200)
                self.send_header("Content-Type", st.content_type)
                self.send_header("Content-Length", str(len(st.body)))
                self.end_headers()
                self.wfile.write(st.body[:5])
                self.wfile.flush()
                self.close_connection = True
                return
            self._send_headers(200, {"Content-Type": st.content_type, "Content-Length": str(len(st.body))})
            self.wfile.write(st.body)
            return
        if mode == "two_stage":
            if len(st.requests) == 1:
                self.send_response(200)
                self.send_header("Content-Type", st.content_type)
                self.send_header("Content-Length", str(len(st.body)))
                for key, value in st.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(st.body[:5])
                self.wfile.flush()
                self.close_connection = True
                return
            self._range_response("range_ok")
            return
        if mode == "slow_stream":
            self.send_response(200)
            self.send_header("Content-Type", st.content_type)
            self.send_header("Content-Length", str(len(st.body)))
            self.end_headers()
            self.wfile.write(st.body[:3])
            self.wfile.flush()
            st.first_sent.set()
            st.release.wait(timeout=5)
            self.wfile.write(st.body[3:])
            return
        if mode in ("http_416", "http_416_head_405"):
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode in ("http_403", "http_429", "http_500"):
            code = int(mode.rsplit("_", 1)[1])
            body = "验证码".encode() if code == 403 else b""
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if mode == "chunked":
            self.send_response(200)
            self.send_header("Content-Type", st.content_type)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(st.body), st.chunk_size):
                piece = st.body[start : start + st.chunk_size]
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if mode == "chunked_truncated":
            self.send_response(200)
            self.send_header("Content-Type", st.content_type)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(f"{len(st.body[:3]):x}\r\n".encode() + st.body[:3] + b"\r\n")
            self.wfile.flush()
            self.close_connection = True
            return
        if mode == "close_delimited":
            self.send_response(200)
            self.send_header("Content-Type", st.content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(st.body)
            self.close_connection = True
            return
        if mode in ("range_ok", "range_missing", "range_wrong_start", "range_star",
                    "range_short", "range_extra", "range_upper", "range_no_cl",
                    "range_bad_etag", "range_cl_mismatch", "range_truncate_200"):
            self._range_response(mode)
            return
        # normal 200 modes
        headers = {"Content-Type": st.content_type, **st.headers}
        if mode not in ("truncated", "cl_te"):
            headers.setdefault("Content-Length", str(len(st.body)))
        if mode == "cl_extra":
            headers["Content-Length"] = str(max(0, len(st.body) - 2))
        if mode == "cl_te":
            headers["Transfer-Encoding"] = "chunked"
            headers["Content-Length"] = str(len(st.body) + 5)
        self._send_headers(200, headers)
        if mode == "truncated":
            self.wfile.write(st.body[: st.chunk_size])
            self.wfile.flush()
            self.close_connection = True
            return
        if mode == "cl_te":
            self.wfile.write(f"{len(st.body):x}\r\n".encode() + st.body + b"\r\n0\r\n\r\n")
            return
        if st.body:
            self.wfile.write(st.body)

    def _range_response(self, mode: str) -> None:
        st = self.state
        total = len(st.body)
        range_header = self.headers.get("Range", "")
        start = 0
        if range_header.startswith("bytes="):
            raw = range_header[6:].split("-", 1)[0]
            if raw.isdigit():
                start = int(raw)
        end = total - 1
        headers = {"Content-Type": st.content_type, **st.headers}
        if mode == "range_truncate_200":
            headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            headers["Content-Length"] = str(end - start + 1)
            headers.setdefault("ETag", '"truncate-v1"')
            self._send_headers(206, headers)
            if len(st.requests) == 1:
                self.wfile.write(st.body[start : start + 200])
                self.wfile.flush()
                self.close_connection = True
            else:
                self.wfile.write(st.body[start:])
            return
        if mode == "range_missing":
            headers["Content-Length"] = str(total - start)
            self._send_headers(206, headers)
            self.wfile.write(st.body[start:])
            return
        if mode == "range_wrong_start":
            start = max(0, start - 2)
        content_range = f"bytes {start}-{end}/*" if mode == "range_star" else f"bytes {start}-{end}/{total}"
        if mode == "range_upper":
            content_range = f"Bytes {start}-{end}/{total}"
        headers["Content-Range"] = content_range
        if mode == "range_no_cl":
            pass
        elif mode == "range_short":
            headers["Content-Length"] = str(max(0, total - start - 2))
        elif mode == "range_extra":
            headers["Content-Length"] = str(total - start + 2)
        elif mode == "range_bad_etag":
            headers["ETag"] = '"different"'
            headers["Content-Length"] = str(total - start)
        elif mode == "range_cl_mismatch":
            headers["Content-Length"] = str(max(0, total - start - 1))
        else:
            headers["Content-Length"] = str(total - start)
        self._send_headers(206, headers)
        if mode == "range_short":
            self.wfile.write(st.body[start : start + max(0, total - start - 2)])
        elif mode == "range_extra":
            self.wfile.write(st.body[start:] + b"xx")
        else:
            self.wfile.write(st.body[start:])


class LocalServer:
    def __init__(self, mode: str, body: bytes = b"", *, content_type: str = "application/pdf", status: int = 200, headers: dict[str, str] | None = None, chunk_size: int = 5):
        self.state = ServerState(
            mode=mode,
            body=body,
            content_type=content_type,
            status=status,
            headers=headers or {},
            chunk_size=chunk_size,
        )
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def __enter__(self) -> LocalServer:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def make_response(body: bytes, headers: dict[str, str] | None = None, status: int = 200) -> http.client.HTTPResponse:
    """Create a real http.client.HTTPResponse over a socketpair.

    The response has genuine read(amt), length, chunked and IncompleteRead
    semantics; it is not a list-backed fake.
    """
    left, right = socket.socketpair()
    raw = f"HTTP/1.1 {status} X\r\n".encode()
    for key, value in (headers or {}).items():
        raw += f"{key}: {value}\r\n".encode()
    raw += b"\r\n" + body
    right.sendall(raw)
    right.shutdown(socket.SHUT_WR)
    left.settimeout(2)
    return http.client.HTTPResponse(left)


@contextlib.contextmanager
def no_proxy():
    import os
    saved = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
