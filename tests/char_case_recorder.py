"""Shared real case recorder for the characterization target suite.

The recorder is intentionally stdlib-only (it imports the local HTTP fixture,
never guiju.net).  A backend adapter supplies download/get/policy operations
for the guiju.net facade under test.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from tests.char_baseline_harness import LocalServer

HEADER_KEYS = (
    "user-agent",
    "accept",
    "accept-encoding",
    "range",
    "if-range",
    "content-type",
    "referer",
)
BODY_200 = b"%PDF-1.7\n" + b"x" * 20
BODY_206 = b"%PDF-1.7\n" + b"0123456789" * 100
BODY_IFR = b"%PDF-12345"


def _headers(server: LocalServer) -> dict[str, str]:
    if not server.state.requests:
        return {}
    headers = server.state.requests[0].headers
    selected = {key: headers[key] for key in HEADER_KEYS if key in headers}
    # urllib's default HEAD UA embeds the Python version; it is not part of the product contract.
    if selected.get("user-agent", "").startswith("Python-urllib/"):
        selected.pop("user-agent")
    return selected


def _run(fn):
    try:
        return "ok", 0, fn()
    except SourceUnreachable:
        return "source_unreachable", 20, None
    except ComplianceDenied:
        return "compliance_denied", 40, None
    except NeedsHuman:
        return "needs_human", 50, None
    except Exception as exc:  # record the real class instead of hiding it
        return "error:" + type(exc).__name__, 1, None


def _part_size(part: Path) -> int:
    return part.stat().st_size if part.exists() else 0


def _result_bytes(result) -> int:
    if result is None:
        return 0
    if isinstance(result, dict):
        return int(result.get("bytes", 0))
    return int(getattr(result, "bytes", 0))


def _result_body_bytes(result) -> int:
    if result is None:
        return 0
    body = result.get("body") if isinstance(result, dict) else getattr(result, "body", None)
    return len(body) if body is not None else 0


def _metrics(*, mode: str, case_id: str, headers: dict[str, str], outcome: str, exit_code: int,
             part: Path | None, final_exists: bool, request_count: int, sleeps: list[float],
             body_bytes: int = 0) -> dict:
    return {
        "mode": mode,
        "url_shape": "/" + case_id,
        "request_headers": headers,
        "outcome": outcome,
        "exit": exit_code,
        "body_read_bytes": body_bytes,
        "part_size": _part_size(part) if part is not None else 0,
        "final_exists": final_exists,
        "request_count": request_count,
        "sleeps": sleeps,
    }


def _seed_meta(part: Path, *, total: int, size: int, etag: str = "", last_modified: str = "") -> None:
    Path(str(part) + ".meta").write_text(json.dumps({
        "etag": etag, "last_modified": last_modified, "total": total, "size": size,
    }), encoding="utf-8")


def _download(backend, server: LocalServer, case_id: str, part: Path):
    outcome, exit_code, result = _run(lambda: backend.download(server.url + "/" + case_id, part, attempts=1))
    return outcome, exit_code, result


def record_case(case_id: str, backend) -> dict:
    if case_id.startswith("CHAR-200-"):
        return _record_a(case_id, backend)
    if case_id.startswith("CHAR-206-"):
        return _record_b(case_id, backend)
    if case_id.startswith("CHAR-416-"):
        return _record_c(case_id, backend)
    if case_id.startswith("CHAR-IFR-"):
        return _record_d(case_id, backend)
    if case_id.startswith("CHAR-RED-"):
        return _record_e(case_id, backend)
    if case_id.startswith("CHAR-IDN-"):
        return _record_f(case_id, backend)
    if case_id.startswith("CHAR-CAP-"):
        return _record_g(case_id, backend)
    if case_id.startswith("CHAR-BLK-"):
        return _record_h(case_id, backend)
    if case_id.startswith(("CHAR-403-", "CHAR-429-")):
        return _record_i(case_id, backend)
    if case_id.startswith("CHAR-THR-"):
        return _record_j(case_id, backend)
    if case_id in ("CHAR-PRB-01", "CHAR-PRB-02", "CHAR-PRB-03", "CHAR-HEAD-01", "CHAR-PROXY-01", "CHAR-FIN-01"):
        return _record_k(case_id, backend)
    raise AssertionError(f"unhandled case {case_id}")


def _record_a(case_id: str, backend) -> dict:
    mode = {
        "CHAR-200-02": "truncated",
        "CHAR-200-03": "close_delimited",
        "CHAR-200-04": "chunked",
        "CHAR-200-05": "chunked_truncated",
        "CHAR-200-06": "cl_extra",
        "CHAR-200-07": "cl_te",
    }.get(case_id, "ok")
    part = Path(tempfile.mkdtemp(prefix="guiju-c0-")) / "case.part"
    try:
        with LocalServer(mode, body=BODY_200) as server:
            outcome, exit_code, _ = _download(backend, server, case_id, part)
            return _metrics(mode="local_server", case_id=case_id, headers=_headers(server),
                            outcome=outcome, exit_code=exit_code, part=part, final_exists=False,
                            request_count=len(server.state.requests), sleeps=[],
                            body_bytes=_part_size(part))
    finally:
        shutil.rmtree(part.parent, ignore_errors=True)


def _record_b(case_id: str, backend) -> dict:
    mode = {
        "CHAR-206-02": "range_missing",
        "CHAR-206-03": "range_wrong_start",
        "CHAR-206-04": "range_star",
        "CHAR-206-05": "range_short",
        "CHAR-206-06": "range_extra",
        "CHAR-206-07": "range_upper",
        "CHAR-206-08": "range_no_cl",
        "CHAR-206-09": "range_bad_etag",
        "CHAR-206-10": "range_cl_mismatch",
    }.get(case_id, "range_ok")
    part = Path(tempfile.mkdtemp(prefix="guiju-c0-")) / "case.part"
    try:
        if case_id not in ("CHAR-206-07", "CHAR-206-08"):
            part.write_bytes(BODY_206[:500])
            if getattr(backend, "sidecar", False):
                _seed_meta(part, total=len(BODY_206), size=500, etag='"v1"')
        headers = {"ETag": '"v1"'}
        with LocalServer(mode, body=BODY_206, headers=headers) as server:
            outcome, exit_code, _ = _download(backend, server, case_id, part)
            return _metrics(mode="local_server", case_id=case_id, headers=_headers(server),
                            outcome=outcome, exit_code=exit_code, part=part, final_exists=False,
                            request_count=len(server.state.requests), sleeps=[],
                            body_bytes=_part_size(part))
    finally:
        shutil.rmtree(part.parent, ignore_errors=True)


def _record_c(case_id: str, backend) -> dict:
    part = Path(tempfile.mkdtemp(prefix="guiju-c0-")) / "case.part"
    try:
        if case_id != "CHAR-416-04":
            part.write_bytes(b"x" * 500)
            if getattr(backend, "sidecar", False):
                _seed_meta(part, total=500, size=500, etag='"v1"')
        mode = "http_416"
        body = b"x" * 500
        if case_id == "CHAR-416-02":
            body = b"x" * 400
            if getattr(backend, "sidecar", False):
                _seed_meta(part, total=400, size=500, etag='"v1"')
        elif case_id == "CHAR-416-03":
            mode = "http_416_head_405"
        elif case_id == "CHAR-416-05" and getattr(backend, "sidecar", False):
            part.unlink(missing_ok=True)
            part.write_bytes(b"x" * 500)
        with LocalServer(mode, body=body, headers={"ETag": '"v1"'}) as server:
            outcome, exit_code, _ = _download(backend, server, case_id, part)
            return _metrics(mode="http_416", case_id=case_id, headers=_headers(server),
                            outcome=outcome, exit_code=exit_code, part=part, final_exists=False,
                            request_count=len(server.state.requests), sleeps=[],
                            body_bytes=_part_size(part))
    finally:
        shutil.rmtree(part.parent, ignore_errors=True)


def _record_d(case_id: str, backend) -> dict:
    part = Path(tempfile.mkdtemp(prefix="guiju-c0-")) / "case.part"
    try:
        part.write_bytes(BODY_IFR[:5])
        if getattr(backend, "sidecar", False):
            n = int(case_id.rsplit("-", 1)[1])
            if n == 2:
                _seed_meta(part, total=len(BODY_IFR), size=5, last_modified="Wed, 01 Jan 2025 00:00:00 GMT")
            elif n in (4, 5, 6):
                pass
            else:
                _seed_meta(part, total=len(BODY_IFR), size=5, etag='"v1"')
        headers = {} if case_id == "CHAR-IFR-07" else {"ETag": '"v1"'}
        with LocalServer("range_ok", body=BODY_IFR, headers=headers) as server:
            outcome, exit_code, _ = _download(backend, server, case_id, part)
            return _metrics(mode="local_server", case_id=case_id, headers=_headers(server),
                            outcome=outcome, exit_code=exit_code, part=part, final_exists=False,
                            request_count=len(server.state.requests), sleeps=[],
                            body_bytes=_part_size(part))
    finally:
        shutil.rmtree(part.parent, ignore_errors=True)


def _record_e(case_id: str, backend) -> dict:
    n = int(case_id.rsplit("-", 1)[1])
    mode = {
        1: "redirect",
        2: "redirect",
        3: "redirect_loop",
        4: "redirect_loop",
        5: "redirect_loop",
        6: "redirect_loop",
        7: "truncated",
        8: "redirect",
    }[n]
    with LocalServer(mode, body=b"%PDF") as server:
        outcome, exit_code, result = _run(lambda: backend.get(server.url + "/" + case_id, attempts=1))
        return _metrics(mode="local_server_redirect", case_id=case_id, headers=_headers(server),
                        outcome=outcome, exit_code=exit_code, part=None, final_exists=False,
                        request_count=len(server.state.requests), sleeps=[],
                        body_bytes=_result_body_bytes(result))


def _record_f(case_id: str, backend) -> dict:
    urls = {
        "CHAR-IDN-01": "https://xn--fiqs8s.gov.cn/a",
        "CHAR-IDN-02": "https://中国.gov.cn/a",
        "CHAR-IDN-03": "https://xn--fiqs8s.gov.cn.evil.com/",
        "CHAR-IDN-04": "https://-bad.gov.cn/a",
        "CHAR-IDN-05": "https://中国.gov.cn/a",
    }
    outcome, exit_code, _ = _run(lambda: backend.check_url(urls[case_id]))
    return _metrics(mode="policy", case_id=case_id, headers={}, outcome=outcome, exit_code=exit_code,
                    part=None, final_exists=False, request_count=0, sleeps=[])


def _record_g(case_id: str, backend) -> dict:
    part = Path(tempfile.mkdtemp(prefix="guiju-c0-")) / "case.part"
    try:
        if case_id == "CHAR-CAP-01":
            body = b"x" * (9 * 1024 * 1024)
            with LocalServer("ok", body=body, content_type="text/html") as server:
                outcome, exit_code, result = _run(lambda: backend.get(server.url + "/" + case_id, attempts=1))
                return _metrics(mode="local_server_large", case_id=case_id, headers=_headers(server),
                                outcome=outcome, exit_code=exit_code, part=None, final_exists=False,
                                request_count=len(server.state.requests), sleeps=[],
                                body_bytes=_result_body_bytes(result))
        if case_id == "CHAR-CAP-02":
            server = LocalServer("truncated", body=b"%PDF" + b"x" * 100, headers={"Content-Length": str(600 * 1024 * 1024)})
        elif case_id == "CHAR-CAP-03":
            server = LocalServer("chunked", body=b"%PDF" + b"x" * 1024)
        elif case_id == "CHAR-CAP-04":
            server = LocalServer("ok", body=b"x", content_type="application/json", headers={"Content-Length": "2000001"})
        elif case_id == "CHAR-CAP-05":
            part.write_bytes(b"x" * 5)
            if getattr(backend, "sidecar", False):
                _seed_meta(part, total=1004, size=5, etag='"v1"')
            server = LocalServer("range_ok", body=b"%PDF" + b"x" * 1000)
        else:
            body = b"%PDF-1.7\n" + b"x" * (9 * 1024 * 1024)
            server = LocalServer("ok", body=body)
        with server:
            if case_id == "CHAR-CAP-04":
                outcome, exit_code, result = _run(lambda: backend.get(server.url + "/" + case_id, attempts=1))
                body_bytes = _result_body_bytes(result)
            else:
                outcome, exit_code, _ = _download(backend, server, case_id, part)
                body_bytes = _part_size(part)
            return _metrics(mode="local_server_large", case_id=case_id, headers=_headers(server),
                            outcome=outcome, exit_code=exit_code, part=part, final_exists=False,
                            request_count=len(server.state.requests), sleeps=[], body_bytes=body_bytes)
    finally:
        shutil.rmtree(part.parent, ignore_errors=True)


def _record_h(case_id: str, backend) -> dict:
    n = int(case_id.rsplit("-", 1)[1])
    body = b"x" * 70000 + "验证码".encode()
    if n == 1:
        body = "验证码".encode()
    elif n == 2:
        body = b"x" * 9000 + "验证码".encode()
    elif n == 3:
        body = "验证码".encode() + b"x" * 2_100_000
    elif n == 4:
        body = "访问过于频繁".encode("gbk")
    elif n == 5:
        body = b"&#x9A8C;&#x8BC1;&#x7801;"
    elif n == 8:
        body = "<html>验证码</html>".encode()
    elif n == 9:
        body = br'{"error":"\u9a8c\u8bc1\u7801"}'
    elif n == 10:
        body = b"%PDF-1.7\n" + "<html>验证码</html>".encode()
    elif n == 11:
        body = b"x" * 65536 + "验证码".encode()
    ctype = "application/json" if n == 9 else ("text/html" if n != 8 else "application/octet-stream")
    outcome, exit_code, _ = _run(lambda: backend.block_check(body, ctype, reject_binary=(n == 10)))
    return _metrics(mode="block", case_id=case_id, headers={}, outcome=outcome, exit_code=exit_code,
                    part=None, final_exists=False, request_count=0, sleeps=[])


def _record_i(case_id: str, backend) -> dict:
    mode = "http_403" if case_id.startswith("CHAR-403") else "http_429"
    with LocalServer(mode) as server:
        outcome, exit_code, result = _run(lambda: backend.get(server.url + "/" + case_id, attempts=1))
        return _metrics(mode="local_server_error", case_id=case_id, headers=_headers(server),
                        outcome=outcome, exit_code=exit_code, part=None, final_exists=False,
                        request_count=len(server.state.requests), sleeps=[],
                        body_bytes=_result_body_bytes(result))


def _record_j(case_id: str, backend) -> dict:
    sleeps = backend.throttle_sleeps(case_id)
    return _metrics(mode="throttle", case_id=case_id, headers={}, outcome="ok", exit_code=0,
                    part=None, final_exists=False, request_count=0, sleeps=sleeps)


def _record_k(case_id: str, backend) -> dict:
    if case_id == "CHAR-PRB-01":
        part = Path(tempfile.mkdtemp(prefix="guiju-c0-")) / "case.part"
        try:
            with LocalServer("ok", body=b"%PDF-1.7\n") as server:
                outcome, exit_code, _ = _download(backend, server, case_id, part)
                return _metrics(mode="probe", case_id=case_id, headers=_headers(server), outcome=outcome,
                                exit_code=exit_code, part=part, final_exists=False,
                                request_count=len(server.state.requests), sleeps=[], body_bytes=_part_size(part))
        finally:
            shutil.rmtree(part.parent, ignore_errors=True)
    if case_id == "CHAR-PRB-02":
        part = Path(tempfile.mkdtemp(prefix="guiju-c0-")) / "case.part"
        try:
            with LocalServer("truncated", body=b"%PDF-1.7\n" * 3, headers={"Content-Length": str(len(b"%PDF-1.7\n" * 3))}) as server:
                outcome, exit_code, _ = _download(backend, server, case_id, part)
                return _metrics(mode="probe", case_id=case_id, headers=_headers(server), outcome=outcome,
                                exit_code=exit_code, part=part, final_exists=False,
                                request_count=len(server.state.requests), sleeps=[], body_bytes=_part_size(part))
        finally:
            shutil.rmtree(part.parent, ignore_errors=True)
    if case_id == "CHAR-PRB-03":
        with LocalServer("head_ok", body=b"%PDF" + b"x") as server:
            outcome, exit_code, _ = _run(lambda: backend.remote_length(server.url + "/" + case_id))
            return _metrics(mode="probe", case_id=case_id, headers=_headers(server), outcome=outcome,
                            exit_code=exit_code, part=None, final_exists=False,
                            request_count=len(server.state.requests), sleeps=[], body_bytes=0)
    if case_id == "CHAR-HEAD-01":
        with LocalServer("head_405") as server:
            outcome, exit_code, _ = _run(lambda: backend.remote_length(server.url + "/" + case_id))
            return _metrics(mode="probe", case_id=case_id, headers=_headers(server), outcome=outcome,
                            exit_code=exit_code, part=None, final_exists=False,
                            request_count=len(server.state.requests), sleeps=[], body_bytes=0)
    if case_id == "CHAR-PROXY-01":
        outcome, exit_code, _ = _run(backend.proxy_ok)
        return _metrics(mode="probe", case_id=case_id, headers={}, outcome=outcome, exit_code=exit_code,
                        part=None, final_exists=False, request_count=0, sleeps=[])
    if case_id == "CHAR-FIN-01":
        outcome, exit_code, _ = _run(backend.finalize_ok)
        return _metrics(mode="probe", case_id=case_id, headers={}, outcome=outcome, exit_code=exit_code,
                        part=None, final_exists=False, request_count=0, sleeps=[])
    raise AssertionError(f"unhandled case {case_id}")
