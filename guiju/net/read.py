"""Prefix-reading helpers shared by the outbound response owners.

Deliberately free of body/redirect/transport imports so that BodyClient, the
redirect guard and the metadata client can share it without an import cycle.
"""

from __future__ import annotations

import http.client


def read_prefix(resp, limit: int) -> tuple[bytes, BaseException | None]:
    """Read up to 'limit' bytes, keeping bytes delivered with a short read.

    Anti-bot layers often cut a block page off mid-body.  Both
    http.client.IncompleteRead and ConnectionResetError can therefore arrive
    *after* the server already delivered part of the marker page.  The caller
    must inspect the returned prefix before deciding whether to re-raise the
    error, so a truncated block page still maps to exit 40 instead of a
    retryable network failure.
    """
    if limit <= 0:
        return b"", None
    buffer = bytearray()
    while len(buffer) < limit:
        try:
            chunk = resp.read(limit - len(buffer))
        except (http.client.IncompleteRead, ConnectionResetError) as exc:
            partial = getattr(exc, "partial", b"") or b""
            if partial:
                buffer.extend(partial[: limit - len(buffer)])
            return bytes(buffer), exc
        if not chunk:
            return bytes(buffer), None
        buffer.extend(chunk[: limit - len(buffer)])
        # HTTPResponse exposes the number of declared bytes still outstanding.
        # A zero length means the declared body is complete, so a short read
        # must not trigger a blocking extra read() just to see EOF.
        if getattr(resp, "length", None) == 0:
            return bytes(buffer), None
    return bytes(buffer), None
