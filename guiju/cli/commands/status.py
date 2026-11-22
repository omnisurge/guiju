"""status —— 一条命令说清"我手里有什么、缺什么、为什么缺"。

这个命令的存在意义：让 agent 不必猜就能知道自己是否处于可用状态，
并把「2014 年前无正文」这一事实作为**数据**而不是注释传达出去。
"""

from __future__ import annotations

import argparse

from ...autosync import MAX_AGE_DAYS
from ...domain.availability import FULL_TEXT_SINCE_YEAR, coverage_statement
from ...store.sqlite import SCHEMA_VERSION, Store
from .. import render


def _catalog_message(
    sources: list[dict],
    *,
    all_complete: bool,
    ages_known: bool,
    age_days: float | None,
) -> str:
    """解释顶层目录结论为什么可用/不可用。

    不允许“catalog_age_days=0.0 但 catalog_stale=true”这种自相矛盾的输出：
    age 只在所有源都完整且时间已知时才有值，其余情况必须说明是未完整还是未知。
    """
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


def run(store: Store, args: argparse.Namespace) -> int:
    st = store.stats()
    by_year = st["by_year"]
    pre = sum(c for y, c in by_year.items() if y and y.isdigit() and int(y) < FULL_TEXT_SINCE_YEAR)
    in_window = sum(c for y, c in by_year.items() if y and y.isdigit() and int(y) >= FULL_TEXT_SINCE_YEAR)

    from ... import currency as currency_mod
    from ...sources import registry

    sources = []
    for source_id in registry():
        meta = store.source_meta(source_id)
        meta["stale"] = (
            not meta["complete"]
            or meta["age_days"] is None
            or meta["age_days"] > MAX_AGE_DAYS
        )
        meta["catalog_meta_corrupt"] = store.catalog_meta_corrupt(source_id)
        sources.append(meta)

    # 顶层结论按“最坏源”取：任一源不完整/过期/时间未知，整体就不能算新鲜。
    # 只有所有源都完整且 age 已知时才给出整体 age（取最老源）；否则 None。
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

    probe = store.db.execute(
        "SELECT outcome, COUNT(*) c FROM probe GROUP BY outcome ORDER BY c DESC"
    ).fetchall()
    payload = {
        "ok": True,
        "db": str(store.path),
        "schema_version": SCHEMA_VERSION,
        "currency_source": currency_mod.default_currency().name,
        "currency_endpoint": currency_mod.endpoint_of(currency_mod.default_currency()),
        "probe": {row["outcome"]: row["c"] for row in probe},
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
    render.emit(payload)
    if payload["announcements"] and catalog_message:
        render.note(catalog_message)
    if not render.is_json():
        render.out(f"库：{payload['db']}（schema v{payload['schema_version']}）")
        render.out(f"公告 {st['announcements']} 条 · 版本 {st['editions']} 个 · 提及 {st['mentions']} 处")
        render.out(f"同步于 {st['synced_at'] or '尚未同步'}")
        for meta in sources:
            total = "?" if meta["total"] is None else meta["total"]
            seen = "?" if meta["seen"] is None else meta["seen"]
            state = "完整" if meta["complete"] else f"不完整（{meta['reason'] or '未知'}）"
            render.out(f"  {meta['source']}：{state} {seen}/{total}")
        render.out(f"时效服务：{payload['currency_source']}　请求台账：{payload['probe'] or '无'}")
        render.out(f"可得性：{coverage_statement()}")
        render.out(f"  {FULL_TEXT_SINCE_YEAR} 年及以后 {in_window} 条 / 之前 {pre} 条（后者通常无正文，不是故障）")
    return 0


def add_parser(sub, parents) -> None:
    sub.add_parser("status", parents=parents, help="查看本地库状态与可得性口径").set_defaults(func=run)
