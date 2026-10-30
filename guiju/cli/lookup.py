"""把用户给的引用（URL / 公告ID / 编号）解析成确定性目标。

铁律：多候选时报歧义，绝不替用户挑版本 —— 挑错会产生错误的引用依据。
"""

from __future__ import annotations

import sqlite3

from ..domain.codes import normalize_key
from ..errors import AmbiguousCode, GuijuError
from ..sources.mohurd import migrate_legacy_url
from ..store.sqlite import Store

#: 这两类角色可以作为「用户问的就是这个版本」的依据
_DECISIVE_ROLES = ("primary", "target_of_revision")


def resolve(store: Store, ref: str) -> dict:
    ref = (ref or "").strip()
    if not ref:
        raise GuijuError("引用为空")

    if ref.lower().startswith("http"):
        row = store.get_ann_by_url(ref)
        if row is None:
            legacy = migrate_legacy_url(ref)
            if legacy:
                row = store.get_ann_by_url(legacy)
        if row is None:
            raise GuijuError(
                "该链接不在本地目录中，请先运行 guiju sync",
                hint="先 guiju sync 同步目录，再重试",
            )
        return {"ann": row, "code_key": None, "code_display": None, "confidence": "unknown"}

    if ref.isdigit():
        row = store.get_ann(int(ref))
        if row is None:
            raise GuijuError(f"本地目录中没有 ID={ref} 的公告")
        return {"ann": row, "code_key": None, "code_display": None, "confidence": "unknown"}

    key = normalize_key(ref)
    rows = store.find_by_code(key)
    if not rows:
        from ..errors import NotIndexed

        raise NotIndexed(f"编号 {ref} 尚未建立索引")

    decisive = [r for r in rows if r["role"] in _DECISIVE_ROLES] or list(rows)
    keys = {r["code_key"] for r in decisive}
    if len(keys) > 1:
        raise AmbiguousCode(
            f"编号 {ref} 命中 {len(keys)} 个候选版本，需要人工选定",
            candidates=[
                {
                    "code": r["code_display"],
                    "key": r["code_key"],
                    "published_on": r["published_on"],
                    "ann_id": r["id"],
                    "evidence": r["evidence"],
                }
                for r in _dedupe(decisive)
            ],
        )

    best = decisive[0]
    ann = store.get_ann(int(best["id"]))
    return {
        "ann": ann,
        "code_key": best["code_key"],
        "code_display": best["code_display"],
        "confidence": best["confidence"] or "parsed",
    }


def _dedupe(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["code_key"] in seen:
            continue
        seen.add(r["code_key"])
        out.append(r)
    return out
