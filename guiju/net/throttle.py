"""Process-wide per-host request spacing."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .headers import canonical_host


class Throttle:
    """A process-local throttle. Read/wait/write is one critical section."""

    def __init__(
        self,
        min_interval: float = 1.2,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.min_interval = float(min_interval)
        self._clock = clock or (lambda: time.monotonic())
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self._guard = threading.Lock()
        self._lock = self._guard  # compatibility for existing tests/inspection
        self._host_locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> float:
        host = canonical_host(host)
        with self._guard:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            now = self._clock()
            last = self._last.get(host)
            waited = 0.0
            if last is not None:
                gap = now - last
                if gap < self.min_interval:
                    waited = self.min_interval - gap
                    self._sleep(waited)
            self._last[host] = self._clock()
            return waited


_DEFAULT: Throttle | None = None


def default_throttle() -> Throttle:
    """The one explicit ADR rule-5 exception; replaced by AppContext injection later."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Throttle()
    return _DEFAULT
