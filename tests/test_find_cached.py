"""find_cached 的幂等判定：指纹匹配、readable 存在即算、stem 里的 glob 元字符。

旧实现把 stem 直接拼进 glob 模式：doc_name 含 ] / * 时模式失真，
曾按"已有"误命中或漏命中。现在走 glob.escape + 逐文件指纹复核。
"""

import tempfile
import unittest
from pathlib import Path

from guiju.domain import naming
from guiju.files import find_cached, sha256_bytes

BODY = b"%PDF-1.7 fake body for find_cached tests"
DIGEST = sha256_bytes(BODY)


def _write(dirp: Path, name: str, data: bytes = BODY) -> Path:
    p = dirp / name
    p.write_bytes(data)
    return p


class FindCachedTest(unittest.TestCase):
    def test_fingerprint_hit(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            want = naming.library_name("GB55037-2022", "建筑防火通用规范", DIGEST)
            _write(d, want)
            got = find_cached(d, "GB55037-2022", "建筑防火通用规范")
            self.assertIsNotNone(got)
            path, digest = got
            self.assertEqual(path.name, want)
            self.assertEqual(digest, DIGEST)

    def test_fingerprint_miss_on_modified_file(self):
        # 文件名声明的指纹与内容不符 → 不算已有（防"名字对、内容坏"误判）
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write(d, naming.library_name("GB1", "规范", "0" * 64))
            self.assertIsNone(find_cached(d, "GB1", "规范"))

    def test_readable_hit(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _write(d, naming.readable_name("GB1", "规范"))
            got = find_cached(d, "GB1", "规范", readable=True)
            self.assertIsNotNone(got)
            self.assertEqual(got[0].name, naming.readable_name("GB1", "规范"))

    def test_readable_miss(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(find_cached(Path(td), "GB1", "规范", readable=True))

    def test_stem_with_glob_metachars_is_escaped(self):
        # doc_name 含 ] 与 *：旧版 glob 模式会失真，现在必须精确命中本文件
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            name = "规范[2024]*修订"
            want = naming.library_name("GB1", name, DIGEST)
            _write(d, want)
            # 再放一个"模式失真时可能误中"的邻居文件
            _write(d, naming.library_name("GB1", "规范2024修订", DIGEST))
            got = find_cached(d, "GB1", name)
            self.assertIsNotNone(got)
            self.assertEqual(got[0].name, want)

    def test_no_match(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(find_cached(Path(td), "GB1", "规范"))


if __name__ == "__main__":
    unittest.main()
