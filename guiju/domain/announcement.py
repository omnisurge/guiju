"""公告标题解析（与站点无关的通用规则）。

标题是唯一稳定出现在目录里的信息，标准编号反而要开详情页才有。
所以「名称 + 分类」必须在同步阶段就落库，否则 39% 无编号的公告永远搜不到。

名称是多值的（ADR 0004 D4）：标题里所有《》名称都要保留（names），doc_name
只作展示（第一个）。实测 2026-11-18 的本地目录 3256 条里有 7 条标题含 2-3 本书，
「关于批准发布《A》《B》的通知」按 B 查询时，拿 A 的正文就是抓错书。
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

#: 名称比对时忽略的包裹符号与空白：用户会带书名号，标题里的名称不会
_NAME_NOISE = re.compile(r"[《》〈〉\s\u3000]+")


def extract_names(title: str | None) -> tuple[str, ...]:
    """标题里全部《》名称（按出现顺序去重）。doc_name 只是其中第一个。"""
    names: dict[str, None] = {}
    for match in _NAME_RE.finditer(title or ""):
        name = match.group(1).strip()
        if name:
            names.setdefault(name, None)
    return tuple(names)


def names_of(title: str | None, doc_name: str | None = None) -> tuple[str, ...]:
    """公告的全部名称：优先标题；标题没有书名号时退回展示名。

    api_mode 源（上海地标）没有公告页标题，落库时 title 就是名称，
    所以这条回退不是兜底猜测，而是同一事实的另一种存法。
    """
    names = extract_names(title)
    if names:
        return names
    clean = (doc_name or "").strip()
    return (clean,) if clean else ()


def _norm_name(s: str | None) -> str:
    return _NAME_NOISE.sub("", s or "").casefold()


def contains_name(text: str | None, name: str | None) -> bool:
    """正文/证据片段里是否出现这本书名（忽略书名号与空白）。"""
    target = _norm_name(name)
    return bool(target) and target in _norm_name(text)


def match_name(names, ref: str | None) -> tuple[str | None, tuple[str, ...]]:
    """用户给的名称 → 公告名称里的那一本。

    返回 (唯一命中的名称, 全部命中的名称)。对上多本或一本都对不上都返回 None，
    由调用方交 33 —— 这里不做"取第一个"的兜底，那正是抓错书的来源。
    """
    target = _norm_name(ref)
    if not target:
        return None, ()
    pool = tuple(n for n in (names or ()) if _norm_name(n))
    exact = tuple(n for n in pool if _norm_name(n) == target)
    if exact:
        return (exact[0] if len(exact) == 1 else None), exact
    loose = tuple(n for n in pool if target in _norm_name(n) or _norm_name(n) in target)
    return (loose[0] if len(loose) == 1 else None), loose


def classify_title(title: str, rules: tuple[tuple[str, str], ...] = _CATEGORY_RULES) -> tuple[str, str]:
    """返回 (规范名, 分类)。名称取第一个《》（仅展示）；无书名号时回退为标题全文。"""
    names = extract_names(title)
    name = names[0] if names else (title or "").strip()
    category = "other"
    for needle, label in rules:
        if needle in (title or ""):
            category = label
            break
    return name, category
