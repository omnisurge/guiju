"""ADR 0002 决策 5：元数据适配器不能复用正文出口或落盘。"""

import ast
import builtins
import io
import socket
import sqlite3
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from guiju import compliance
from guiju.currency.csres import CsresCurrency
from guiju.currency.httpx import HttpClient
from tests.currency_helpers import FakeResponse

ROOT = Path(__file__).resolve().parent.parent


class NativeMetadataBoundaryTest(unittest.TestCase):
    def test_native_subtree_has_no_content_storage_or_filesystem_dependency(self):
        # __init__ 是旧兼容门面，既有 CachedCurrency 落库不属于本约束范围。
        paths = sorted((ROOT / "guiju/currency").rglob("*.py"))
        paths.append(ROOT / "guiju/domain/shzjw.py")
        self.assertGreaterEqual(len(paths), 5)
        forbidden = {"compliance", "files", "store", "pathlib", "sqlite3", "os", "subprocess", "socket", "shutil"}
        for path in paths:
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [alias.name for alias in node.names]
                    if isinstance(node, ast.ImportFrom) and node.module:
                        names.append(node.module)
                    for name in names:
                        with self.subTest(path=path.name, line=node.lineno, dependency=name):
                            self.assertTrue(forbidden.isdisjoint(name.split(".")))
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name):
                        self.assertNotIn(func.id, {"open", "__import__", "eval", "exec"}, str(path))
                    if isinstance(func, ast.Attribute):
                        self.assertNotIn(func.attr, {"write", "writelines", "write_bytes", "write_text", "mkdir",
                                                     "touch", "unlink", "rename", "connect"}, str(path))

    def test_full_lookup_neither_calls_content_guard_nor_writes_files(self):
        raw = (ROOT / "tests/fixtures/currency/csres_gb50011.gbk").read_bytes()
        client = HttpClient()
        client._opener = mock.Mock()
        client._opener.open.return_value = FakeResponse(raw)
        cur = CsresCurrency(client)
        fail = AssertionError("元数据链路不得触及正文出口、文件、数据库或真实网络")
        with ExitStack() as stack:
            for target, attr in (
                (compliance, "get"), (compliance, "download"), (compliance, "post_json"),
                (builtins, "open"), (io, "open"), (Path, "write_text"), (Path, "write_bytes"),
                (Path, "mkdir"), (sqlite3, "connect"), (socket, "create_connection"),
            ):
                stack.enter_context(mock.patch.object(target, attr, side_effect=fail))
            stack.enter_context(mock.patch("guiju.currency.httpx._throttle"))
            records = cur.lookup("GB50011")
        self.assertEqual([r.status for r in records], ["superseded", "current"])
        client._opener.open.assert_called_once()


    def test_all_six_adapters_and_native_without_cache_have_no_file_writes(self):
        from guiju.currency.ccsn import CcsnCurrency
        from guiju.currency.dbba import DbbaCurrency
        from guiju.currency.jtst import JtstCurrency
        from guiju.currency.native import NativeCurrency
        from guiju.currency.registry import default_registry
        from guiju.currency.shzjw import ShzjwCurrency
        from guiju.currency.stdsamr import StdSamrCurrency

        fixtures = ROOT / "tests/fixtures/currency"
        cases = (
            (CsresCurrency, "GB50011", "csres_gb50011.gbk", "text/html"),
            (CcsnCurrency, "GB50011", "ccsn_gb50011.json", "application/json"),
            (StdSamrCurrency, "GB/T19002-2018", "stdsamr_gbt19001.json", "application/json"),
            (JtstCurrency, "JTG D60", "jtst_jtgd60.html", "text/html"),
            (DbbaCurrency, "DB31/T792", "dbba_db31t792.json", "application/json"),
            (ShzjwCurrency, "DG/TJ08-2007-2026", "shzjw_current_projection.json", "application/json"),
        )
        # 夹具与客户端在哨兵开始前准备；被测调用自身不能读取/写入任何文件。
        prepared = [(adapter, code, (fixtures / name).read_bytes(), ctype) for adapter, code, name, ctype in cases]
        for adapter, code, raw, ctype in prepared:
            client = HttpClient()
            client._opener = mock.Mock()
            client._opener.open.return_value = FakeResponse(raw, url=adapter.url, headers={"Content-Type": ctype})
            instance = adapter(client)
            fail = AssertionError("元数据适配器不能访问正文/文件/数据库/真实网络")
            with self.subTest(source=instance.name), ExitStack() as stack:
                for target, attr in (
                    (compliance, "get"), (compliance, "download"), (compliance, "post_json"),
                    (builtins, "open"), (io, "open"), (Path, "write_text"), (Path, "write_bytes"),
                    (Path, "mkdir"), (sqlite3, "connect"), (socket, "create_connection"),
                ):
                    stack.enter_context(mock.patch.object(target, attr, side_effect=fail))
                stack.enter_context(mock.patch("guiju.currency.httpx._throttle"))
                records = instance.lookup(code)
                self.assertTrue(records)
                self.assertTrue(all(row.source == instance.name for row in records))
            client._opener.open.assert_called_once()
        # Native 默认不装配磁盘缓存；只注册纯元数据适配器。
        client = HttpClient()
        client._opener = mock.Mock()
        client._opener.open.return_value = FakeResponse(prepared[0][2], url=CsresCurrency.url)
        native = NativeCurrency(default_registry(client))
        with (
            mock.patch.object(builtins, "open", side_effect=AssertionError("不可落盘")),
            mock.patch.object(io, "open", side_effect=AssertionError("不可落盘")),
            mock.patch("guiju.currency.httpx._throttle"),
        ):
            self.assertEqual(len(native.lookup("GB50011")), 2)


    def test_shanghai_metadata_reuses_record_core_but_never_content_transport_or_download(self):
        from guiju.currency.shzjw import ShzjwCurrency
        from guiju.domain import shzjw as core
        from guiju.sources import shzjw as content

        self.assertIs(content._record, core.record)
        raw = (ROOT / "tests/fixtures/currency/shzjw_current_projection.json").read_bytes()
        client = HttpClient()
        client._opener = mock.Mock()
        client._opener.open.return_value = FakeResponse(raw, url=ShzjwCurrency.url,
                                                       headers={"Content-Type": "application/json"})
        failure = AssertionError("原生上海元数据不得调用正文传输/下载/签名/存储")
        with ExitStack() as stack:
            for target, attr in (
                (content, "_token"), (content, "download"), (content, "download_to"),
                (compliance, "get"), (compliance, "download"), (compliance, "post_json"),
                (builtins, "open"), (io, "open"), (Path, "write_text"), (Path, "write_bytes"),
                (Path, "mkdir"), (sqlite3, "connect"), (socket, "create_connection"),
            ):
                stack.enter_context(mock.patch.object(target, attr, side_effect=failure))
            parse = stack.enter_context(mock.patch.object(core, "record", wraps=core.record))
            stack.enter_context(mock.patch("guiju.currency.httpx._throttle"))
            result = ShzjwCurrency(client).lookup("DG/TJ08-2007-2026")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source, "shzjw")
        parse.assert_called_once()
        client._opener.open.assert_called_once()


if __name__ == "__main__":
    unittest.main()
