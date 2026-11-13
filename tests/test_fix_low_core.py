"""Low 批次回归测试（2026-11-12 code review 第 3 轮）。

覆盖：mcp do_POST 的 Content-Length 防线、announcement 未知源显式报错、
非回环绑定告警、catalog meta 损坏信号、sync --if-stale 单次 count、
announcement 共享只读 Store（惰性 + 跨线程 + 关闭）。
"""

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from guiju import mcp as mcp_mod
from guiju.mcp import McpServer, make_http_server
from guiju.store.sqlite import Store


class _FakeBackend:
    """McpServer 只需一个 lookup()，announcement 不真正调用它。"""

    def lookup(self, code):
        return []


def _post_raw(port, body, headers, path="/mcp"):
    """裸 socket 发 POST（http.client 没法发"缺 Content-Length"/负值）。"""
    lines = [f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    lines += headers
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(raw)
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            data = s.recv(65536)
            if not data:
                break
            chunks.append(data)
    head = b"".join(chunks).split(b"\r\n", 1)[0]
    return int(head.split(b" ", 2)[1])


def _post(port, payload, path="/mcp"):
    body = json.dumps(payload).encode("utf-8")
    return _post_raw(port, body, [f"Content-Length: {len(body)}"], path)


class TestContentLengthGuard(unittest.TestCase):
    """do_POST 不能再无条件信任客户端声明的 Content-Length。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = make_http_server(McpServer(_FakeBackend()), "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_oversized_content_length_rejected_413(self):
        # 声明超过硬上限就直接拒，服务端不等你把 body 发完
        status = _post_raw(
            self.port, b"{}",
            [f"Content-Length: {mcp_mod.MAX_BODY_BYTES + 1}"])
        self.assertEqual(status, 413)

    def test_at_limit_boundary_accepted(self):
        # 正好 4MB 的请求读得进去（body 太小 JSON 会 400，但不会 413）
        status = _post_raw(
            self.port, b" ",
            [f"Content-Length: {mcp_mod.MAX_BODY_BYTES}"])
        # 实际 body 只发了 1 字节，rfile.read 返回短数据 → JSON 解析失败 400（不是 413）
        self.assertEqual(status, 400)

    def test_missing_content_length_rejected(self):
        self.assertEqual(_post_raw(self.port, b"{}", []), 400)

    def test_negative_content_length_rejected(self):
        self.assertEqual(
            _post_raw(self.port, b"{}", ["Content-Length: -1"]), 400)

    def test_non_numeric_content_length_rejected(self):
        self.assertEqual(
            _post_raw(self.port, b"{}", ["Content-Length: abc"]), 400)

    def test_normal_request_still_works(self):
        status = _post(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(status, 200)


class TestNonLoopbackWarning(unittest.TestCase):
    """绑定非回环地址时 stderr 必须有一行告警（不阻断）。"""

    def _make(self, host):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            httpd = make_http_server(McpServer(_FakeBackend()), host, 0)
        httpd.server_close()
        return err.getvalue()

    def test_non_loopback_warns(self):
        self.assertIn("非回环", self._make("0.0.0.0"))

    def test_loopback_quiet(self):
        self.assertNotIn("非回环", self._make("127.0.0.1"))

    def test_loopback_detection_covers_whole_127_block(self):
        # 127.x 全段都是回环（判定函数层面测，macOS 绑不了 127.0.0.2）
        self.assertTrue(mcp_mod._is_loopback("127.0.0.2"))
        self.assertTrue(mcp_mod._is_loopback("::1"))
        self.assertTrue(mcp_mod._is_loopback("localhost"))
        self.assertFalse(mcp_mod._is_loopback("0.0.0.0"))
        self.assertFalse(mcp_mod._is_loopback("192.168.1.10"))


class TestAnnouncementUnknownSource(unittest.TestCase):
    """_announcement 不许静默回落 mohurd：认不出的源要显式 needs_human。"""

    def test_unresolved_source_id_raises_needs_human(self):
        server = McpServer(_FakeBackend())
        ann = {"id": 1, "url": "https://some.unknown.gov.cn/notice/1",
               "doc_name": "某标准", "title": "t", "category": "",
               "published_on": "2026-01-01"}
        fake_target = {"ann": ann, "code_display": "GB 1", "status": "", "confidence": ""}
        fake_source = unittest.mock.Mock()
        with patch("guiju.cli.lookup.resolve", return_value=fake_target), \
             patch("guiju.sources.registry", return_value={"mohurd": fake_source}), \
             tempfile.TemporaryDirectory() as tmp:
                os.environ["GUIJU_HOME"] = tmp
                try:
                    result = server._announcement({"ref": "https://some.unknown.gov.cn/notice/1"})
                finally:
                    server.close()
        self.assertTrue(result.get("isError"))
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["reason"], "needs_human")
        self.assertEqual(payload["exit"], 50)
        fake_source.detail.assert_not_called()


class TestSharedAnnouncementStore(unittest.TestCase):
    """announcement 复用 McpServer 生命周期的共享 Store（不再每请求跑 DDL）。"""

    def _call(self, server):
        ann = {"id": 1, "url": "https://www.mohurd.gov.cn/x/1",
               "doc_name": "某标准", "title": "t", "category": "",
               "published_on": "2026-01-01"}
        fake_target = {"ann": ann, "code_display": "GB 1", "status": "", "confidence": ""}
        fake_source = unittest.mock.Mock(api_mode=True)
        with patch("guiju.cli.lookup.resolve", return_value=fake_target), \
             patch("guiju.sources.registry", return_value={"mohurd": fake_source}):
            return server._announcement({"ref": "1"})

    def test_store_reused_across_requests_and_closed_once(self):
        created = []
        instances = []

        class _FakeStore:
            def __init__(self, **kw):
                created.append(kw)
                self.db = object()  # _ReadOnlyStore 只转发属性，对象身份能认
                self.closed = 0
                instances.append(self)

            def close(self):
                self.closed += 1

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["GUIJU_HOME"] = tmp
            # _ann_store 做的是局部 import，patch 的是 store.sqlite 里的名字
            with patch("guiju.store.sqlite.Store", _FakeStore):
                server = McpServer(_FakeBackend())
                self._call(server)
                self._call(server)
                self.assertEqual(len(created), 1)
                self.assertEqual(created[0].get("cross_thread"), True)
                server.close()
                self.assertEqual(instances[0].closed, 1)

    def test_cross_thread_store_usable_from_other_thread(self):
        # cross_thread=True 的连接在线程间用不再触发 sqlite3.ProgrammingError
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(__import__("pathlib").Path(tmp) / "c.db"),
                          cross_thread=True)
            errors = []

            def q():
                try:
                    store.count_announcements()
                    store.catalog_age_days()
                except Exception as exc:
                    errors.append(exc)

            t = threading.Thread(target=q)
            t.start()
            t.join()
            store.close()
            self.assertEqual(errors, [])


class TestCatalogMetaCorrupt(unittest.TestCase):
    """synced_at 解析失败要和'没同步过'区分开。"""

    def test_corrupt_synced_at_signalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(__import__("pathlib").Path(tmp) / "c.db"))
            store.set_meta("synced_at", "垃圾值")
            store.commit()
            self.assertIsNone(store.catalog_age_days())
            corrupt = store.catalog_meta_corrupt()
            self.assertIsNotNone(corrupt)
            self.assertIn("损坏", corrupt)
            store.close()

    def test_no_meta_not_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(__import__("pathlib").Path(tmp) / "c.db"))
            self.assertIsNone(store.catalog_age_days())
            self.assertIsNone(store.catalog_meta_corrupt())
            store.close()

    def test_valid_meta_not_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(str(__import__("pathlib").Path(tmp) / "c.db"))
            store.set_meta("synced_at", "2026-11-01 10:00:00")
            store.commit()
            self.assertIsNotNone(store.catalog_age_days())
            self.assertIsNone(store.catalog_meta_corrupt())
            store.close()


class TestSyncIfStaleCountsOnce(unittest.TestCase):
    """sync --if-stale 命中新鲜目录时 count_announcements 只查一次。"""

    def test_single_count_call(self):
        from guiju.cli.commands import sync as sync_mod

        args = unittest.mock.MagicMock()
        args.if_stale = 7
        store = unittest.mock.MagicMock()
        store.count_announcements.return_value = 3254
        store.catalog_age_days.return_value = 0.5
        store.get_meta.return_value = "2026-11-12 09:00:00"
        store.path = __import__("pathlib").Path("/tmp/x.db")
        with contextlib.redirect_stderr(io.StringIO()):
            rc = sync_mod.run(store, args)
        self.assertEqual(rc, 0)
        self.assertEqual(store.count_announcements.call_count, 1)


if __name__ == "__main__":
    unittest.main()
