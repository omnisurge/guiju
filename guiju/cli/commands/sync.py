"""sync —— 目录增量同步。"""

from __future__ import annotations

import argparse

from ...domain.announcement import classify_title
from ...sources import registry
from ...store.sqlite import Store, now
from .. import render


def _classify(source, title: str) -> tuple[str, str]:
    fn = getattr(source, "classify_title", None)
    return fn(title) if callable(fn) else classify_title(title)


def run(store: Store, args: argparse.Namespace) -> int:
    source = registry()[args.source]
    page_size = args.page_size
    page = 1
    total_new = total_seen = total = 0
    empty_page = 0

    while True:
        items, total = source.catalog_page(page, page_size)
        if not items:
            break
        fresh = 0
        for item in items:
            name, category = _classify(source, item["title"])
            _, is_new = store.upsert_announcement(
                item["url"], item["date"], item["title"], name, category
            )
            fresh += int(is_new)
        store.commit()
        total_new += fresh
        total_seen += len(items)
        empty_page = empty_page + 1 if fresh == 0 else 0
        render.note(f"第 {page:>3} 页：{len(items):>3} 条，新增 {fresh:>3}（累计 {total_seen}/{total}）")

        if args.pages and page >= args.pages:
            break
        if empty_page >= 2:
            render.note("连续两页无新增，增量同步完成")
            break
        if total and page * page_size >= total:
            break
        page += 1

    store.set_meta("synced_at", now())
    store.set_meta("catalog_total", str(total or store.count_announcements()))
    store.commit()

    payload = {
        "ok": True,
        "new": total_new,
        "scanned": total_seen,
        "total_reported": total,
        "total_in_db": store.count_announcements(),
        "latest_published_on": store.max_published_on(),
        "synced_at": store.get_meta("synced_at"),
        "db": str(store.path),
    }
    render.emit(payload)
    if not render.is_json():
        render.note(f"✓ 新增 {total_new} 条，库中共 {payload['total_in_db']} 条")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("sync", parents=parents, help="同步标准公告目录（增量）")
    p.add_argument("--pages", type=int, default=0, help="最多抓取页数，0 为不限")
    p.add_argument("--page-size", type=int, default=100, help="每页条数（100 已验证完整）")
    p.add_argument("--source", default="mohurd", help="数据源 id")
    p.set_defaults(func=run)
