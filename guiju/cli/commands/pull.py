"""pull —— 命令行参数定义与入口装配；编排在 guiju.application.pull。"""

from __future__ import annotations

import argparse
from dataclasses import replace

from ...application import AppContext
from ...application import pull as pull_app
from ...currency import CachedCurrency
from ...sources import registry
from ...store.sqlite import Store


def run(store: Store, args: argparse.Namespace, sources=None) -> int:
    ctx = getattr(args, "app_context", None)
    sources_map = (
        sources
        if sources is not None
        else (getattr(ctx, "sources", None) if ctx is not None else None)
    )
    if sources_map is None:
        sources_map = registry()
    currency = getattr(ctx, "currency", None) if ctx is not None else None
    if currency is None:
        currency = CachedCurrency(store=store)
    if ctx is None:
        args.app_context = AppContext.from_args(
            store, args, currency=currency, sources=sources_map
        )
    elif ctx.currency is not currency or ctx.sources is not sources_map:
        args.app_context = replace(ctx, currency=currency, sources=sources_map)
    return int(pull_app.run(store, args, sources=sources_map))


def add_parser(sub, parents) -> None:
    p = sub.add_parser("pull", parents=parents, help="下载规范正文（单条或批量，幂等）")
    p.add_argument("ref", nargs="?", default=None, help="编号 / 规范名 / 公告ID / 公告URL")
    p.add_argument("--list", default=None, help="批量清单文件（每行一个 ref，- 表示 stdin）")
    p.add_argument("--out", default=None, help="下载目录（默认 ~/.guiju/library）")
    p.add_argument("--readable", action="store_true", help="用人类可读文件名（默认带 sha256 指纹；同名文件在就跳过、不校验内容）")
    p.add_argument("--force", action="store_true", help="已存在也重新下载")
    p.add_argument("--source", default="mohurd")
    p.set_defaults(func=run)
