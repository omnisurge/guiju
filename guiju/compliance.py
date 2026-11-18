"""合规护栏 —— 全仓唯一出站闸门。

四条铁律（刻意不提供 override 开关：靠自觉的合规，三个月内必然失效）：

1. 域名白名单：只允许 *.gov.cn，且**每一次重定向**都要重新校验
2. 限速：按 host 令牌间隔，默认最小 1.2 秒，宁可慢，不可打
3. 诚实 UA：声明身份，不伪装浏览器（纯 ASCII，HTTP 头只接受 latin-1）
4. 命中人机校验立即放弃 —— 不尝试绕过，放弃比绕过正确

历史教训（2026-10-30 复核）：原实现只在发请求前校验白名单，而 urllib 默认自动跟随
30x 重定向，一次跳转即可绕过白名单。现由 _GuardedRedirect 逐跳校验。
"""

from __future__ import annotations

import http.client
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ._version import __version__
from .errors import ComplianceDenied, SourceUnreachable

#: 诚实 UA：声明身份，不伪装浏览器。必须纯 ASCII。
UA = f"guiju/{__version__} (compliance-aware; official code fetcher)"
ALLOWED_SUFFIX = ".gov.cn"
DEFAULT_MIN_INTERVAL = 1.2

#: 命中这些就认定被反爬拦下，直接放弃（不重试、不换 UA、不接代理）
BLOCK_MARKERS = (
    "验证码",
    "人机验证",
    "滑动验证",
    "安全验证",
    "访问过于频繁",
    "请求过于频繁",
)

#: 只对这些类型的响应做拦截检测（PDF 与二进制不检测，避免误伤与额外开销）
_INSPECTABLE = ("text/html", "text/plain", "application/json")
_MAX_INSPECT_BYTES = 2_000_000
_INSPECT_HEAD = 8192


@dataclass(frozen=True)
class Response:
    url: str          # 跟随重定向后的最终 URL
    body: bytes
    content_type: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "ignore")


def check_url(url: str) -> str:
    """白名单校验。任何出站 URL（含重定向目标）都必须过这里。"""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise ComplianceDenied(f"仅允许 http(s)：{url}")
    host = (parts.hostname or "").lower()
    if not (host == "gov.cn" or host.endswith(ALLOWED_SUFFIX)):
        raise ComplianceDenied(f"非政府域名，拒绝访问：{host or '(空)'}")
    return url


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """把白名单钉死在每一次跳转上 —— 这是本产品唯一的红线级校验。

    校验之外还要对跳转目标重新节流：白名单是 host 级，跳去另一个
    *.gov.cn 子站时若不复算间隔，一次跨站重定向就绕过对目标的限速。"""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        check_url(target)
        host = (urllib.parse.urlsplit(target).hostname or "").lower()
        if host:
            _throttle(host)
        return super().redirect_request(req, fp, code, msg, headers, target)


#: 自定义 opener：重定向走白名单闸门，HTTPS 用默认 CA 上下文
#: （注意：OpenerDirector.open() 不接受 context 参数，必须在 handler 上配置）
_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    _GuardedRedirect(),
)
_last_call: dict[str, float] = {}
#: 读-睡-写必须互斥：MCP --http 是 ThreadingHTTPServer，并发线程会读到
#: 同一个 last 然后都不睡，等于对同一 host 并发绕过限速。锁包住 sleep
#: 会让跨 host 请求也串行——偏保守，但政府站点宁可慢不可打，方向正确。
_throttle_lock = threading.Lock()


def _throttle(host: str) -> None:
    with _throttle_lock:
        last = _last_call.get(host, 0.0)
        gap = time.monotonic() - last
        if gap < DEFAULT_MIN_INTERVAL:
            time.sleep(DEFAULT_MIN_INTERVAL - gap)
        _last_call[host] = time.monotonic()


def _detect_block(resp: Response) -> None:
    ctype = resp.content_type.lower()
    if not any(t in ctype for t in _INSPECTABLE):
        return
    if len(resp.body) > _MAX_INSPECT_BYTES:
        return
    head = resp.body[:_INSPECT_HEAD].decode("utf-8", "ignore")
    for marker in BLOCK_MARKERS:
        if marker in head:
            raise ComplianceDenied(
                f"目标站点返回人机校验（命中「{marker}」）。按要求放弃，不做绕过。"
            )


def get(url: str, referer: str | None = None, timeout: int = 60, attempts: int = 2) -> Response:
    """发一次 GET，返回 Response。这是全仓唯一允许发起网络请求的函数。

    弱网容错：只对**可重试**的失败（超时/连接错误/5xx）做有界重试，默认 2 次、退避 1s。
    合规拒绝（40）与 4xx 一律不重试 —— 重试它们只会变成攻击。
    """
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return _get_once(url, referer=referer, timeout=timeout)
        except _Retryable as exc:
            if attempt >= attempts:
                raise SourceUnreachable(str(exc)) from None
            time.sleep(1.0 * attempt)
    raise SourceUnreachable(f"重试耗尽：{url}")  # pragma: no cover


def post_json(
    url: str,
    body: dict,
    *,
    referer: str | None = None,
    timeout: int = 60,
    attempts: int = 2,
) -> dict:
    """POST JSON 并解析响应。与 GET 走同一道闸门（白名单 / 逐跳校验 / 限速 / 诚实 UA）。

    为什么 POST 也要进闸门：多数"标准查询"接口是 POST JSON，如果给它开私路，
    白名单与限速就会在第二个源上失效。
    """
    import json

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    for attempt in range(1, max(1, attempts) + 1):
        try:
            raw = _post_once(url, data, referer=referer, timeout=timeout)
            try:
                return json.loads(raw.decode("utf-8", "ignore"))
            except json.JSONDecodeError as exc:
                # WAF/网关常把 403/登录页以 200+HTML 返回：JSON 解析会炸成裸 ValueError
                # 冒到 CLI 变 exit 1（程序缺陷），实际是源不可达 → 归到 SourceUnreachable(20)。
                # 失败信息只截头部几行，HTML 整页进 stderr 没有信息量。
                head = raw.decode("utf-8", "ignore").strip().splitlines()[:3]
                raise SourceUnreachable(
                    f"POST 返回的不是 JSON（{len(raw)} 字节，前 3 行：{head}）：{url}"
                ) from exc
        except _Retryable as exc:
            if attempt >= attempts:
                raise SourceUnreachable(str(exc)) from None
            time.sleep(1.0 * attempt)
    raise SourceUnreachable(f"重试耗尽：{url}")  # pragma: no cover


def _post_once(url: str, data: bytes, *, referer: str | None, timeout: int) -> bytes:
    check_url(url)
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    _throttle(host)
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise _Retryable(f"HTTP {exc.code}：{url}") from exc
        raise SourceUnreachable(f"HTTP {exc.code}：{url}") from exc
    except ComplianceDenied:
        raise
    except ssl.SSLError as exc:
        raise _Retryable(f"TLS 失败：{exc}") from exc
    except OSError as exc:
        raise _Retryable(f"{type(exc).__name__}：{exc}［{url}］") from exc


def _remote_length(url: str, *, referer: str | None, timeout: int) -> int | None:
    """HEAD 取 Content-Length；拿不准（4xx/无头字段/网络错）返回 None。

    只服务于 416 校验这一个场景，不为别的流程多生请求。"""
    try:
        check_url(url)
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        _throttle(host)
        headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, headers=headers, method="HEAD")
        with _opener.open(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
        return int(length) if length is not None and str(length).isdigit() else None
    except Exception:
        return None


def _header_int(value: str | None) -> int | None:
    """只接受 ASCII 纯数字头；缺失/畸形/超长一律视为未知，不拿猜的长度做校验。"""
    if value is None:
        return None
    text = value.strip()
    if not text.isascii() or not text.isdigit() or len(text) > 32:
        return None
    try:
        return int(text)
    except ValueError:
        # Python 3.11+ 对超长数字串会抛 ValueError；按未知处理，不能让头部
        # 解析把下载主路径打成 internal traceback。
        return None


def _parse_content_range(value: str | None) -> tuple[int | None, int | None, int | None]:
    """解析 206 的 Content-Range：bytes start-end/total 或 bytes start-end/*。

    返回 (start, end, total)；total 为 * 或无法识别时为 None。
    HTTP 的 range unit 大小写不敏感（RFC 9110），接受 Bytes/BYTES。
    """
    if not value:
        return None, None, None
    match = re.fullmatch(
        r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", value, re.IGNORECASE
    )
    if not match:
        return None, None, None
    start = _header_int(match.group(1))
    end = _header_int(match.group(2))
    total = None if match.group(3) == "*" else _header_int(match.group(3))
    if start is None or end is None or (match.group(3) != "*" and total is None):
        return None, None, None
    return start, end, total


class _Retryable(Exception):
    """内部标记：这类失败值得再试一次。"""


def download(
    url: str,
    part_path: str | Path,
    *,
    referer: str | None = None,
    timeout: int = 180,
    attempts: int = 2,
    chunk_size: int = 1 << 16,
) -> dict:
    """流式下载到 part_path，支持 HTTP Range 续传。

    为什么落 `.part` 而不直接写最终文件：最终名是内容寻址（`<名>__<sha256前12>.pdf`），
    只有下完才知道。调用方拿到 `bytes` 后自己算 sha256 再改名。

    续传条件：`part_path` 已存在且非空 → 发 `Range: bytes=<size>-`。
    - 服务器回 206 → 追加（真续传）
    - 服务器回 200 → 服务端不支持 Range，截断重下（不是错误，只是没省流量）
    - 服务器回 416 → 只有 HEAD 明确给出与本地相同的总长才按"已完成"；
      否则丢弃 .part 从 0 重下，仍失败则 20，绝不接受未知长度的旧部分
    """
    part = Path(part_path)
    part.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, max(1, attempts) + 1):
        start = part.stat().st_size if part.exists() else 0
        try:
            _download_once(
                url, part, start=start, referer=referer, timeout=timeout, chunk_size=chunk_size
            )
            return {
                "bytes": part.stat().st_size if part.exists() else start,
                "resumed_from": start,
                "url": url,
            }
        except _Retryable as exc:
            if attempt >= attempts:
                raise SourceUnreachable(str(exc)) from None
            time.sleep(1.0 * attempt)
    raise SourceUnreachable(f"重试耗尽：{url}")  # pragma: no cover


def _download_once(
    url: str,
    part: Path,
    *,
    start: int,
    referer: str | None,
    timeout: int,
    chunk_size: int,
) -> None:
    check_url(url)
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    _throttle(host)

    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)

    try:
        with _opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype.startswith("text/html"):
                head = resp.read(4096)
                text = head.decode("utf-8", "ignore")
                for marker in BLOCK_MARKERS:
                    if marker in text:
                        raise ComplianceDenied(
                            f"目标站点返回人机校验（命中「{marker}」）。按要求放弃，不做绕过。"
                        )
                raise SourceUnreachable(f"附件返回 HTML 而非文件（{len(head)} 字节）：{url}")
            if status not in (200, 206):
                raise SourceUnreachable(f"附件返回意外状态 {status}：{url}")
            append = status == 206 and start > 0
            mode = "ab" if append else "wb"
            content_length = _header_int(resp.headers.get("Content-Length"))
            range_start = range_end = range_total = None
            if status == 206:
                range_start, range_end, range_total = _parse_content_range(
                    resp.headers.get("Content-Range")
                )
                if range_start is None or range_end is None:
                    raise SourceUnreachable(f"206 响应缺少可校验的 Content-Range：{url}")
                if range_start != start:
                    # ADR 0004 D1 规则 4：起点不符必须失败并保留 .part，
                    # 不能为了自愈丢掉大文件的续传进度；让外层有限次重试后报 20。
                    raise _Retryable(
                        f"206 起点 {range_start} 与本地 {start} 不符，保留 .part：{url}"
                    )
                if range_end < range_start or (
                    range_total is not None and range_end >= range_total
                ):
                    raise SourceUnreachable(
                        f"206 Content-Range 非法：{range_start}-{range_end}/{range_total}［{url}］"
                    )
                if range_total is None:
                    # bytes start-end/* 只能证明这一段，不能证明整个文件完整；
                    # 宁可 20，也不把未知总长的短文件当成功。
                    raise SourceUnreachable(
                        f"206 未给出总长（Content-Range 以 * 结尾），无法证明完整：{url}"
                    )
            with part.open(mode) as fh:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
            final_size = part.stat().st_size
            if status == 200:
                if content_length is not None and final_size != content_length:
                    raise _Retryable(
                        f"响应截断：声明 {content_length} 字节，实收 {final_size} 字节［{url}］"
                    )
            else:
                if final_size != range_end + 1:
                    raise _Retryable(
                        f"206 响应截断：应到 {range_end}，实收 {final_size - 1}［{url}］"
                    )
                if range_total is not None and final_size != range_total:
                    raise _Retryable(
                        f"206 响应不完整：总长 {range_total}，实收 {final_size}［{url}］"
                    )
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and start > 0:
            # 416 多半是"本地已下完"（部分 ≥ 远端总长）；但也可能是 .part 损坏超界。
            # 别盲信：HEAD 对一次 Content-Length，对不上就删掉重下一次（仅此一次）。
            remote = _remote_length(url, referer=referer, timeout=timeout)
            if remote == start:
                # HEAD 给出了与本地一致的明确总长，才接受“已完成”。
                return
            if remote is None:
                # HEAD 被站点禁用时，416 不能再被当成“已完成”的证据；
                # 丢掉 .part 从 0 重下一次，宁可慢，也不接受未知长度的文件。
                part.unlink(missing_ok=True)
                raise _Retryable(
                    f"416 且无法确认远端总长，已丢弃 .part 从 0 重下：{url}"
                ) from exc
            part.unlink(missing_ok=True)
            _download_once(
                url, part, start=0, referer=referer, timeout=timeout, chunk_size=chunk_size
            )
            return
        if exc.code >= 500:
            raise _Retryable(f"HTTP {exc.code}：{url}") from exc
        raise SourceUnreachable(f"HTTP {exc.code}：{url}") from exc
    except ComplianceDenied:
        raise
    except http.client.IncompleteRead as exc:
        raise _Retryable(f"响应截断：{exc}［{url}］") from exc
    except ssl.SSLError as exc:
        raise _Retryable(f"TLS 失败：{exc}") from exc
    except OSError as exc:
        raise _Retryable(f"{type(exc).__name__}：{exc}［{url}］") from exc


def _get_once(url: str, referer: str | None = None, timeout: int = 60) -> Response:
    check_url(url)
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    _throttle(host)

    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)

    try:
        with _opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            final_url = resp.geturl() or url
            ctype = resp.headers.get("Content-Type", "") or ""
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            raise _Retryable(f"HTTP {exc.code}：{url}") from exc
        raise SourceUnreachable(f"HTTP {exc.code}：{url}") from exc
    except ComplianceDenied:
        raise
    except ssl.SSLError as exc:
        raise _Retryable(f"TLS 失败：{exc}") from exc
    except Exception as exc:
        raise _Retryable(f"{type(exc).__name__}：{exc}［{url}］") from exc

    out = Response(url=final_url, body=body, content_type=ctype)
    _detect_block(out)
    return out
