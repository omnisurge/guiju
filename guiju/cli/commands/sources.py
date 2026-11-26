"""sources —— 数据源健康度、改版指纹覆盖度与只读探活（M4）。"""

from __future__ import annotations

import argparse

from ... import report as render
from ...application.sources import build_sources
from ...errors import BadUsage
from ...sources import registry
from ...store.sqlite import Store


def run(store: Store, args: argparse.Namespace) -> int:
    probe = bool(getattr(args, "probe", False))
    offline = bool(getattr(args, "offline", False))
    if probe and offline:
        raise BadUsage(
            "--probe 与 --offline 不能同时使用",
            hint="查看本地台账请去掉 --probe；在线探活请去掉 --offline",
        )
    ctx = getattr(args, "app_context", None)
    src_map = (getattr(ctx, "sources", None) if ctx is not None else None) or registry()
    payload, rc = build_sources(store, sources=src_map, probe=probe)
    render.emit(payload)
    if not render.is_json():
        cov = payload["body_hash_coverage"]
        render.out(f"库：{payload['db']}（详情指纹 {cov['hashed']}/{cov['total']}）")
        for entry in payload["sources"]:
            cat = entry["catalog"]
            pr = entry["probe"]
            total = "?" if cat["total"] is None else cat["total"]
            seen = "?" if cat["seen"] is None else cat["seen"]
            sync_state = "完整" if cat["complete"] else f"不完整（{cat['reason'] or '未知'}）"
            render.out(
                f"  {entry['source']} [{entry['health']}]：{sync_state} {seen}/{total}"
                f" · probe={pr['counts'] or '无'} · 指纹变更={pr['body_changes']}"
            )
            if "live_probe" in entry:
                lp = entry["live_probe"]
                if lp.get("ok"):
                    render.out(f"    探活：ok（items={lp.get('items')} total={lp.get('total')}）")
                else:
                    render.out(f"    探活：失败（exit={lp.get('exit')} {lp.get('error')}）")
            for fail in pr.get("recent_failures") or []:
                render.out(f"    近期异常：{fail['outcome']} {fail['url']} {fail['note']}".rstrip())
    return rc


def add_parser(sub, parents) -> None:
    p = sub.add_parser("sources", parents=parents, help="查看数据源健康度、探针台账与改版信号")
    p.add_argument("--probe", action="store_true", help="按 1.2s 限速对已注册数据源执行只读探活")
    p.add_argument("--offline", action="store_true", help="离线只读模式（与 --probe 互斥）")
    p.set_defaults(func=run)
