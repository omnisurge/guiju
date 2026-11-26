"""S1 / M4 改版观测闭环测试：body_sha256 指纹、MetadataClient probe_sink、guiju sources。"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from guiju import report as render
from guiju.application import AppContext
from guiju.application import pull as pull_app
from guiju.application import resolve as resolve_app
from guiju.cli import main
from guiju.currency.model import Record
from guiju.domain.attachment import Attachment
from guiju.errors import ComplianceDenied, NeedsHuman, SourceUnreachable
from guiju.files import sha256_bytes
from guiju.mcp import _ReadOnlyStore
from guiju.net.metadata import MetadataClient
from guiju.net.policy import MetadataPolicy
from guiju.net.throttle import Throttle
from guiju.store.sqlite import Store, now
from tests.net_harness import FakeResponse, FakeTransport

URL_CCSN = "https://www.ccsn.org.cn/newweb/api/StandardQuery.assx/queryStandard"
PDF = b"%PDF-1.7\n" + b"body" * 64


class BodyHashTest(unittest.TestCase):
    def test_record_body_hash_lifecycle_and_readonly_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "c.db"
            with Store(db_path) as store:
                ann_id, _ = store.upsert_announcement(
                    "https://www.mohurd.gov.cn/x/1.html",
                    "2026-01-01",
                    "关于发布《某标准》的公告",
                    "某标准",
                    "national",
                )
                store.commit()
                self.assertEqual(store.body_hash_coverage(), {"hashed": 0, "total": 1})

                h1 = sha256_bytes(b"<html>v1</html>")
                h2 = sha256_bytes(b"<html>v2</html>")
                self.assertFalse(store.record_body_hash(ann_id, "", source="mohurd"))
                self.assertFalse(store.record_body_hash(999999, h1, source="mohurd"))
                self.assertFalse(store.record_body_hash(ann_id, h1, source="mohurd"))
                self.assertEqual(store.body_hash_coverage(), {"hashed": 1, "total": 1})
                # 相同指纹再写不记录 body_changed
                self.assertFalse(store.record_body_hash(ann_id, h1, source="mohurd"))
                # 指纹变更返回 True 并写入 body_changed 探针
                self.assertTrue(store.record_body_hash(ann_id, h2, source="mohurd"))
                store.commit()

                row = store.get_ann(ann_id)
                self.assertIsNotNone(row)
                self.assertEqual(row["body_sha256"], h2)
                summary = store.probe_summary_by_source()
                self.assertIn("mohurd", summary)
                self.assertEqual(summary["mohurd"]["body_changes"], 1)
                self.assertEqual(summary["mohurd"]["counts"], {"body_changed": 1})
                self.assertEqual(len(summary["mohurd"]["recent_failures"]), 1)
                self.assertEqual(summary["mohurd"]["recent_failures"][0]["outcome"], "body_changed")

            with Store(db_path, read_only=True) as ro_inner:
                ro = _ReadOnlyStore(ro_inner)
                self.assertFalse(ro.record_body_hash(ann_id, h1, source="mohurd"))
            with Store(db_path) as verify_store:
                self.assertEqual(verify_store.get_ann(ann_id)["body_sha256"], h2)


class PullAndResolveBodyHashTest(unittest.TestCase):
    def test_pull_records_body_hash_and_detects_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out_dir = root / "library"
            with Store(root / "c.db") as store:
                ann_id, _ = store.upsert_announcement(
                    "https://www.mohurd.gov.cn/x/1.html",
                    "2026-01-01",
                    "关于发布《某标准》的公告",
                    "某标准",
                    "national",
                )
                store.commit()

                html_state = {"html": "<html><p>发布《某标准》GB 1-2020</p></html>"}

                class FakeMohurd:
                    id = "mohurd"
                    display_name = "住建部"
                    api_mode = False

                    def detail(self, _url):
                        self.detail_calls = getattr(self, "detail_calls", 0) + 1
                        return html_state["html"], "发布《某标准》GB 1-2020"

                    def attachments(self, _html):
                        return [Attachment(url="https://www.mohurd.gov.cn/dl/1.pdf", label="某标准.pdf", kind="pdf")]

                    def download_to(self, _url, part_path, referer=None, probe_sink=None):
                        Path(part_path).parent.mkdir(parents=True, exist_ok=True)
                        Path(part_path).write_bytes(PDF)
                        return len(PDF), "pdf", 0

                src = FakeMohurd()
                cur = mock.Mock()
                cur.name = "native"
                cur.lookup.return_value = [Record("GB 1-2020", "current", "某标准", "2026-01-01", "csres")]

                with mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": "1"}):
                    ns1 = argparse.Namespace(force=False, readable=False, out=str(out_dir), source="mohurd")
                    ns1.app_context = AppContext.from_args(store, ns1, currency=cur, sources={"mohurd": src})
                    res1 = pull_app._pull_one(store, ns1, str(ann_id), src, out_dir)
                    self.assertEqual(res1["exit"], 0)
                    h1 = sha256_bytes(html_state["html"].encode("utf-8"))
                    self.assertEqual(res1["body_sha256"], h1)
                    self.assertFalse(res1["body_changed"])
                    self.assertEqual(store.get_ann(ann_id)["body_sha256"], h1)

                    # 快路径 exit 10：按已落库编号复拉不再抓详情页，但仍带回已存指纹
                    cached_res = pull_app._pull_one(store, ns1, "GB 1-2020", src, out_dir)
                    self.assertEqual(cached_res["exit"], 10)
                    self.assertEqual(cached_res["body_sha256"], h1)
                    self.assertFalse(cached_res["body_changed"])
                    self.assertEqual(src.detail_calls, 1)

                    # 页面内容发生变化后 force 重拉：body_changed=True + stderr 告警 + probe 记账
                    html_state["html"] = "<html><p>改版后：发布《某标准》GB 1-2020</p></html>"
                    h2 = sha256_bytes(html_state["html"].encode("utf-8"))
                    err = io.StringIO()
                    ns2 = argparse.Namespace(force=True, readable=False, out=str(out_dir), source="mohurd")
                    ns2.app_context = AppContext.from_args(store, ns2, currency=cur, sources={"mohurd": src})
                    with redirect_stderr(err):
                        res2 = pull_app._pull_one(store, ns2, str(ann_id), src, out_dir)
                    self.assertEqual(res2["exit"], 0)
                    self.assertEqual(res2["body_sha256"], h2)
                    self.assertTrue(res2["body_changed"])
                    self.assertIn("内容指纹发生变化", err.getvalue())
                    self.assertEqual(store.get_ann(ann_id)["body_sha256"], h2)
                    self.assertEqual(store.probe_summary_by_source()["mohurd"]["body_changes"], 1)

                # API 源（无详情页 HTML）：返回 None/False 而不报错
                class FakeShzjw:
                    id = "shzjw"
                    display_name = "上海地标"
                    api_mode = True

                    def find(self, _ref):
                        return [{
                            "code": "DG/TJ08-2007-2026",
                            "name": "测试地标",
                            "status": "current",
                            "published_on": "2026-01-01",
                            "implement_on": "2026-05-01",
                            "page_url": "https://bzjg.zjw.sh.gov.cn/detail/1",
                            "download_url": "https://bzjg.zjw.sh.gov.cn:9175/dl/1",
                        }]

                    def download_to(self, _url, part_path, referer=None, probe_sink=None):
                        Path(part_path).parent.mkdir(parents=True, exist_ok=True)
                        Path(part_path).write_bytes(PDF)
                        return len(PDF), "pdf", 0

                api_src = FakeShzjw()
                api_res = pull_app._pull_api(
                    store,
                    argparse.Namespace(force=False, readable=False),
                    "DG/TJ08-2007-2026",
                    api_src,
                    out_dir,
                )
                self.assertEqual(api_res["exit"], 0)
                self.assertIsNone(api_res["body_sha256"])
                self.assertFalse(api_res["body_changed"])
                api_cached = pull_app._pull_api(
                    store,
                    argparse.Namespace(force=False, readable=False),
                    "DG/TJ08-2007-2026",
                    api_src,
                    out_dir,
                )
                self.assertEqual(api_cached["exit"], 10)
                self.assertIsNone(api_cached["body_sha256"])
                self.assertFalse(api_cached["body_changed"])

    def test_resolve_pick_current_version_records_body_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with Store(root / "c.db") as store:
                id1, _ = store.upsert_announcement(
                    "https://www.mohurd.gov.cn/x/1.html",
                    "2010-01-01",
                    "关于发布《测试评价标准》的公告",
                    "测试评价标准",
                    "national",
                )
                id2, _ = store.upsert_announcement(
                    "https://www.mohurd.gov.cn/x/2.html",
                    "2024-01-01",
                    "关于发布《测试评价标准》的公告",
                    "测试评价标准",
                    "national",
                )
                store.commit()

                pages = {
                    "https://www.mohurd.gov.cn/x/1.html": ("<html>old GB 1-2010</html>", "发布GB 1-2010"),
                    "https://www.mohurd.gov.cn/x/2.html": ("<html>new GB 1-2024</html>", "发布GB 1-2024"),
                }

                class FakeMohurd:
                    id = "mohurd"

                    def detail(self, url):
                        return pages[url]

                    def attachments(self, _html):
                        return [Attachment(url="https://www.mohurd.gov.cn/dl/x.pdf", label="正文.pdf", kind="pdf")]

                cur = mock.Mock()
                cur.name = "native"
                cur.lookup.return_value = [
                    Record("GB 1-2010", "superseded", "测试评价标准", "2010-01-01", "csres"),
                    Record("GB 1-2024", "current", "测试评价标准", "2024-01-01", "csres"),
                ]

                target = resolve_app.resolve(
                    store,
                    "测试评价标准",
                    currency=cur,
                    allow_network=True,
                    sources={"mohurd": FakeMohurd()},
                )
                self.assertEqual(target["code_key"], "GB1-2024")
                self.assertEqual(
                    store.get_ann(id1)["body_sha256"],
                    sha256_bytes(pages["https://www.mohurd.gov.cn/x/1.html"][0].encode("utf-8")),
                )
                self.assertEqual(
                    store.get_ann(id2)["body_sha256"],
                    sha256_bytes(pages["https://www.mohurd.gov.cn/x/2.html"][0].encode("utf-8")),
                )


class MetadataProbeSinkTest(unittest.TestCase):
    def test_metadata_client_probe_sink_records_outcomes_and_context_manager(self):
        # 1) 200 OK
        records = []
        c = MetadataClient(
            policy=MetadataPolicy.default(),
            throttle=Throttle(0),
            transport=FakeTransport([
                FakeResponse(b'{"Total":0}', url=URL_CCSN,
                             headers={"Content-Type": "application/json", "Content-Length": "11"})
            ]),
            probe_sink=records.append,
        )
        c.post_form(URL_CCSN, {"a": "1"})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, "ok")
        self.assertEqual(records[0].status, 200)
        self.assertEqual(records[0].bytes, 11)
        self.assertEqual(records[0].attempts, 1)

        # 2) 429 retry then 200 -> 1 probe record with attempts=2
        records.clear()
        err_429 = urllib.error.HTTPError(URL_CCSN, 429, "slow", {"Content-Type": "text/html"}, io.BytesIO(b"slow"))
        c2 = MetadataClient(
            policy=MetadataPolicy.default(),
            throttle=Throttle(0),
            transport=FakeTransport([
                err_429,
                FakeResponse(b"{}", url=URL_CCSN,
                             headers={"Content-Type": "application/json", "Content-Length": "2"}),
            ]),
            probe_sink=records.append,
        )
        with mock.patch("guiju.net.metadata.time.sleep"):
            c2.post_form(URL_CCSN, {"a": "1"})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, "ok")
        self.assertEqual(records[0].attempts, 2)

        # 3) Truncated body -> SourceUnreachable + outcome="truncated"
        records.clear()
        c3 = MetadataClient(
            policy=MetadataPolicy.default(),
            throttle=Throttle(0),
            transport=FakeTransport([
                FakeResponse(b"{}", url=URL_CCSN,
                             headers={"Content-Type": "application/json", "Content-Length": "10"})
            ]),
            probe_sink=records.append,
        )
        with self.assertRaises(SourceUnreachable):
            c3.post_form(URL_CCSN, {"a": "1"})
        self.assertEqual(records[-1].outcome, "truncated")
        self.assertEqual(records[-1].status, 200)
        self.assertEqual(records[-1].bytes, 2)

        # 4) Blocked marker -> ComplianceDenied + outcome="blocked"
        records.clear()
        c4 = MetadataClient(
            policy=MetadataPolicy.default(),
            throttle=Throttle(0),
            transport=FakeTransport([
                FakeResponse("<html>验证码</html>".encode(), url=URL_CCSN, headers={"Content-Type": "text/html"})
            ]),
            probe_sink=records.append,
        )
        with self.assertRaises(ComplianceDenied):
            c4.post_form(URL_CCSN, {"a": "1"})
        self.assertEqual(records[-1].outcome, "blocked")

        # 5) 404 non-retryable -> outcome="http_error"
        records.clear()
        err_404 = urllib.error.HTTPError(URL_CCSN, 404, "missing", {"Content-Type": "text/html"}, io.BytesIO(b"no"))
        c5 = MetadataClient(
            policy=MetadataPolicy.default(),
            throttle=Throttle(0),
            transport=FakeTransport([err_404]),
            probe_sink=records.append,
        )
        with self.assertRaises(SourceUnreachable):
            c5.post_form(URL_CCSN, {"a": "1"})
        self.assertEqual(records[-1].outcome, "http_error")
        self.assertEqual(records[-1].status, 404)

        # 6) URLError exhausted -> outcome="network_error", attempts=2
        records.clear()
        c6 = MetadataClient(
            policy=MetadataPolicy.default(),
            throttle=Throttle(0),
            attempts=2,
            transport=FakeTransport([urllib.error.URLError("down"), urllib.error.URLError("down")]),
            probe_sink=records.append,
        )
        with mock.patch("guiju.net.metadata.time.sleep"), self.assertRaises(SourceUnreachable):
            c6.post_form(URL_CCSN, {"a": "1"})
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, "network_error")
        self.assertEqual(records[0].attempts, 2)

        # 7) MetadataClient context manager & post_form endpoint enforcement
        records.clear()
        with MetadataClient(
            policy=MetadataPolicy.default(),
            throttle=Throttle(0),
            transport=FakeTransport([
                FakeResponse(b'{"Total":0}', url=URL_CCSN,
                             headers={"Content-Type": "application/json", "Content-Length": "11"})
            ]),
            probe_sink=records.append,
        ) as client:
            self.assertEqual(client.post_form(URL_CCSN, {"a": "1"}), b'{"Total":0}')
            with self.assertRaises(ComplianceDenied):
                client.post_form("https://www.csres.com/s.jsp", {"a": "1"})
        self.assertEqual([r.outcome for r in records], ["ok", "blocked"])


class SourcesCommandTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(render.configure, as_json=False, quiet=False)
        self.addCleanup(render.set_catalog, None)

    def _seed_db(self, db_path: Path) -> None:
        with Store(db_path) as store:
            ann_id, _ = store.upsert_announcement(
                "https://www.mohurd.gov.cn/x/1.html",
                "2026-01-01",
                "关于发布《某标准》的公告",
                "某标准",
                "national",
            )
            store.record_body_hash(ann_id, sha256_bytes(b"<html>1</html>"), source="mohurd")
            store.set_meta("sync:mohurd:complete", "1")
            store.set_meta("sync:mohurd:synced_at", now())
            store.set_meta("sync:mohurd:total", "1")
            store.set_meta("sync:mohurd:seen", "1")
            store.log_probe("mohurd", "https://www.mohurd.gov.cn/", 200, 128, "ok", "seed")
            store.log_probe("shzjw", "https://bzjg.zjw.sh.gov.cn/", 502, None, "unreachable", "bad gateway")
            store.commit()

    def test_sources_offline_json_and_human_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "c.db"
            self._seed_db(db_path)

            class NoNetSource:
                def __init__(self, sid, display_name):
                    self.id = sid
                    self.display_name = display_name

                def catalog_page(self, *_a, **_k):
                    raise AssertionError("不带 --probe 不得访问网络")

            src_map = {
                "mohurd": NoNetSource("mohurd", "住建部"),
                "shzjw": NoNetSource("shzjw", "上海地标"),
            }
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict("os.environ", {"GUIJU_HOME": str(root)}),
                mock.patch("guiju.sources.registry", return_value=src_map),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                rc = main(["sources", "--json", "--db", str(db_path)])
            self.assertEqual(rc, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["probed"])
            self.assertEqual(payload["body_hash_coverage"], {"hashed": 1, "total": 1})
            by_id = {entry["source"]: entry for entry in payload["sources"]}
            self.assertEqual(by_id["mohurd"]["health"], "healthy")
            self.assertEqual(by_id["shzjw"]["health"], "degraded")
            self.assertEqual(by_id["shzjw"]["probe"]["recent_failures"][0]["outcome"], "unreachable")

            text_out, text_err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict("os.environ", {"GUIJU_HOME": str(root)}),
                mock.patch("guiju.sources.registry", return_value=src_map),
                redirect_stdout(text_out),
                redirect_stderr(text_err),
            ):
                rc_text = main(["sources", "--db", str(db_path)])
            self.assertEqual(rc_text, 0)
            self.assertIn("详情指纹 1/1", text_out.getvalue())
            self.assertIn("mohurd [healthy]", text_out.getvalue())
            self.assertIn("shzjw [degraded]", text_out.getvalue())

    def test_sources_probe_offline_conflict_is_bad_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "c.db"
            self._seed_db(db_path)
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict("os.environ", {"GUIJU_HOME": str(root)}),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                rc = main(["sources", "--probe", "--offline", "--json", "--db", str(db_path)])
            self.assertEqual(rc, 2)
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["reason"], "bad_usage")

    def test_sources_probe_handles_ok_unreachable_needs_human_and_stops_on_40(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "c.db"
            with Store(db_path) as init_store:
                init_store.set_meta("sync:mohurd:complete", "1")
                init_store.set_meta("sync:mohurd:synced_at", now())
                init_store.set_meta("sync:shzjw:complete", "1")
                init_store.set_meta("sync:shzjw:synced_at", now())
                init_store.commit()

            def _run_probe(mohurd_fn, shzjw_fn):
                s1 = mock.Mock()
                s1.id = "mohurd"
                s1.display_name = "住建部"
                s1.BASE = "https://www.mohurd.gov.cn"
                s1.catalog_page.side_effect = mohurd_fn
                s2 = mock.Mock()
                s2.id = "shzjw"
                s2.display_name = "上海地标"
                s2.BASE = "https://bzjg.zjw.sh.gov.cn"
                s2.catalog_page.side_effect = shzjw_fn
                out, err = io.StringIO(), io.StringIO()
                with (
                    mock.patch.dict("os.environ", {"GUIJU_HOME": str(root)}),
                    mock.patch("guiju.sources.registry", return_value={"mohurd": s1, "shzjw": s2}),
                    redirect_stdout(out),
                    redirect_stderr(err),
                ):
                    rc = main(["sources", "--probe", "--json", "--db", str(db_path)])
                return rc, json.loads(out.getvalue()), s1, s2

            # 1) 全通
            rc, payload, s1, s2 = _run_probe(
                lambda *_a, **_k: ([{"url": "u"}], 10),
                lambda *_a, **_k: ([{"url": "s"}], 5),
            )
            self.assertEqual(rc, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["probed"])
            self.assertEqual(s1.catalog_page.call_count, 1)
            self.assertEqual(s2.catalog_page.call_count, 1)

            # 2) 单源不可达 -> 20，但继续探测另一源并保留结果
            rc20, payload20, _, _ = _run_probe(
                lambda *_a, **_k: ([{"url": "u"}], 10),
                lambda *_a, **_k: (_ for _ in ()).throw(SourceUnreachable("shzjw timeout")),
            )
            self.assertEqual(rc20, 20)
            self.assertFalse(payload20["ok"])
            by_id20 = {e["source"]: e for e in payload20["sources"]}
            self.assertTrue(by_id20["mohurd"]["live_probe"]["ok"])
            self.assertFalse(by_id20["shzjw"]["live_probe"]["ok"])
            self.assertEqual(by_id20["shzjw"]["health"], "degraded")

            # 3) 单源改版 NeedsHuman -> 50 优先于 20
            rc50, payload50, _, _ = _run_probe(
                lambda *_a, **_k: (_ for _ in ()).throw(SourceUnreachable("m timeout")),
                lambda *_a, **_k: (_ for _ in ()).throw(NeedsHuman("html changed")),
            )
            self.assertEqual(rc50, 50)
            self.assertFalse(payload50["ok"])

            # 4) 命中 40 立即停止：不得继续探后续源
            rc40, payload40, s1_40, s2_40 = _run_probe(
                lambda *_a, **_k: (_ for _ in ()).throw(ComplianceDenied("captcha")),
                lambda *_a, **_k: ([{"url": "s"}], 5),
            )
            self.assertEqual(rc40, 40)
            self.assertFalse(payload40["ok"])
            self.assertEqual(payload40["reason"], "compliance")
            self.assertEqual(s1_40.catalog_page.call_count, 1)
            self.assertEqual(s2_40.catalog_page.call_count, 0)


if __name__ == "__main__":
    unittest.main()
