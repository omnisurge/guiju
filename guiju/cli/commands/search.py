"""search —— 本地目录检索（不联网、秒回、带相关性排序）。"""

from __future__ import annotations

import argparse

from ...domain.availability import coverage_statement
from ...domain.search import rank, tokens_of
from ...errors import BadUsage
from ...store.sqlite import Store
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    # 负数 limit 在切片语义里是"从尾部往回数"（hits[:-1] 丢最后一条），纯属误用：
    # 按 bad_usage(2) 拒绝，别闷声给出一份看起来正常的结果。
    if not args.all and args.limit is not None and args.limit < 0:
        raise BadUsage("--limit 不能为负数（要全部结果用 --all）")
    tokens = tokens_of(" ".join(args.keywords))
    entries = store.catalog_entries()
    hits, mode = rank(entries, tokens)

    total = len(hits)
    shown = hits if args.all else hits[: args.limit]
    results = [
        {
            "id": h.entry["id"],
            "published_on": h.entry["published_on"],
            "name": h.entry["name"],
            "category": h.entry["category"],
            "codes": h.entry["codes"],
            "match": h.match,
            "score": h.score,
            "url": h.entry["url"],
        }
        for h in shown
    ]
    payload = {
        "ok": True,
        "query": args.keywords,
        "tokens": tokens,
        "mode": mode,
        "total": total,
        "count": len(results),
        "truncated": total > len(results),
        "results": results,
    }
    if not hits:
        payload["hint"] = "本地无匹配。若目录为空，请先 guiju sync。" + coverage_statement()
    render.emit(payload)

    if not render.is_json():
        if not hits:
            render.note(f"没有匹配「{' '.join(args.keywords)}」的公告。若目录为空，请先 guiju sync")
            return 0
        tag = "（模糊匹配）" if mode == "fuzzy" else ""
        render.note(f"命中 {total} 条{tag}：")
        for r in results:
            code = r["codes"][0] if r["codes"] else "（未索引）"
            render.note(f"  [{r['id']:>5}] {r['published_on']}  {code:<20} {(r['name'] or '')[:34]}")
        if total > len(results):
            render.note(f"  还有 {total - len(results)} 条，用 --all 或 --limit N 查看")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("search", parents=parents, help="检索本地目录（不联网，支持模糊）")
    p.add_argument("keywords", nargs="+", help="关键词，空格分隔为「与」关系；书名号会被自动忽略")
    p.add_argument("--limit", type=int, default=20, help="最多显示多少条")
    p.add_argument("--all", action="store_true", help="显示全部命中")
    p.set_defaults(func=run)
