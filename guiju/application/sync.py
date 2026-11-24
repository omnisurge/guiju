"""应用层 sync：目录同步的显式用例编排。

CLI 只传参数和展示回调；同步锁/抓取实现通过参数注入，测试仍可在 CLI 层替换。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..ports.store import StorePort


def run_sync(
    store: StorePort,
    *,
    if_stale: int | None = None,
    source: str = "mohurd",
    page_size: int = 100,
    pages: int = 0,
    on_page: Callable[[int, int, int, int, int], None] | None = None,
    notice: Callable[[str], None] | None = None,
    crawl_fn: Callable[..., dict],
    lock_fn: Callable[..., Any],
) -> dict:
    """执行一次显式同步；抓取/锁由入口注入，application 不 import 具体实现。"""
    total_in_db = store.count_announcements()
    if if_stale is not None and total_in_db:
        age = store.catalog_age_days(source)
        if age is None:
            corrupt = store.catalog_meta_corrupt(source)
            if corrupt and notice is not None:
                notice(f"! {corrupt}，按未同步处理继续抓取")
        elif age <= if_stale:
            meta = store.source_meta(source)
            if not meta["complete"]:
                if notice is not None:
                    notice("目录未标记完整，忽略 --if-stale，继续同步")
            else:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "fresh",
                    "age_days": round(age, 2),
                    "total_in_db": total_in_db,
                    "synced_at": meta["synced_at"],
                    "db": str(store.path),
                }

    with lock_fn(store) as locked:
        if not locked:
            return {
                "ok": True,
                "skipped": True,
                "reason": "refreshing",
                "state": "refreshing",
                "retry_after_seconds": 30,
                "total_in_db": total_in_db,
                "synced_at": store.source_meta(source)["synced_at"],
                "db": str(store.path),
            }
        payload = crawl_fn(
            store,
            page_size=page_size,
            pages=pages,
            source=source,
            on_page=on_page,
        )
    payload["ok"] = True
    return payload
