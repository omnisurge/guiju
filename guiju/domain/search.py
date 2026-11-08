"""检索评分 —— 纯逻辑，可离线单测。

为什么不用 SQL 的 `LIKE` 硬扛：
  1. 用户的自然写法带书名号（《防火》），`LIKE '%《防火》%'` 直接命中 0 条
  2. 中文标题会在词组中间插入修饰词：「防火规范」匹配不到《建筑防火通用规范》
  3. 结果需要相关性排序，而 `ORDER BY date DESC` 会把"最相关"埋在第 8 页

数据量只有 3254 条，把候选读进内存打分比给 SQLite 建全文索引便宜得多，
也更容易解释与测试。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: 检索时应当被忽略的包裹符号（用户很自然地会带书名号）
_WRAPPERS = "《》〈〉「」『』【】\"'\u201c\u201d\u2018\u2019()（）[]"
_SEPARATORS = re.compile(r"[\s,，、;；/|+]+")

#: 词元至少这么长才做二元切分（单字/双字直接子串匹配）
_BIGRAM_MIN_LEN = 3


@dataclass(frozen=True)
class Hit:
    entry: dict
    score: int
    match: str  # exact_code / exact_name / prefix_name / contains_name / contains_title / fuzzy


def normalize(text: str) -> str:
    """统一大小写、全角、空白与包裹符号。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).strip()
    for ch in _WRAPPERS:
        s = s.replace(ch, "")
    return re.sub(r"\s+", "", s).upper()


def tokens_of(query: str) -> list[str]:
    """把查询拆成词元；带分隔符则按分隔符，否则整体作为一个词元。"""
    s = unicodedata.normalize("NFKC", query or "")
    for ch in _WRAPPERS:
        s = s.replace(ch, "")
    parts = [p for p in _SEPARATORS.split(s) if p]
    return [re.sub(r"\s+", "", p).upper() for p in parts]


def bigrams(token: str) -> list[str]:
    if len(token) < _BIGRAM_MIN_LEN:
        return [token]
    return [token[i : i + 2] for i in range(len(token) - 1)]


def _haystack(entry: dict) -> tuple[str, str, str]:
    name = normalize(entry.get("name") or "")
    title = normalize(entry.get("title") or "")
    codes = " ".join(normalize(c) for c in entry.get("codes") or [])
    return name, title, codes


def _score_token(name: str, title: str, codes: str, token: str) -> tuple[int, str]:
    if token and token in codes:
        return 900, "contains_code"
    if not token:
        return 0, ""
    if name == token:
        return 1200, "exact_name"
    if name.startswith(token):
        return 1000, "prefix_name"
    if token in name:
        return 800, "contains_name"
    if token in title:
        return 600, "contains_title"
    return 0, ""


def rank(entries: list[dict], tokens: list[str]) -> tuple[list[Hit], str]:
    """返回 (排序后的命中, 模式)。模式: and / fuzzy。"""
    tokens = [t for t in tokens if t]
    if not tokens:
        return [], "and"

    strict: list[Hit] = []
    strict_ids: set = set()
    for entry in entries:
        name, title, codes = _haystack(entry)
        total, kinds = 0, []
        matched_all = True
        for token in tokens:
            score, kind = _score_token(name, title, codes, token)
            if score == 0:
                matched_all = False
                break
            total += score
            kinds.append(kind)
        if matched_all:
            strict.append(Hit(entry, total, kinds[0] if len(kinds) == 1 else "multi"))
            strict_ids.add(entry.get("id"))

    # 模糊补召回：按二元覆盖度打分，阈值 0.5。
    # 「防火规范」→ 防火/火规/规范：能捞回《建筑防火通用规范》，同时排除
    # 《外墙外保温用防火分隔条》(1/3)、《煤化工工程设计防火标准》(1/3)。
    grams = [g for t in tokens for g in bigrams(t)]
    # 允许"缺一个字"的容差：短词要求全覆盖，长词允许差 1 个二元组。
    # 容差不能放开为比例阈值 —— 「建筑设计防火规范」(7 个二元组) 用 50% 阈值会
    # 把一堆只沾「建筑/设计/规范」的条目捞进来（实测 3 条变 48 条）。
    need = max(1, len(grams) - 1) if len(grams) >= 3 else len(grams)

    fuzzy: list[Hit] = []
    for entry in entries:
        if entry.get("id") in strict_ids:
            continue
        name, title, codes = _haystack(entry)
        hay = f"{name} {title} {codes}"
        matched = sum(1 for g in grams if g in hay)
        if matched < need:
            continue
        fuzzy.append(Hit(entry, int(700 * matched / max(1, len(grams))), "fuzzy"))

    # 第三档（仅在召回太少时启用）：字面覆盖。
    # 中文标准的常见变形是"插字"：《给排水》↔《建筑给水排水设计规范》，
    # 二元组必然断开，但组成字符全在。只在 strict+fuzzy 少于 3 条时才放宽，
    # 否则会把所有含"防/火/规/范"的条目扫进来。
    char_hits: list[Hit] = []
    if len(strict) + len(fuzzy) < 3:
        taken = strict_ids | {h.entry.get("id") for h in fuzzy}
        chars = {ch for t in tokens for ch in t if not ch.isascii()}
        if len(chars) >= 3:
            for entry in entries:
                if entry.get("id") in taken:
                    continue
                # 只看名称与编号：标题常被"等N项标准"式公告污染，放开会误召回
                name, _title, codes = _haystack(entry)
                if all(ch in f"{name} {codes}" for ch in chars):
                    char_hits.append(Hit(entry, 300, "fuzzy_chars"))

    parts = []
    if strict:
        parts.append("and")
    if fuzzy:
        parts.append("fuzzy")
    if char_hits:
        parts.append("chars")
    return _sorted(strict) + _sorted(fuzzy) + _sorted(char_hits), "+".join(parts) or "and"


def _year_of(published_on: object) -> int:
    """published_on 前四位是年份；非数字（"待定"）按 0 算，排在最旧。"""
    try:
        return int(str(published_on or "")[:4] or "0")
    except ValueError:
        return 0


def _sorted(hits: list[Hit]) -> list[Hit]:
    return sorted(
        hits,
        key=lambda h: (
            -h.score,
            len(h.entry.get("name") or ""),          # 名称越短越精确
            -_year_of(h.entry.get("published_on")),
            h.entry.get("id") or 0,
        ),
    )
