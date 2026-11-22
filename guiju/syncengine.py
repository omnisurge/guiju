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

#: 唯一页硬上限：源没有可用 total 或分页参数失效时不能无限翻页。
MAX_PAGES = 200
#: 允许连续多少个"没有新唯一 URL"的页继续往后翻：重复页后面可能还有新页。
MAX_DUPLICATE_PAGES = 2


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
      - 上一轮已完整时，连续两页 0 新增才允许 quiet stop（仅增量场景）；
      - 重复页（本页没有带来任何新的唯一 URL）不参与 quiet stop：
        未知 total 下连续 2 页 → no_progress；已知 total 时允许 2 页继续翻，
        超过 → duplicate_page；
      - 唯一页数达到 MAX_PAGES → truncated / page_limit。

    seen 只按**唯一 URL** 累加。站点同一页重复条目、或分页参数失效返回重复页时，
    用重复条目凑够 total 会把缺口写成 complete；这里按唯一 URL 计数，宁可不完整。
    """
    src = registry()[source]
    page = 1
    new_total = 0
    seen = 0
    seen_urls: set[str] = set()
    best_total: int | None = None
    quiet_pages = 0
    duplicate_pages = 0
    unique_pages = 0
    complete = truncated = False
    reason = ""

    # 按源读完成水位；source_meta 已封装 mohurd 旧 catalog_complete=="1" 兼容。
    was_complete = store.source_meta(source)["complete"]

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
        page_seen = 0
        for item in items:
            url = item["url"]
            if url not in seen_urls:
                seen_urls.add(url)
                seen += 1
                page_seen += 1
            name, category = _classify(src, item["title"])
            _, is_new = store.upsert_announcement(
                url, item["date"], item["title"], name, category
            )
            fresh += int(is_new)
        store.commit()
        new_total += fresh

        duplicate = page_seen == 0
        if duplicate:
            duplicate_pages += 1
        else:
            unique_pages += 1
            duplicate_pages = 0
            quiet_pages = quiet_pages + 1 if fresh == 0 else 0
        if on_page is not None:
            on_page(page, len(items), fresh, seen, best_total or 0)

        if pages and page >= pages:
            truncated = True
            reason = "truncated"
            break

        # 顺序：先登记唯一 URL，再判重复/无进展，最后才轮到 quiet stop。
        # 重复页上的 0 新增不是"目录已抓完"的证据，不能让 previous_complete 提前收工。
        if duplicate:
            if best_total is None and duplicate_pages >= 2:
                complete = False
                reason = "no_progress"
                break
            if duplicate_pages > MAX_DUPLICATE_PAGES:
                complete = False
                reason = "duplicate_page"
                break
        elif was_complete and quiet_pages >= 2:
            complete = True
            reason = "previous_complete"
            break
        elif best_total is not None and seen >= best_total:
            complete = True
            reason = "seen_ge_total"
            break

        if unique_pages >= MAX_PAGES:
            truncated = True
            reason = "page_limit"
            break

        page += 1

    if complete and not truncated:
        stamp = now()
        store.set_meta(f"sync:{source}:complete", "1")
        store.set_meta(f"sync:{source}:reason", reason)
        store.set_meta(f"sync:{source}:synced_at", stamp)
        if source == "mohurd":
            # 只保留旧 synced_at 兼容读；旧版 catalog_complete 不再写，
            # 所有“完整”判断都以 sync:<source>:complete 为准。
            store.set_meta("synced_at", stamp)
    else:
        store.set_meta(f"sync:{source}:complete", "0")
        store.set_meta(f"sync:{source}:reason", reason)
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
