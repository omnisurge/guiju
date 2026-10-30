"""批量清单解析 —— 允许行尾注释，让清单能直接带标准名。"""

import io
import sys
import tempfile
import unittest
from pathlib import Path

from guiju.cli.commands.pull import _read_refs


class ReadRefsTest(unittest.TestCase):
    def _file(self, text: str) -> str:
        path = Path(tempfile.mkdtemp()) / "list.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_strips_comments_and_blanks(self):
        path = self._file("# 头部注释\nGB 55001-2021\n\nGB 55002-2021  # 抗震\n")
        self.assertEqual(_read_refs(path), ["GB 55001-2021", "GB 55002-2021"])

    def test_inline_comment_with_names(self):
        path = self._file("GB 55037-2022    # 建筑防火通用规范\n")
        self.assertEqual(_read_refs(path), ["GB 55037-2022"])

    def test_keeps_names_with_book_marks(self):
        path = self._file("《建筑防火通用规范》\n")
        self.assertEqual(_read_refs(path), ["《建筑防火通用规范》"])

    def test_stdin(self):
        old = sys.stdin
        sys.stdin = io.StringIO("GB 55001-2021\n# skip\n")
        try:
            self.assertEqual(_read_refs("-"), ["GB 55001-2021"])
        finally:
            sys.stdin = old


if __name__ == "__main__":
    unittest.main()
