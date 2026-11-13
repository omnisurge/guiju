"""fix_20261112：lookup.py 三处 except Exception 曾把契约错误吞掉。

被吞的 NeedsHuman(50)/ComplianceDenied(40)/QueryCancelled 会变成：
  - 详情页抓取处 continue → 候选被悄悄丢光 → 落 32 歧义
  - 时效定版处 return None → 同上落 32
  - 正文编号核实处 continue → 伪装成"编号对不上"
契约要求它们按原错误冒到 CLI 决策表，绝不能降级成 32/31。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.cli.lookup import resolve
from guiju.currency import Record
from guiju.currency.model import QueryCancelled
from guiju.errors import AmbiguousTarget, ComplianceDenied, NeedsHuman
from guiju.store.sqlite import Store

_CONTRACT_ERRORS = (NeedsHuman, ComplianceDenied, QueryCancelled)

#: setUp 里创建的库统一在模块结束时关闭，避免 ResourceWarning 掩盖真正的问题
_STORES: list[Store] = []


def tearDownModule() -> None:
    for store in _STORES:
        store.close()


def make_store() -> Store:
    """两条同名公告（建筑设计防火规范 2015 发布 + 2019 局部修订）→ exact_name 两条。"""
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


class FakeCurrency:
    name = "fake"

    def __init__(self, records=(), error=None):
        self.records = list(records)
        self.error = error

    def lookup(self, code):
        if self.error is not None:
            raise self.error
        return self.records


def _patch_source(error):
    """让 registry()["mohurd"].detail 抛指定错误（详情页抓取路径）。"""
    source = mock.Mock()
    source.detail.side_effect = error
    source.attachments.return_value = []
    return mock.patch("guiju.sources.registry", lambda: {"mohurd": source})


class PickVersionDetailFetchTest(unittest.TestCase):
    """_pick_current_version 的详情页抓取：契约错误不得被 continue 吞成 32。"""

    def setUp(self):
        self.store = make_store()

    def test_contract_errors_propagate(self):
        for err_cls in _CONTRACT_ERRORS:
            with self.subTest(error=err_cls.__name__), _patch_source(err_cls("x")), \
                 self.assertRaises(err_cls):
                resolve(self.store, "建筑设计防火规范", FakeCurrency())

    def test_generic_error_still_drops_candidates(self):
        """普通抓取失败仍按原样 continue，最终交还 32 歧义——韧性语义不变。"""
        with _patch_source(ValueError("boom")), self.assertRaises(AmbiguousTarget):
            resolve(self.store, "建筑设计防火规范", FakeCurrency())


class PickVersionCurrencyLoopTest(unittest.TestCase):
    """_pick_current_version 的时效查询：契约错误不得被 return None 吞成 32。"""

    def setUp(self):
        self.store = make_store()
        # 已缓存的 decisive 编号让详情页抓取整段跳过，直达 currency.lookup
        # （mention 外键要求 edition 先存在，故先 upsert_edition）
        self.store.upsert_edition("GB50016-2014", "GB 50016-2014", None, "parsed")
        self.store.add_mention(2, "GB50016-2014", "primary", "编号为GB 50016-2014")
        self.store.add_mention(3, "GB50016-2014", "target_of_revision", "局部修订为GB 50016-2014")

    def test_contract_errors_propagate(self):
        for err_cls in _CONTRACT_ERRORS:
            with self.subTest(error=err_cls.__name__):
                cur = FakeCurrency(error=err_cls("x"))
                with self.assertRaises(err_cls):
                    resolve(self.store, "建筑设计防火规范", cur)

    def test_generic_error_still_ambiguous(self):
        cur = FakeCurrency(error=ValueError("boom"))
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "建筑设计防火规范", cur)


class VerifyByPrimaryCodeTest(unittest.TestCase):
    """_verify_by_primary_code 的 fetch_detail：契约错误不得被 continue 吞掉。"""

    def setUp(self):
        self.store = make_store()
        # 同名命中 2 条 → suspicious → 进入正文编号核实
        self.cur = FakeCurrency(
            [Record(code="GB 50016-2014", status="current", name="建筑设计防火规范")]
        )

    def test_contract_errors_propagate(self):
        for err_cls in _CONTRACT_ERRORS:
            with self.subTest(error=err_cls.__name__):
                def fake_detail(url, _cls=err_cls):
                    raise _cls("x")

                with self.assertRaises(err_cls):
                    resolve(self.store, "GB 50016-2014", self.cur, fetch_detail=fake_detail)

    def test_generic_error_still_ambiguous(self):
        def fake_detail(url):
            raise ValueError("boom")

        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, "GB 50016-2014", self.cur, fetch_detail=fake_detail)


if __name__ == "__main__":
    unittest.main()
