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
    """抓取目录直到有完整证据或无可新增。

    完成不再由"空页"或"page * page_size >= total"单独宣布：
      - 空页 + seen < total → 缺口（empty_page_before_total），不写完整/同步时间；
      - 已知 total 且 seen >= total → 完整；
      - 未知 total 且遇到空页 → 不把部分当完整；
      - 截断（--pages）→ 不写完整/同步时间；
      - 上一轮已完整时，连续两页 0 新增才允许 quiet stop（仅增量场景）。
    seen 按实际条目累加，所以站点每页少于请求 page_size（mohurd 实测 99/100）
    不会再用放大后的 page * page_size 提前宣布完整。
    """
    src = registry()[source]
    page = 1
    new_total = seen = 0
    best_total: int | None = None
    quiet_pages = 0
    complete = truncated = False
    reason = ""

    # 按源读完成水位；mohurd 过渡期兼容旧的全局键。
    source_complete = store.get_meta(f"sync:{source}:complete")
    if not source_complete and source == "mohurd":
        source_complete = store.get_meta("catalog_complete")
    was_complete = source_complete == "1"

    while True:
        items, page_total = src.catalog_page(page, page_size)
        if page_total is not None:
            try:
                page_total = int(page_total)
            except (TypeError, ValueError):
                page_total = 0
            if page_total:
                best_total = page_total if best_total is None else max(best_total, page_total)
            elif not items and best_total is None and seen == 0:
                # 只有"一条都没见到"时，0 才是空目录的明确证据；
                # 已见条目却拿到 total=0（计数元素失效）不能当空目录。
                best_total = 0

        if not items:
            if best_total is None:
                complete = False
                reason = "total_unknown"
            elif seen < best_total:
                complete = False
                reason = "empty_page_before_total"
            elif best_total == 0:
                complete = True
                reason = "empty_catalog"
            else:
                complete = True
                reason = "empty_page"
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
            on_page(page, len(items), fresh, seen, best_total or 0)

        if pages and page >= pages:
            truncated = True
            reason = "truncated"
            break
        if was_complete and quiet_pages >= 2:
            complete = True
            reason = "previous_complete"
            break
        if best_total is not None and seen >= best_total:
            complete = True
            reason = "seen_ge_total"
            break
        page += 1

    if complete and not truncated:
        stamp = now()
        store.set_meta(f"sync:{source}:complete", "1")
        store.set_meta(f"sync:{source}:reason", reason)
        store.set_meta(f"sync:{source}:synced_at", stamp)
        if source == "mohurd":
            # 只保留给旧版/旧调用方读；新逻辑不再用全局 complete 代替任何源。
            store.set_meta("catalog_complete", "1")
            store.set_meta("synced_at", stamp)
    else:
        store.set_meta(f"sync:{source}:complete", "0")
        store.set_meta(f"sync:{source}:reason", reason)
        if source == "mohurd":
            # 旧读者即使只看全局键，也不能把这一轮缺口当成完整。
            store.set_meta("catalog_complete", "0")
    store.set_meta(f"sync:{source}:total", "" if best_total is None else str(best_total))
    store.set_meta(f"sync:{source}:seen", str(seen))
    if source == "mohurd":
        store.set_meta(
            "catalog_total", str(best_total if best_total is not None else store.count_announcements())
        )
    store.commit()
    return {
        "source": source,
        "new": new_total,
        "scanned": seen,
        "total_reported": best_total,
        "total_in_db": store.count_announcements(),
        "latest_published_on": store.max_published_on(),
        "synced_at": store.source_meta(source)["synced_at"],
        "complete": complete,
        "complete_reason": reason,
        "truncated": truncated,
        "db": str(store.path),
    }
