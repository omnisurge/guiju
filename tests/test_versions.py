"""同名多公告 = 版本迭代：按时效定版，不把人叫来选。

实测背景：《绿色建筑评价标准》在住建部有 4 条公告（2024 局部修订 / 2019 发布 / 2014 / 2006），
正文编号解析为 GB/T 50378-2019（前两条）、GB/T 50378-2014、无编号。
正确答案是"2024 那条"，而不是 exit 32。
"""

import unittest

from guiju.cli.lookup import resolve
from guiju.currency import Record
from guiju.domain.codes import classify
from guiju.errors import AmbiguousTarget
from guiju.store.sqlite import Store, persist_codes

NAME = "绿色建筑评价标准"


class _FakeCurrency:
    name = "fake"

    def __init__(self, statuses):
        self.statuses = statuses

    def lookup(self, code):
        status = self.statuses.get(code.replace(" ", "").upper())
        if status is None:
            return []
        return [Record(code=code, status=status, name=NAME, source="fake")]


def _seed(store):
    rows = [
        ("https://example.gov.cn/a/2024", "2024-07-11", "GB/T 50378-2019"),
        ("https://example.gov.cn/a/2019", "2019-05-30", "GB/T 50378-2019"),
        ("https://example.gov.cn/a/2014", "2014-04-16", "GB/T 50378-2014"),
    ]
    ids = {}
    for url, day, code in rows:
        ann_id, _ = store.upsert_announcement(url, day, f"关于发布《{NAME}》的公告", NAME, "national")
        codes = classify(f"现批准《{NAME}》为国家标准，编号为 {code}，自发布之日起实施。", NAME)
        persist_codes(store, ann_id, codes, NAME)
        ids[day] = ann_id
    store.commit()
    return ids


class VersionPickingTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(str(Path(self.tmp.name) / "c.db"))
        self.addCleanup(self.store.close)
        self.ids = _seed(self.store)

    def test_picks_latest_announcement_of_current_code(self):
        cur = _FakeCurrency({"GB/T50378-2019": "current", "GB/T50378-2014": "superseded"})
        target = resolve(self.store, NAME, currency=cur)
        self.assertEqual(target["via"], "name_current_version")
        self.assertEqual(target["ann"]["published_on"], "2024-07-11")
        self.assertEqual(target["status"], "current")
        self.assertEqual(len(target["versions"]), 3)
        chosen = [v for v in target["versions"] if v["chosen"]]
        self.assertEqual(len(chosen), 1)
        self.assertEqual(chosen[0]["published_on"], "2024-07-11")

    def test_no_current_version_still_asks_human(self):
        cur = _FakeCurrency({"GB/T50378-2019": "superseded", "GB/T50378-2014": "withdrawn"})
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, NAME, currency=cur)

    def test_offline_cannot_verify_still_asks_human(self):
        # 没有缓存的编号且不允许联网 → 无法定版，退回人工而不是瞎猜
        with self.assertRaises(AmbiguousTarget):
            resolve(self.store, NAME, currency=_FakeCurrency({}), allow_network=False)


if __name__ == "__main__":
    unittest.main()
