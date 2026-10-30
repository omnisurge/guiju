"""search —— 本地目录检索（不联网、秒回）。"""

from __future__ import annotations

import argparse

from ...domain.availability import coverage_statement
from ...store.sqlite import Store
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    keywords = [k for k in args.keywords if k]
    if not keywords:
        render.emit({"ok": True, "results": [], "note": "未给关键词"})
        return 0
    rows = store.search(keywords, args.limit, args.status)
    merged: dict[int, dict] = {}
    for row in rows:
        item = merged.get(row["id"])
        if item is None:
            item = merged[row["id"]] = {
                "id": row["id"],
                "published_on": row["published_on"],
                "name": row["doc_name"],
                "category": row["category"],
                "title": row["title"],
                "url": row["url"],
                "codes": [],
            }
        if row["code_display"]:
            item["codes"].append(
                {"code": row["code_display"], "role": row["role"], "confidence": row["confidence"]}
            )
    results = list(merged.values())
    payload = {"ok": True, "query": keywords, "count": len(results), "results": results}
    if not results:
        payload["hint"] = "本地无匹配。若目录为空，请先 guiju sync。" + coverage_statement()
    render.emit(payload)
    if not render.is_json():
        if not results:
            render.note(f"没有匹配「{' '.join(keywords)}」的公告。若目录为空，请先 guiju sync")
            return 0
        render.note(f"命中 {len(results)} 条：")
        for r in results:
            primary = next((c["code"] for c in r["codes"] if c["role"] != "superseded_ref"), None)
            render.note(f"  [{r['id']:>5}] {r['published_on']}  {(primary or '（未索引）'):<20} {(r['name'] or '')[:34]}")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("search", parents=parents, help="检索本地目录（不联网）")
    p.add_argument("keywords", nargs="+", help="关键词，多个为「与」关系")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--status", default=None, help="按版本状态过滤：current/superseded/unknown")
    p.set_defaults(func=run)
