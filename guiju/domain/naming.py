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


def _stem_parts(code_key: str | None, name: str, name_limit: int, ann_id=None) -> list[str]:
    parts = [p for p in (_clean(code_key or "", 30), _clean(name, name_limit)) if p]
    if not parts:
        # 编号与名称都被清洗为空时不再统一叫 "untitled"：
        # 不同公告会塌成同一个主干，find_cached 的 glob 会把别的公告的文件算成"已有"。
        return [f"untitled-ann{ann_id}" if ann_id is not None else "untitled"]
    return parts


def library_stem(code_key: str | None, name: str, ann_id=None) -> str:
    """library_name 的主干（不含指纹后缀）：幂等快路径按它 glob 落盘文件。"""
    return "_".join(_stem_parts(code_key, name, 40, ann_id))


def library_name(code_key: str | None, name: str, sha256: str, ann_id=None) -> str:
    """{编号}_{名称}__{sha前12}.pdf —— 名字里带指纹，文件被改动会和名字对不上。"""
    return f"{library_stem(code_key, name, ann_id)}__{sha256[:12]}.pdf"


def readable_name(code_key: str | None, name: str, ann_id=None) -> str:
    stem = "_".join(_stem_parts(code_key, name, 60, ann_id))
    return f"{stem}.pdf"
