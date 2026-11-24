"""sync —— 参数与展示；目录同步编排在 guiju.application.sync。"""

from __future__ import annotations

import argparse

from ...application.sync import run_sync
from ...autosync import safe_sync
from ...store.sqlite import Store
from ...syncengine import crawl
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    def on_page(page: int, count: int, fresh: int, seen: int, total: int) -> None:
        render.note(f"第 {page:>3} 页：{count:>3} 条，新增 {fresh:>3}（累计 {seen}/{total}）")

    # crawl/safe_sync 以模块属性传入：既有测试在本模块 patch 它们时仍然生效。
    payload = run_sync(
        store,
        if_stale=args.if_stale,
        source=args.source,
        page_size=args.page_size,
        pages=args.pages,
        on_page=on_page,
        notice=render.note,
        crawl_fn=crawl,
        lock_fn=safe_sync,
    )
    render.emit(payload)
    if payload.get("skipped"):
        if payload.get("reason") == "fresh":
            render.note(
                f"目录 {payload['age_days']:.1f} 天前同步，未超过 {args.if_stale} 天阈值，跳过（0 请求）"
            )
        elif payload.get("reason") == "refreshing":
            render.note("另一个进程正在同步目录；本次未抓取，建议 30 秒后重试")
    elif not render.is_json():
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
