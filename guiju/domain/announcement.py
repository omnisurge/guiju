"""公告标题解析（与站点无关的通用规则）。

标题是唯一稳定出现在目录里的信息，标准编号反而要开详情页才有。
所以「名称 + 分类」必须在同步阶段就落库，否则 39% 无编号的公告永远搜不到。
"""

from __future__ import annotations

import re

_CATEGORY_RULES = (
    ("局部修订", "partial_revision"),
    ("英文版", "draft_en"),
    ("征求意见", "draft"),
    ("国家标准", "national"),
    ("行业标准", "industry"),
    ("地方标准", "local"),
)

_NAME_RE = re.compile(r"《([^》]+)》")


def classify_title(title: str, rules: tuple[tuple[str, str], ...] = _CATEGORY_RULES) -> tuple[str, str]:
    """返回 (规范名, 分类)。无书名号时名称回退为标题全文。"""
    m = _NAME_RE.search(title)
    name = m.group(1).strip() if m else (title or "").strip()
    category = "other"
    for needle, label in rules:
        if needle in (title or ""):
            category = label
            break
    return name, category
