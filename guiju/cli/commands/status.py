"""status —— 参数与渲染；状态聚合逻辑在 guiju.application.status。"""

from __future__ import annotations

import argparse

from ...application.status import build_status
from ...domain.availability import FULL_TEXT_SINCE_YEAR, coverage_statement
from ...sources import registry
from ...store.sqlite import Store
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    ctx = getattr(args, "app_context", None)
    if ctx is not None:
        currency = ctx.currency_or_default()
    else:
        # 旧测试/嵌入调用直接传 Namespace：只在命令入口兜底，不进入 application。
        from ...currency import default_currency

        currency = default_currency()
    sources = getattr(ctx, "sources", None) if ctx is not None else None
    payload = build_status(store, currency=currency, source_ids=tuple(sources or registry()))
    render.emit(payload)
    if payload["announcements"] and payload["catalog_message"]:
        render.note(payload["catalog_message"])
    if not render.is_json():
        render.out(f"库：{payload['db']}（schema v{payload['schema_version']}）")
        render.out(
            f"公告 {payload['announcements']} 条 · 版本 {payload['editions']} 个 · "
            f"提及 {payload['mentions']} 处"
        )
        render.out(f"同步于 {payload['synced_at'] or '尚未同步'}")
        for meta in payload["sources"]:
            total = "?" if meta["total"] is None else meta["total"]
            seen = "?" if meta["seen"] is None else meta["seen"]
            state = "完整" if meta["complete"] else f"不完整（{meta['reason'] or '未知'}）"
            render.out(f"  {meta['source']}：{state} {seen}/{total}")
        render.out(
            f"时效服务：{payload['currency_source']}　请求台账：{payload['probe'] or '无'}"
        )
        render.out(f"可得性：{coverage_statement()}")
        render.out(
            f"  {FULL_TEXT_SINCE_YEAR} 年及以后 {payload['coverage']['in_window']} 条 / "
            f"之前 {payload['coverage']['pre_window']} 条（后者通常无正文，不是故障）"
        )
    return 0


def add_parser(sub, parents) -> None:
    sub.add_parser("status", parents=parents, help="查看本地库状态与可得性口径").set_defaults(func=run)
