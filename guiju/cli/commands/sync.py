"""sync —— 目录同步的显式入口。

抓取逻辑在 `guiju/syncengine.py`，与进程内自保鲜（`guiju/autosync.py`）共用同一份实现，
所以两条路径不会长出不同的行为。
"""

from __future__ import annotations

import argparse

from ...store.sqlite import Store
from ...syncengine import crawl
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    # --if-stale：给"我就想确认它是新的"场景；新则 0 请求秒退
    # 总数只查一次（原来条件里一次、payload 里又一次，白白扫两遍表）
    total_in_db = store.count_announcements()
    if args.if_stale is not None and total_in_db:
        age = store.catalog_age_days()
        if age is None:
            # None 分"没同步过"与"meta 坏了"：后者必须露出来，不能静默当旧库
            corrupt = store.catalog_meta_corrupt()
            if corrupt:
                render.note(f"! {corrupt}，按未同步处理继续抓取")
        elif age <= args.if_stale:
            render.emit(
                {
                    "ok": True,
                    "skipped": True,
                    "reason": "fresh",
                    "age_days": round(age, 2),
                    "total_in_db": total_in_db,
                    "synced_at": store.get_meta("synced_at"),
                    "db": str(store.path),
                }
            )
            render.note(f"目录 {age:.1f} 天前同步，未超过 {args.if_stale} 天阈值，跳过（0 请求）")
            return 0

    def on_page(page: int, count: int, fresh: int, seen: int, total: int) -> None:
        render.note(f"第 {page:>3} 页：{count:>3} 条，新增 {fresh:>3}（累计 {seen}/{total}）")

    payload = crawl(
        store,
        page_size=args.page_size,
        pages=args.pages,
        source=args.source,
        on_page=on_page,
    )
    payload["ok"] = True
    render.emit(payload)
    if not render.is_json():
        render.note(f"✓ 新增 {payload['new']} 条，库中共 {payload['total_in_db']} 条")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("sync", parents=parents, help="同步标准公告目录（增量）")
    p.add_argument("--pages", type=int, default=0, help="最多抓取页数，0 为不限")
    p.add_argument("--page-size", type=int, default=100, help="每页条数（100 已验证完整）")
    p.add_argument(
        "--if-stale",
        type=int,
        default=None,
        metavar="DAYS",
        help="仅当目录超过 DAYS 天未更新时才同步（0 = 只在库为空时同步）",
    )
    p.add_argument("--source", default="mohurd", help="数据源 id")
    p.set_defaults(func=run)
