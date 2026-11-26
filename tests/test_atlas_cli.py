"""标准图集索引存储、guiju atlas CLI 与 which/pull/resolve 图集边界离线测试。"""

from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from guiju import currency as currency_mod
from guiju.application.resolve import ref_is_code
from guiju.cli import main
from guiju.cli.commands import atlas as atlas_cmd
from guiju.currency.model import Record
from guiju.domain import atlas as atlas_domain
from guiju.errors import NeedsHuman
from guiju.store import atlas as atlas_store_mod
from guiju.store.atlas import AtlasCatalogStore
from guiju.store.sqlite import SCHEMA_VERSION, Store

FIXTURES = Path(__file__).parent / "fixtures" / "atlas"


def _concise_bytes() -> bytes:
    return (FIXTURES / "concise_catalogue_sample.html").read_bytes()


def _run_cli(argv: list[str], *, home: Path, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    env = {"GUIJU_HOME": str(home), "GUIJU_NO_AUTO_SYNC": "1", **(extra_env or {})}
    with mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


class AtlasCatalogStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "atlas-catalog.json"

    def test_load_missing_returns_none_without_creating_directory(self) -> None:
        missing = self.root / "never-created" / "atlas-catalog.json"
        with mock.patch.object(Path, "mkdir", side_effect=AssertionError("load must not mkdir")):
            self.assertIsNone(AtlasCatalogStore(missing).load())
        self.assertFalse(missing.parent.exists())

    def test_save_and_load_atomic_roundtrip_and_preserves_old_on_failure(self) -> None:
        entries = atlas_domain.parse_concise_catalogue(_concise_bytes().decode("utf-8"))
        store = AtlasCatalogStore(self.path, clock=lambda: 1_800_000_000.0)
        snap = store.save(entries)
        self.assertEqual(len(snap.entries), 21)
        self.assertTrue(self.path.exists())

        loaded = store.load()
        self.assertIsNotNone(loaded)
        self.assertFalse(loaded.corrupt)
        self.assertEqual(len(loaded.entries), 21)
        self.assertEqual(loaded.synced_epoch, 1_800_000_000.0)
        self.assertAlmostEqual(loaded.age_days(1_800_086_400.0), 1.0)

        original = self.path.read_bytes()
        for target in ("guiju.store.atlas.os.fsync", "guiju.store.atlas.os.replace"):
            with self.subTest(target=target), mock.patch(target, side_effect=OSError("disk full")), \
                    self.assertRaises(NeedsHuman) as ctx:
                store.save([replace(entries[0], name="不应覆盖")])
            self.assertEqual(ctx.exception.exit, 50)
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_corrupt_catalog_degrades_safely_without_deleting_file(self) -> None:
        bad_payloads = [
            b"",
            b"{broken",
            b"[]",
            json.dumps({"schema": 99, "source": "x", "source_url": "u", "synced_at": "t", "synced_epoch": 1, "total": 0, "entries": []}).encode(),
            b'{"schema":1,"schema":1}',
            json.dumps({
                "schema": 1, "source": "chinabuilding", "source_url": "u",
                "synced_at": "2026-11-26 10:00:00", "synced_epoch": True, "total": 0, "entries": [],
            }).encode(),
        ]
        store = AtlasCatalogStore(self.path)
        for raw in bad_payloads:
            with self.subTest(raw=raw[:40]):
                self.path.write_bytes(raw)
                snap = store.load()
                self.assertIsNotNone(snap)
                self.assertTrue(snap.corrupt)
                self.assertEqual(snap.entries, ())
                self.assertTrue(self.path.exists(), "load 不得擅自删除损坏文件")

    def test_save_permission_error_is_needs_human_50(self) -> None:
        entries = atlas_domain.parse_concise_catalogue(_concise_bytes().decode("utf-8"))
        with mock.patch.object(atlas_store_mod.tempfile, "NamedTemporaryFile", side_effect=PermissionError("ro")):
            with self.assertRaises(NeedsHuman) as ctx:
                AtlasCatalogStore(self.path).save(entries)
            self.assertEqual(ctx.exception.exit, 50)


class AtlasCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        mock.patch("socket.create_connection", side_effect=AssertionError("禁止真实 socket")).start()
        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("禁止真实 socket")).start()
        self.addCleanup(mock.patch.stopall)

    def test_sync_search_list_lifecycle_and_schema_v2_untouched(self) -> None:
        # 1) 裸命令与未同步提示
        rc_root, out_root, _ = _run_cli(["atlas", "--json"], home=self.home)
        self.assertEqual(rc_root, 0)
        self.assertEqual(json.loads(out_root)["subcommands"], ["sync", "search", "list"])

        rc_missing, out_missing, _ = _run_cli(["atlas", "search", "22G101", "--json"], home=self.home)
        self.assertEqual(rc_missing, 0)
        missing_payload = json.loads(out_missing)
        self.assertFalse(missing_payload["catalog_ready"])
        self.assertEqual(missing_payload["catalog_state"], "missing")
        self.assertIn("guiju atlas sync", missing_payload["hint"])

        # 2) --offline 同步拒绝 exit 2
        rc_off, out_off, _ = _run_cli(["atlas", "sync", "--offline", "--json"], home=self.home)
        self.assertEqual(rc_off, 2)
        self.assertEqual(json.loads(out_off)["reason"], "bad_usage")

        # 3) 首次 sync（离线黄金 conciseCatalogueList）
        with mock.patch.object(atlas_cmd, "_fetch_catalogue", return_value=_concise_bytes()):
            rc_sync, out_sync, _ = _run_cli(["atlas", "sync", "--json"], home=self.home)
        self.assertEqual(rc_sync, 0)
        sync_payload = json.loads(out_sync)
        self.assertTrue(sync_payload["ok"])
        self.assertFalse(sync_payload["skipped"])
        self.assertEqual(sync_payload["total"], 21)
        self.assertTrue((self.home / "atlas-catalog.json").exists())

        # catalog.db 保持 schema v2、零污染公告表
        db = Store(self.home / "catalog.db")
        try:
            self.assertEqual(db.schema_version, SCHEMA_VERSION)
            self.assertEqual(db.count_announcements(), 0)
        finally:
            db.close()

        # 4) --if-stale 7 / 0 跳过且零抓取
        with mock.patch.object(atlas_cmd, "_fetch_catalogue", side_effect=AssertionError("fresh 应跳过")) as mocked:
            for flag in ("7", "0"):
                rc_skip, out_skip, _ = _run_cli(["atlas", "sync", "--if-stale", flag, "--json"], home=self.home)
                self.assertEqual(rc_skip, 0)
                skip_payload = json.loads(out_skip)
                self.assertTrue(skip_payload["skipped"])
                self.assertEqual(skip_payload["reason"], "fresh")
            mocked.assert_not_called()

        # 5) 检索与列表筛选（关键词、前缀、状态、分页、替代展示、人读 stdout）
        rc_s1, out_s1, _ = _run_cli(["atlas", "search", "16G101-1", "--json"], home=self.home)
        self.assertEqual(rc_s1, 0)
        s1 = json.loads(out_s1)
        self.assertTrue(s1["catalog_ready"])
        self.assertGreaterEqual(s1["total"], 1)
        self.assertEqual(s1["results"][0]["code"], "16G101-1")
        self.assertEqual(s1["results"][0]["status"], "superseded")
        self.assertEqual(s1["results"][0]["replaced_by"], "22G101-1")
        self.assertIn("22G101-1", {r["code"] for r in s1["results"]})

        rc_s2, out_s2, _ = _run_cli(["atlas", "search", "22G101-1", "--status", "current"], home=self.home)
        self.assertEqual(rc_s2, 0)
        self.assertIn("22G101-1", out_s2)
        self.assertIn("现行", out_s2)

        rc_prov, out_prov, _ = _run_cli(["atlas", "list", "--prefix", "粤", "--json"], home=self.home)
        self.assertEqual(rc_prov, 0)
        prov = json.loads(out_prov)
        self.assertEqual([r["code"] for r in prov["results"]], ["粤25G/T116"])

        rc_wd, out_wd, _ = _run_cli(["atlas", "list", "--status", "withdrawn", "--json"], home=self.home)
        self.assertEqual(rc_wd, 0)
        wd_codes = {r["code"] for r in json.loads(out_wd)["results"]}
        self.assertIn("11G101-1", wd_codes)
        self.assertIn("16G101-1", wd_codes)

        rc_sup, out_sup, _ = _run_cli(["atlas", "list", "--status", "superseded", "--json"], home=self.home)
        self.assertEqual(rc_sup, 0)
        sup_codes = {r["code"] for r in json.loads(out_sup)["results"]}
        self.assertIn("16G101-1", sup_codes)
        self.assertNotIn("11G101-1", sup_codes)

        rc_lim, out_lim, _ = _run_cli(["atlas", "list", "--limit", "3", "--json"], home=self.home)
        self.assertEqual(rc_lim, 0)
        lim = json.loads(out_lim)
        self.assertEqual(lim["count"], 3)
        self.assertEqual(lim["total"], 21)
        self.assertTrue(lim["truncated"])

        rc_all, out_all, _ = _run_cli(["atlas", "list", "--all", "--json"], home=self.home)
        self.assertEqual(rc_all, 0)
        self.assertEqual(json.loads(out_all)["count"], 21)

        # 负数参数护栏 → exit 2
        rc_bad1, _, _ = _run_cli(["atlas", "search", "G101", "--limit", "-1", "--json"], home=self.home)
        rc_bad2, _, _ = _run_cli(["atlas", "sync", "--if-stale", "-2", "--json"], home=self.home)
        self.assertEqual(rc_bad1, 2)
        self.assertEqual(rc_bad2, 2)


class AtlasResolveWhichPullBoundariesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        mock.patch("socket.create_connection", side_effect=AssertionError("禁止真实 socket")).start()
        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("禁止真实 socket")).start()
        self.addCleanup(mock.patch.stopall)

    def test_ref_is_code_recognizes_chinese_province_atlas_codes(self) -> None:
        for code in ("粤25G/T116", "京05G69", "冀00J201", "22G101-1", "DBJ/T02-81-2013-12D1", "G101-1"):
            with self.subTest(code=code):
                self.assertTrue(ref_is_code(code))
        for title in ("混凝土结构施工图平面整体表示方法", "《建筑防火通用规范》", "12345", ""):
            with self.subTest(title=title):
                self.assertFalse(ref_is_code(title))

    def test_which_skips_mohurd_lookup_and_shows_replacement(self) -> None:
        db_path = self.home / "catalog.db"
        store = Store(db_path)
        store.upsert_announcement(
            "https://www.mohurd.gov.cn/gongkai/zhengce/1.html",
            "2022-05-01",
            "关于发布混凝土结构施工图的通知",
            "混凝土结构施工图",
            "national",
        )
        store.commit()
        store.close()

        class FakeAtlasCurrency:
            name = "native"
            endpoint = "native"
            from_cache = False
            stale = False
            cache_age_days = None
            upstream_error = None
            complete = True
            empty_authoritative = False
            incomplete_reason = ""

            def __init__(self, store=None, **_kwargs) -> None:
                pass

            def lookup(self, code: str) -> list[Record]:
                if code == "粤25G/T116":
                    return [Record("粤25G/T116", "current", "广东标准图集示例", "2025-03-01", "chinabuilding")]
                return [
                    Record(
                        code="16G101-1",
                        status="superseded",
                        name="混凝土结构施工图平面整体表示方法制图规则和构造详图",
                        implement_date="2016-09-01",
                        source="chinabuilding",
                        replaced_by="22G101-1",
                    )
                ]

        with mock.patch.object(currency_mod, "CachedCurrency", FakeAtlasCurrency), \
                mock.patch("guiju.cli.commands.which.resolve", side_effect=AssertionError("图集不得走住建部 resolve")):
            rc, out_json, _ = _run_cli(["which", "16G101-1", "--json"], home=self.home)
            self.assertEqual(rc, 0)
            payload = json.loads(out_json)
            self.assertFalse(payload["resolved"])
            self.assertIsNone(payload["announcement"])
            self.assertEqual(payload["currency"][0]["status"], "superseded")
            self.assertEqual(payload["currency"][0]["replaced_by"], "22G101-1")
            self.assertIn("版权出版物", payload["hint"])

            # 汉字省前缀图集在 which 里也走编号分支并跳过 resolve
            rc_prov, out_prov, _ = _run_cli(["which", "粤25G/T116", "--json"], home=self.home)
            self.assertEqual(rc_prov, 0)
            self.assertEqual(json.loads(out_prov)["currency"][0]["code"], "粤25G/T116")

            # 人读输出带 替代 22G101-1
            rc_human, out_human, _ = _run_cli(["which", "16G101-1"], home=self.home)
            self.assertEqual(rc_human, 0)
            self.assertIn("替代 22G101-1", out_human)
            self.assertIn("版权出版物", out_human)

        # --offline 对图集给明确免联网提示且不误报本地公告错误
        rc_off, out_off, _ = _run_cli(["which", "22G101-1", "--offline", "--json"], home=self.home)
        self.assertEqual(rc_off, 0)
        off_payload = json.loads(out_off)
        self.assertIsNone(off_payload["announcement"])
        self.assertIn("离线模式", off_payload["hint"])

    def test_pull_honestly_refuses_atlas_with_exit_30_and_zero_downloads(self) -> None:
        out_dir = self.home / "lib"
        for argv in (
            ["pull", "22G101-1", "--out", str(out_dir), "--json"],
            ["pull", "粤25G/T116", "--out", str(out_dir), "--json"],
            ["pull", "--source", "shzjw", "22G101-1", "--out", str(out_dir), "--json"],
        ):
            with self.subTest(argv=argv):
                rc, stdout_text, _ = _run_cli(argv, home=self.home, extra_env={"GUIJU_CURRENCY": "off"})
                self.assertEqual(rc, 30)
                payload = json.loads(stdout_text)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["reason"], "no_attachment")
                self.assertEqual(payload["verdict"], "copyright_atlas")
                self.assertIn("版权保护", payload["error"])
                self.assertIn("guiju which", payload["hint"])
        self.assertFalse(out_dir.exists())

        # pull --list 里包含图集时单项记 exit=30 并保留 verdict，不触发严重失败阻断后续
        list_file = self.home / "refs.txt"
        list_file.write_text("22G101-1\n粤25G/T116\n", encoding="utf-8")
        rc_batch, out_batch, _ = _run_cli(
            ["pull", "--list", str(list_file), "--out", str(out_dir), "--json"],
            home=self.home,
            extra_env={"GUIJU_CURRENCY": "off"},
        )
        self.assertEqual(rc_batch, 0)
        batch_payload = json.loads(out_batch)
        self.assertTrue(batch_payload["ok"])
        self.assertEqual(batch_payload["counts"], {"30": 2})
        self.assertEqual(batch_payload["results"][0]["verdict"], "copyright_atlas")
        self.assertEqual(batch_payload["results"][1]["verdict"], "copyright_atlas")


if __name__ == "__main__":
    unittest.main()
