"""index —— 打开详情页，解析标准编号并落角色证据。"""

from __future__ import annotations

import argparse

from ...domain.codes import classify
from ...errors import SourceUnreachable
from ...sources import registry
from ...store.sqlite import Store, persist_codes
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    source = registry()[args.source]
    rows = store.announcements_without_mention(args.match, args.limit)
    if not rows:
        render.emit({"ok": True, "indexed": [], "note": "没有待索引的条目"})
        render.note("没有待索引的条目" + ("（匹配条件内）" if args.match else ""))
        return 0

    done: list[dict] = []
    skipped = 0
    for row in rows:
        try:
            _, text = source.detail(row["url"])
        except SourceUnreachable as exc:
            skipped += 1
            render.warn(f"跳过 [{row['id']}] {exc}")
            continue
        codes = classify(text, row["doc_name"])
        persist_codes(store, int(row["id"]), codes, row["doc_name"] or "")
        store.commit()
        done.append(
            {
                "id": row["id"],
                "name": row["doc_name"],
                "primary": codes.primary_display,
                "confidence": codes.confidence.value,
                "codes": codes.candidates,
            }
        )
        if not render.is_json():
            primary = codes.primary_display or "（正文无编号）"
            flag = "" if codes.confidence.value == "parsed" else f"  ⚠{codes.confidence.value}"
            render.note(f"  [{row['id']:>5}] {codes.confidence.value:<9} {primary}{flag}")

    payload = {"ok": True, "indexed": done, "skipped": skipped, "editions": store.stats()["editions"]}
    render.emit(payload)
    if not render.is_json():
        render.note(f"✓ 索引 {len(done)} 条，跳 {skipped} 条；库中版本号 {payload['editions']} 个")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("index", parents=parents, help="解析公告编号（建立编号索引）")
    p.add_argument("--match", default=None, help="只处理标题含该关键词的公告")
    p.add_argument("--limit", type=int, default=50, help="本次最多处理条数")
    p.add_argument("--source", default="mohurd")
    p.set_defaults(func=run)
