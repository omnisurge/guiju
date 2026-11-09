"""文件命名策略。

两套命名，各有明确用途：
  · library_name()  带 sha256 指纹的默认命名 —— 天然去重，文件被改动会和名字对不上
  · readable_name() 人读文件名 —— 仅在用户显式 --readable 时使用
"""

from __future__ import annotations

import re

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACES = re.compile(r"\s+")


def _clean(s: str, limit: int | None = None) -> str:
    s = _SPACES.sub("", _UNSAFE.sub("", s or "")).strip(" .")
    return s[:limit] if limit else s


def library_stem(code_key: str | None, name: str) -> str:
    """library_name 的主干（不含指纹后缀）：幂等快路径按它 glob 落盘文件。"""
    return "_".join(p for p in (_clean(code_key or "", 30), _clean(name, 40)) if p) or "untitled"


def library_name(code_key: str | None, name: str, sha256: str) -> str:
    """{编号}_{名称}__{sha前12}.pdf —— 名字里带指纹，文件被改动会和名字对不上。"""
    return f"{library_stem(code_key, name)}__{sha256[:12]}.pdf"


def readable_name(code_key: str | None, name: str) -> str:
    stem = "_".join(p for p in (_clean(code_key or "", 30), _clean(name, 60)) if p) or "untitled"
    return f"{stem}.pdf"
