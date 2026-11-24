"""应用层 status：只构造 status payload，不负责 JSON/人读投影。"""

from __future__ import annotations

from typing import Any

from ..autosync import MAX_AGE_DAYS
from ..domain.availability import FULL_TEXT_SINCE_YEAR, coverage_statement
from ..ports.store import StorePort


def _endpoint_of(cur: Any) -> str | None:
    """从端口对象读取实际端点；不 import 具体 currency 适配器。"""
    url = getattr(cur, "url", None)
    if url:
        return str(url)
    binary = getattr(cur, "binary", None)
    if binary:
        return str(binary)
    reason = getattr(cur, "reason", None)
    if reason:
        return f"不可用：{reason}"
    if getattr(cur, "name", "") == "native":
        return "native"
    return None


def _catalog_message(
    sources: list[dict],
    *,
    all_complete: bool,
    ages_known: bool,
    age_days: float | None,
) -> str:
    """解释顶层目录结论为什么可用/不可用。"""
    if not sources:
        return "没有可用的数据源配置"
    if not all_complete:
        detail = "; ".join(
            f"{s['source']}={s['reason'] or 'unknown'}"
            for s in sources
            if not s["complete"]
        )
        return f"存在未完整源（{detail}）；目录年龄不可用，运行 guiju sync 做完整同步"
    if not ages_known:
        missing = ", ".join(s["source"] for s in sources if s["age_days"] is None)
        return f"存在同步时间未知的源（{missing}）；目录年龄不可用，运行 guiju sync"
    if age_days is not None and age_days > MAX_AGE_DAYS:
        return f"目录已 {age_days:.1f} 天未更新：guiju sync（增量约 3 秒 / 2 个请求）"
    return ""


def build_status(
    store: StorePort, *, currency: Any, source_ids: tuple[str, ...]
) -> dict:
    """生成 status 的完整 JSON payload（CLI/MCP 共用）。"""
    cur = currency
    st = store.stats()
    by_year = st["by_year"]
    pre = sum(c for y, c in by_year.items() if y and y.isdigit() and int(y) < FULL_TEXT_SINCE_YEAR)
    in_window = sum(c for y, c in by_year.items() if y and y.isdigit() and int(y) >= FULL_TEXT_SINCE_YEAR)

    sources = []
    for source_id in source_ids:
        meta = store.source_meta(source_id)
        meta["stale"] = (
            not meta["complete"]
            or meta["age_days"] is None
            or meta["age_days"] > MAX_AGE_DAYS
        )
        meta["catalog_meta_corrupt"] = store.catalog_meta_corrupt(source_id)
        sources.append(meta)

    all_complete = all(meta["complete"] for meta in sources)
    ages_known = all(meta["age_days"] is not None for meta in sources)
    age = (
        round(max(meta["age_days"] for meta in sources), 2)
        if sources and all_complete and ages_known
        else None
    )
    catalog_stale = any(meta["stale"] for meta in sources)
    corrupts = [
        meta["catalog_meta_corrupt"]
        for meta in sources
        if meta["catalog_meta_corrupt"]
    ]
    catalog_meta_corrupt = corrupts[0] if corrupts else None
    catalog_message = _catalog_message(
        sources, all_complete=all_complete, ages_known=ages_known, age_days=age
    )

    return {
        "ok": True,
        "db": str(store.path),
        "schema_version": store.schema_version,
        "currency_source": cur.name,
        "currency_endpoint": _endpoint_of(cur),
        "probe": store.probe_counts(),
        "announcements": st["announcements"],
        "editions": st["editions"],
        "mentions": st["mentions"],
        "scanned": st["scanned"],
        "synced_at": st["synced_at"],
        "catalog_age_days": age,
        "catalog_stale": catalog_stale,
        "catalog_message": catalog_message,
        "catalog_meta_corrupt": catalog_meta_corrupt,
        "sources": sources,
        "latest_published_on": store.max_published_on(),
        "coverage": {
            "full_text_since_year": FULL_TEXT_SINCE_YEAR,
            "in_window": in_window,
            "pre_window": pre,
            "statement": coverage_statement(),
        },
        "by_year": by_year,
    }
