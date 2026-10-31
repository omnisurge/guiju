"""which —— 一个编号（或名称）到底是什么：权威时效 + 本地公告 + 是否有正文。

这个命令是 index 的替代品：它不需要任何预建索引就能回答"GB 55037-2022 是哪本、现在什么状态、
我本地有没有它的公告和正文"。
"""

from __future__ import annotations

import argparse

from ... import currency as currency_mod
from ... import localstatus
from ...domain.availability import note as availability_note
from ...domain.availability import state
from ...errors import AmbiguousTarget, GuijuError, NotIndexed
from ...store.sqlite import Store
from .. import render
from ..lookup import resolve


def run(store: Store, args: argparse.Namespace) -> int:
    cur = currency_mod.CachedCurrency(store=store)
    records = cur.lookup(args.ref) if not args.offline else []
    source_name = cur.name
    if not records and not args.offline and localstatus.is_local_code(args.ref):
        # 地方标准：源站自己就有现行/废止结论，不必依赖外部时效服务
        try:
            records = localstatus.records(args.ref)
            source_name = "shzjw"
        except GuijuError:
            records = []

    target = None
    error = None
    try:
        target = resolve(
            store,
            args.ref,
            currency=currency_mod.NullCurrency() if args.offline else cur,
            allow_network=not args.offline,
        )
    except (NotIndexed, AmbiguousTarget) as exc:
        error = exc
    except Exception as exc:
        error = exc

    ann = target["ann"] if target else None
    payload = {
        "ok": True,
        "query": args.ref,
        "currency_source": source_name,
        "currency_from_cache": bool(getattr(cur, "from_cache", False)),
        "currency_cache_age_days": getattr(cur, "cache_age_days", None),
        "currency": [r.as_payload() for r in records],
        "resolved": bool(target),
        "announcement": None
        if ann is None
        else {
            "id": ann["id"],
            "name": ann["doc_name"],
            "published_on": ann["published_on"],
            "url": ann["url"],
            "availability": state(ann["published_on"]).value,
            "availability_note": availability_note(state(ann["published_on"]), ann["published_on"]),
        },
        "via": target["via"] if target else None,
        "code": target["code_display"] if target else None,
        "hint": getattr(error, "hint", "") if error else "",
    }
    if isinstance(error, AmbiguousTarget):
        payload["candidates"] = error.extra.get("candidates", [])
    render.emit(payload)
    if payload["currency_from_cache"]:
        render.note(
            f"时效服务不可达，使用本地缓存（{payload['currency_cache_age_days']} 天前）："
            "现行性可能已变化"
        )

    if not render.is_json():
        status_cn = {
            "current": "现行",
            "superseded": "被替代",
            "upcoming": "即将实施",
            "withdrawn": "废止",
            "unknown": "无法判定",
        }
        for r in records:
            render.out(
                f"{r.code}　{status_cn.get(r.status, r.status)}"
                f"　{r.name}" + (f"　实施 {r.implement_date}" if r.implement_date else "")
            )
        if ann is not None:
            render.out(f"本地公告：#{ann['id']} {ann['doc_name']}（{ann['published_on']}）")
            render.out(f"  {payload['announcement']['availability_note']}")
        elif error is not None:
            render.out(f"本地未定位：{getattr(error, 'message', str(error))}")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("which", parents=parents, help="查一个编号/名称：权威状态 + 本地公告")
    p.add_argument("ref", help="编号 或 规范名")
    p.add_argument("--offline", action="store_true", help="不访问时效服务")
    p.set_defaults(func=run)
