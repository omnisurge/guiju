"""Block-page detection that never depends on a caller remembering to check."""

from __future__ import annotations

import html
import re

from ..errors import ComplianceDenied

BLOCK_MARKERS = (
    "验证码",
    "人机验证",
    "滑动验证",
    "安全验证",
    "访问过于频繁",
    "请求过于频繁",
    "verify you are human",
    "cf-chl-",
)

_INSPECTABLE = ("text/html", "text/plain", "application/json", "application/xhtml+xml")
_BINARY_MAGIC = (b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0")


class BlockDetector:
    def __init__(self, markers: tuple[str, ...] = BLOCK_MARKERS, *, max_scan: int = 65536) -> None:
        self.markers = markers
        self.max_scan = max_scan

    def check(self, body: bytes, content_type: str = "", *, reject_binary: bool = False) -> None:
        if reject_binary and body.lstrip().startswith(_BINARY_MAGIC):
            raise ComplianceDenied("响应伪装成文本，实际是正文文件")
        ctype = (content_type or "").split(";", 1)[0].strip().lower()
        looks_like_html = body[:512].lstrip().lower().startswith((b"<html", b"<!doctype", b"<?xml"))
        if ctype not in _INSPECTABLE and not looks_like_html:
            return
        head = body[: self.max_scan]
        for encoding in ("utf-8", "gbk"):
            text = head.decode(encoding, "ignore").lower()
            text = html.unescape(re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text))
            for marker in self.markers:
                if marker.lower() in text:
                    raise ComplianceDenied(
                        f"目标站点返回人机校验（命中「{marker}」）。按要求放弃，不做绕过。"
                    )
