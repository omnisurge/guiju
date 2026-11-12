"""目录同步引擎 —— 显式 sync 与进程内自保鲜共用同一实现。

单独成层的原因：`cli` 层负责输出（render），而"抓列表 + upsert"是纯动作。
自动刷新发生在命令执行之前，不能反向依赖 cli，否则形成循环导入。
"""

from __future__ import annotations

from collections.abc import Callable

from .domain.announcement import classify_title
from .sources import registry
from .store.sqlite import Store, now

PageHook = Callable[[int, int, int, int, int], None]


def _classify(source, title: str) -> tuple[str, str]:
    fn = getattr(source, "classify_title", None)
    return fn(title) if callable(fn) else classify_title(title)


def crawl(
    store: Store,
    *,
    page_size: int = 100,
    pages: int = 0,
    source: str = "mohurd",
    on_page: PageHook | None = None,
) -> dict:
    """抓取目录直到完整或无可新增（连续两页 0 新增即停）。

    早停前提：上一轮的 corpus_meta["catalog_complete"] == "1"（目录已爬到底）。
    否则静默早停会掩盖缺口：首轮爬一半失败，重跑时前几页全是已入库条目，
    连续两页 0 新增即停，缺口之后的内容永远爬不到，而 synced_at 照常刷新。
    截断（--pages 截停的轮次）既不写 synced_at 也不置 catalog_complete。
    """
    src = registry()[source]
    page = 1
    new_total = seen = total = 0
    quiet_pages = 0
    # 上一轮已完整才允许增量早停；未知（默认/截断后）必须爬到底
    was_complete = store.get_meta("catalog_complete") == "1"
    completed = truncated = False

    while True:
        items, total = src.catalog_page(page, page_size)
        if not items:
            completed = True
            break
        fresh = 0
        for item in items:
            name, category = _classify(src, item["title"])
            _, is_new = store.upsert_announcement(
                item["url"], item["date"], item["title"], name, category
            )
            fresh += int(is_new)
        store.commit()
        new_total += fresh
        seen += len(items)
        quiet_pages = quiet_pages + 1 if fresh == 0 else 0
        if on_page is not None:
            on_page(page, len(items), fresh, seen, total)

        if pages and page >= pages:
            truncated = True
            break
        if was_complete and quiet_pages >= 2:
            break
        if total and page * page_size >= total:
            completed = True
            break
        page += 1

    complete = completed or was_complete
    if complete and not truncated:
        store.set_meta("catalog_complete", "1")
        store.set_meta("synced_at", now())
    store.set_meta("catalog_total", str(total or store.count_announcements()))
    store.commit()
    return {
        "new": new_total,
        "scanned": seen,
        "total_reported": total,
        "total_in_db": store.count_announcements(),
        "latest_published_on": store.max_published_on(),
        "synced_at": store.get_meta("synced_at"),
        "complete": complete,
        "truncated": truncated,
        "db": str(store.path),
    }
