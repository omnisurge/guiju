"""P3-7 / P3-8a：cite --strict 失败体合同（真实 main()/stdout 入口）。

覆盖 C3：20/34/32 三条 strict 失败分支必须经 _fail() 投影到 stdout，
返回码不变，stdout 是纯 JSON，且失败体不含 cite/候选串。
另保留非 strict success 的 main() 用例，证明 cite 成功字段未被误删。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from guiju.cli import main, render
from guiju.currency import Record
from guiju.errors import NoCurrencyRecord, SourceUnreachable
from guiju.store.sqlite import Store


class _FakeCurrency:
    """替身：cite.run 内部会构造 CachedCurrency(store=...)。"""

    name = "fake"
    complete = True
    stale = False

    def __init__(self, inner=None, store=None):
        pass

    def lookup(self, code):
        return [Record(code="GB 1-2020", status="current", name="某标准", source="fake")]


class _UnreachableCurrency(_FakeCurrency):
    def lookup(self, code):
        raise SourceUnreachable("时效服务不可达（测试替身）")


class _NoRecordCurrency(_FakeCurrency):
    def lookup(self, code):
        raise NoCurrencyRecord("时效侧没有该编号（测试替身）")


class _SupersededCurrency(_FakeCurrency):
    def lookup(self, code):
        return [
            Record(code="GB 1-2020", status="superseded", name="某标准", source="fake",
                   replaces="GB 1-2010"),
            Record(code="GB 1-2022", status="current", name="某标准", source="fake"),
        ]


class _EmptyCurrency(_FakeCurrency):
    def lookup(self, code):
        return []


class _IncompleteCurrency(_FakeCurrency):
    complete = False
    incomplete_reason = "翻页上限截断，可能缺少记录"


class _IncompleteEmptyCurrency(_IncompleteCurrency):
    def lookup(self, code):
        return []


def _make_db():
    tmp = tempfile.TemporaryDirectory()
    db = Path(tmp.name) / "c.db"
    store = Store(db)
    store.upsert_announcement(
        "https://www.mohurd.gov.cn/x.html", "2022-01-01",
        "关于发布《某标准》的公告", "某标准", "national",
    )
    store.upsert_edition("GB1-2020", "GB 1-2020", None, "parsed")
    store.add_mention(1, "GB1-2020", "primary", "编号为GB 1-2020")
    store.commit()
    store.close()
    return tmp, db


class CiteMainContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db = _make_db()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(render.configure, as_json=False, quiet=False)

    def _run(self, fake, *, strict=True):
        out, err = io.StringIO(), io.StringIO()
        args = ["cite", "GB1-2020", "5.2.1", "--json", "--db", str(self.db)]
        if strict:
            args.insert(3, "--strict")
        with (
            mock.patch.dict("os.environ", {"GUIJU_NO_AUTO_SYNC": "1"}),
            mock.patch("guiju.currency.CachedCurrency", fake),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            rc = main(args)
        return rc, out.getvalue(), err.getvalue()

    def test_strict_unreachable_main_exit20_no_cite(self):
        rc, stdout, _ = self._run(_UnreachableCurrency)
        self.assertEqual(rc, 20)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["exit"], 20)
        self.assertNotIn("cite", payload)
        self.assertNotIn("《某标准》", stdout)
        self.assertEqual(payload["currency_check"]["state"], "unreachable")

    def test_strict_no_currency_record_main_exit34_no_cite(self):
        rc, stdout, _ = self._run(_NoRecordCurrency)
        self.assertEqual(rc, 34)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["exit"], 34)
        self.assertNotIn("cite", payload)
        self.assertNotIn("《某标准》", stdout)
        self.assertEqual(payload["currency_check"]["state"], "unsupported")

    def test_strict_empty_records_main_exit34_no_cite(self):
        rc, stdout, _ = self._run(_EmptyCurrency)
        self.assertEqual(rc, 34)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["exit"], 34)
        self.assertNotIn("cite", payload)
        self.assertNotIn("《某标准》", stdout)
        self.assertEqual(payload["currency_check"]["state"], "no_record")

    def test_strict_superseded_main_exit32_no_cite(self):
        rc, stdout, _ = self._run(_SupersededCurrency)
        self.assertEqual(rc, 32)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["exit"], 32)
        self.assertNotIn("cite", payload)
        self.assertNotIn("《某标准》", stdout)
        self.assertTrue(any("superseded" in w for w in payload["warnings"]))

    def test_non_strict_success_main_keeps_cite(self):
        rc, stdout, _ = self._run(_FakeCurrency, strict=False)
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["cite"], "《某标准》GB 1-2020 第5.2.1条")
        self.assertEqual(payload["warnings"], [])
        self.assertEqual(payload["currency_check"]["state"], "ok")

    def test_non_strict_unreachable_main_keeps_cite_and_warning(self):
        rc, stdout, _ = self._run(_UnreachableCurrency, strict=False)
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["cite"], "《某标准》GB 1-2020 第5.2.1条")
        self.assertTrue(any("时效服务不可达" in w for w in payload["warnings"]))
        self.assertEqual(payload["currency_check"]["state"], "unreachable")

    def test_non_strict_no_currency_record_main_keeps_cite(self):
        rc, stdout, _ = self._run(_NoRecordCurrency, strict=False)
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["cite"], "《某标准》GB 1-2020 第5.2.1条")
        self.assertTrue(any("未覆盖" in w for w in payload["warnings"]))
        self.assertEqual(payload["currency_check"]["state"], "unsupported")

    def test_strict_incomplete_main_exit32_no_cite(self):
        rc, stdout, _ = self._run(_IncompleteCurrency)
        self.assertEqual(rc, 32)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["exit"], 32)
        self.assertNotIn("cite", payload)
        self.assertNotIn("《某标准》", stdout)
        self.assertTrue(any("时效结果不完整" in w for w in payload["warnings"]))

    def test_strict_incomplete_empty_main_exit32_no_cite(self):
        rc, stdout, _ = self._run(_IncompleteEmptyCurrency)
        self.assertEqual(rc, 32)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], False)
        self.assertEqual(payload["exit"], 32)
        self.assertNotIn("cite", payload)
        self.assertNotIn("《某标准》", stdout)
        self.assertTrue(any("时效结果不完整" in w for w in payload["warnings"]))

    def test_non_strict_superseded_main_keeps_cite(self):
        rc, stdout, _ = self._run(_SupersededCurrency, strict=False)
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        self.assertIs(payload["ok"], True)
        self.assertEqual(payload["cite"], "《某标准》GB 1-2020 第5.2.1条")
        self.assertEqual(payload["status"], "superseded")
        self.assertTrue(any("superseded" in w for w in payload["warnings"]))


if __name__ == "__main__":
    unittest.main()
