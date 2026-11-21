"""Header and host helpers shared by the outbound clients."""

from __future__ import annotations

import re

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def header_int(value: str | None) -> int | None:
    """Only accept an ASCII decimal header value; malformed values mean unknown."""
    if value is None:
        return None
    text = value.strip()
    if not text.isascii() or not text.isdigit() or len(text) > 32:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_content_range(value: str | None) -> tuple[int | None, int | None, int | None]:
    """Parse bytes start-end/total (unit case-insensitive; * means unknown)."""
    if not value:
        return None, None, None
    match = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", value, re.IGNORECASE)
    if not match:
        return None, None, None
    start = header_int(match.group(1))
    end = header_int(match.group(2))
    total = None if match.group(3) == "*" else header_int(match.group(3))
    if start is None or end is None or (match.group(3) != "*" and total is None):
        return None, None, None
    return start, end, total


def canonical_host(value: str | None) -> str:
    """Canonicalise a host for throttling: IDNA, lower-case, no trailing dot."""
    host = (value or "").strip().lower().rstrip(".")
    if not host:
        return ""
    if host.isascii():
        ascii_host = host
    else:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            return ""
    labels = ascii_host.split(".")
    if not all(_HOST_LABEL.fullmatch(label) for label in labels):
        return ""
    return ascii_host


def valid_host(value: str | None) -> bool:
    return bool(canonical_host(value))
