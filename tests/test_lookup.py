"""解析链：编号 → 权威名称 → 本地公告。这条链是 index 的替代品。"""

import tempfile
import unittest
from pathlib import Path

from guiju.cli.lookup import resolve
from guiju.currency import Record
from guiju.errors import AmbiguousTarget, GuijuError, NotIndexed
from guiju.store.sqlite import Store

#: setUp 里创建的库统一在模块结束时关闭，避免 ResourceWarning 掩盖真正的问题
_STORES: list[Store] = []


def tearDownModule() -> None:
    for store in _STORES:
        store.close()


class FakeCurrency:
    name = "fake"

    def __init__(self, records):
        self.records = records

    def lookup(self, code):
        return self.records


def make_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "c.db"
    store = Store(tmp)
    _STORES.append(store)
    store.upsert_announcement(
        "https://www.mohurd.gov.cn/a1.html", "2022-01-01",
        "住房和城乡建设部关于发布国家标准《建筑防火通用规范》的公告", "建筑防火通用规范", "national",
    )
    store.upsert_announcement(
        "https://www.mohurd.gov.cn/a2.html", "2015-05-01",
        "关于发布国家标准《建筑设计防火规范》的公告", "建筑设计防火规范", "national",
    )
    store.upsert_announcement(
        "https://www.mohurd.gov.cn/a3.html", "2019-01-01",
        "关于发布国家标准《建筑设计防火规范》局部修订的公告", "建筑设计防火规范", "partial_revision",
    )
    return store


class ByRefTest(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_by_url(self):
        got = resolve(self.store, "https://www.mohurd.gov.cn/a1.html", FakeCurrency([]))
        self.assertEqual(got["ann"]["doc_name"], "建筑防火通用规范")
        self.assertEqual(got["via"], "url")

    def test_by_id(self):
        got = resolve(self.store, "1", FakeCurrency([]))
        self.assertEqual(got["via"], "ann_id")

    def test_by_name_unique(self):
        got = resolve(self.store, "建筑防火通用规范", FakeCurrency([]))
        self.assertEqual(got["ann"]["id"], 1)
        self.assertEqual(got["via"], "name")

    def test_by_name_ambiguous(self):
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "建筑设计防火规范", FakeCurrency([]))

    def test_by_name_not_found(self):
        with self.assertRaises(NotIndexed):
            resolve(self.store, "钢结构设计标准", FakeCurrency([]))


class ByCodeWithoutIndexTest(unittest.TestCase):
    """核心用例：本地从未索引过任何编号，仍能靠权威名称定位公告。"""

    def setUp(self):
        self.store = make_store()

    def test_code_resolved_via_currency_name(self):
        cur = FakeCurrency([Record(code="GB 55037-2022", status="current", name="建筑防火通用规范")])
        got = resolve(self.store, "GB55037-2022", cur, allow_network=False)
        self.assertEqual(got["ann"]["id"], 1)
        self.assertEqual(got["via"], "code_via_currency")
        self.assertEqual(got["status"], "current")

    def test_superseded_status_is_surfaced(self):
        cur = FakeCurrency([Record(code="GB 55037-2017", status="superseded", name="建筑防火通用规范")])
        got = resolve(self.store, "GB55037-2017", cur, allow_network=False)
        self.assertEqual(got["status"], "superseded")

    def test_currency_name_ambiguous(self):
        cur = FakeCurrency([Record(code="X-1", status="current", name="建筑设计防火规范")])
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "X-1", cur, allow_network=False)

    def test_currency_has_no_name(self):
        with self.assertRaises(NotIndexed):
            resolve(self.store, "GB99999-2099", FakeCurrency([]))

    def test_currency_name_without_local_announcement(self):
        cur = FakeCurrency([Record(code="Y-1", status="current", name="钢结构设计标准")])
        with self.assertRaises(GuijuError):
            resolve(self.store, "Y-1", cur)

class RoleAndVerificationTest(unittest.TestCase):
    """两个会"拿错书"的坑：被别的公告提到、以及同名公告的核实。"""

    def setUp(self):
        self.store = make_store()

    def test_mentioned_code_is_not_the_publisher(self):
        """GB 50016-2014 只在《建筑防火通用规范》里被"代替"，不能因此定位到那本。"""
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(1, "GB50016-2014", "superseded_ref", "代替GB 50016-2014")
        cur = FakeCurrency([
            Record(code="GB 50016-2014", status="current", name="建筑设计防火规范",
                   implement_date="2015-05-01"),
        ])
        got = resolve(self.store, "GB 50016-2014", cur, allow_network=False)
        self.assertEqual(got["ann"]["doc_name"], "建筑设计防火规范")
        self.assertNotEqual(got["ann"]["id"], 1)

    def test_verification_picks_announcement_whose_text_has_the_code(self):
        cur = FakeCurrency([Record(code="GB 50016-2014", status="current", name="建筑设计防火规范")])

        def fake_detail(url):
            if url.endswith("a3.html"):
                return "现批准《建筑设计防火规范》为国家标准，编号为GB 50016-2014。"
            return "现批准《建筑设计防火规范》为国家标准，编号为GB 50099-2011。"

        got = resolve(self.store, "GB 50016-2014", cur, fetch_detail=fake_detail)
        self.assertEqual(got["ann"]["id"], 3)

    def test_name_mismatch_without_verification_evidence_is_refused(self):
        cur = FakeCurrency([Record(code="X-9", status="current", name="建筑抗震设计规范")])
        with self.assertRaises(NotIndexed):
            resolve(self.store, "X-9", cur, fetch_detail=lambda url: "编号为GB 50099-2011")


if __name__ == "__main__":
    unittest.main()
