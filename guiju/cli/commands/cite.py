"""cite —— 命令行参数定义与入口装配；编排在 guiju.application.cite。"""

from __future__ import annotations

import argparse
from dataclasses import replace

from ...application import AppContext
from ...application import cite as cite_app
from ...currency import CachedCurrency, NullCurrency
from ...sources import registry
from ...store.sqlite import Store


def run(store: Store, args: argparse.Namespace) -> int:
    ctx = getattr(args, "app_context", None)
    offline = bool(getattr(args, "offline", getattr(ctx, "offline", False)))
    existing_currency = getattr(ctx, "currency", None) if ctx is not None else None
    if offline:
        currency = (
            existing_currency
            if isinstance(existing_currency, NullCurrency)
            else NullCurrency()
        )
    else:
        currency = existing_currency if existing_currency is not None else CachedCurrency(store=store)
    sources_map = getattr(ctx, "sources", None) if ctx is not None else None
    if sources_map is None:
        sources_map = registry()
    if ctx is None:
        args.app_context = AppContext.from_args(
            store, args, currency=currency, sources=sources_map
        )
    elif ctx.currency is not currency or ctx.sources is not sources_map:
        args.app_context = replace(ctx, currency=currency, sources=sources_map)
    return int(cite_app.run(store, args))


def add_parser(sub, parents) -> None:
    p = sub.add_parser("cite", parents=parents, help="生成标准引用串（含权威状态校验）")
    p.add_argument("ref", help="编号 / 规范名 / 公告ID / 公告URL")
    p.add_argument("clause", nargs="?", default=None, help="条款号，如 5.2.1")
    p.add_argument("--strict", action="store_true", help="存在未核实项或被替代时非零退出")
    p.add_argument("--offline", action="store_true", help="不访问时效服务")
    p.set_defaults(func=run)
