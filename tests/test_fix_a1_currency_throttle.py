"""A1：元数据出口的限速必须对并发线程生效（与 compliance 同一模式）。"""

import contextlib
import threading
import unittest
from unittest import mock

from guiju.currency import httpx


class _SyncDict(dict):
    """让两个线程都先读到旧值，再允许写；用来暴露"只锁写"的竞态。"""

    def __init__(self):
        super().__init__()
        self.armed = True
        self.barrier = threading.Barrier(2)

    def get(self, key, default=None):
        value = super().get(key, default)
        if self.armed:
            self.armed = False
            with contextlib.suppress(threading.BrokenBarrierError):
                self.barrier.wait(timeout=0.2)
        return value


class CurrencyThrottleTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.original_last = httpx.default_throttle()._last
        mock.patch.object(httpx, "MIN_INTERVAL", 0.5).start()
        self.sleeps = []
        mock.patch.object(httpx.time, "sleep", lambda s: self.sleeps.append(round(s, 3))).start()

    def tearDown(self):
        httpx.default_throttle()._last = self.original_last
        httpx.default_throttle()._last.clear()

    def test_serial_calls_wait(self):
        httpx.default_throttle()._last.clear()
        httpx._throttle("www.csres.com")
        httpx._throttle("www.csres.com")
        self.assertEqual(len(self.sleeps), 1)
        self.assertGreaterEqual(self.sleeps[0], 0.4)

    def test_concurrent_calls_are_serialized(self):
        httpx.default_throttle()._last = _SyncDict()
        threads = [threading.Thread(target=httpx._throttle, args=("www.csres.com",)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        # 有锁：第一个线程在 get 里等 0.2s 超时后写 last，第二个线程读到 last 后 sleep。
        # 无锁：两个线程都读到 None，sleeps 为空。
        self.assertTrue(self.sleeps, "并发限速没有生效：两个线程都判定无需等待")
        self.assertGreaterEqual(self.sleeps[0], 0.4)


if __name__ == "__main__":
    unittest.main()
