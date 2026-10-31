"""status —— 一条命令说清"我手里有什么、缺什么、为什么缺"。

这个命令的存在意义：让 agent 不必猜就能知道自己是否处于可用状态，
并把「2014 年前无正文」这一事实作为**数据**而不是注释传达出去。
"""

from __future__ import annotations

import argparse

from ...domain.availability import FULL_TEXT_SINCE_YEAR, coverage_statement
from ...store.sqlite import Store
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    st = store.stats()
    age = store.catalog_age_days()
    by_year = st["by_year"]
    pre = sum(c for y, c in by_year.items() if y and y.isdigit() and int(y) < FULL_TEXT_SINCE_YEAR)
    in_window = sum(c for y, c in by_year.items() if y and y.isdigit() and int(y) >= FULL_TEXT_SINCE_YEAR)

    from ... import currency as currency_mod

    probe = store.db.execute(
        "SELECT outcome, COUNT(*) c FROM probe GROUP BY outcome ORDER BY c DESC"
    ).fetchall()
    payload = {
        "ok": True,
        "db": str(store.path),
        "schema_version": 2,
        "currency_source": currency_mod.default_currency().name,
        "probe": {row["outcome"]: row["c"] for row in probe},
        "announcements": st["announcements"],
        "editions": st["editions"],
        "mentions": st["mentions"],
        "scanned": st["scanned"],
        "artifacts": st["artifacts"],
        "synced_at": st["synced_at"],
        "catalog_age_days": None if age is None else round(age, 2),
        "catalog_stale": age is None or age > 7,
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
    if payload["announcements"] and payload["catalog_stale"]:
        render.note(f"目录已 {age:.1f} 天未更新：guiju sync（增量约 3 秒 / 2 个请求）")
    if not render.is_json():
        render.out(f"库：{payload['db']}（schema v{payload['schema_version']}）")
        render.out(f"公告 {st['announcements']} 条 · 版本 {st['editions']} 个 · 正文 {st['artifacts']} 份")
        render.out(f"同步于 {st['synced_at'] or '尚未同步'}")
        render.out(f"时效服务：{payload['currency_source']}　请求台账：{payload['probe'] or '无'}")
        render.out(f"可得性：{coverage_statement()}")
        render.out(f"  {FULL_TEXT_SINCE_YEAR} 年及以后 {in_window} 条 / 之前 {pre} 条（后者通常无正文，不是故障）")
    return 0


def add_parser(sub, parents) -> None:
    sub.add_parser("status", parents=parents, help="查看本地库状态与可得性口径").set_defaults(func=run)
