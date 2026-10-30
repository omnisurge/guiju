"""解析链：编号 → 权威名称 → 本地公告。这条链是 index 的替代品。"""

import tempfile
import unittest
from pathlib import Path

from guiju.cli.lookup import resolve
from guiju.currency import Record
from guiju.errors import AmbiguousTarget, NotIndexed, GuijuError
from guiju.store.sqlite import Store


class FakeCurrency:
    name = "fake"

    def __init__(self, records):
        self.records = records

    def lookup(self, code):
        return self.records


def make_store() -> Store:
    tmp = Path(tempfile.mkdtemp()) / "c.db"
    store = Store(tmp)
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
        got = resolve(self.store, "GB55037-2022", cur)
        self.assertEqual(got["ann"]["id"], 1)
        self.assertEqual(got["via"], "code_via_currency")
        self.assertEqual(got["status"], "current")

    def test_superseded_status_is_surfaced(self):
        cur = FakeCurrency([Record(code="GB 55037-2017", status="superseded", name="建筑防火通用规范")])
        got = resolve(self.store, "GB55037-2017", cur)
        self.assertEqual(got["status"], "superseded")

    def test_currency_name_ambiguous(self):
        cur = FakeCurrency([Record(code="X-1", status="current", name="建筑设计防火规范")])
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "X-1", cur)

    def test_currency_has_no_name(self):
        with self.assertRaises(NotIndexed):
            resolve(self.store, "GB99999-2099", FakeCurrency([]))

    def test_currency_name_without_local_announcement(self):
        cur = FakeCurrency([Record(code="Y-1", status="current", name="钢结构设计标准")])
        with self.assertRaises(GuijuError):
            resolve(self.store, "Y-1", cur)


if __name__ == "__main__":
    unittest.main()
