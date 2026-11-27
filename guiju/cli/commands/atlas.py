"""atlas —— 标准图集完整目录同步、检索与清单查看（国标电子书库只读元数据）。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ... import report as render
from ...application.atlas import list_atlas, search_atlas, sync_atlas
from ...domain import atlas as atlas_domain
from ...errors import EXIT_OK
from ...net.metadata import MetadataClient
from ...store.atlas import AtlasCatalogStore
from ...store.sqlite import Store

_STATUS_CN = {
    "current": "现行",
    "superseded": "被替代",
    "upcoming": "即将实施",
    "withdrawn": "废止",
    "unknown": "未知",
}


def _fetch_catalogue() -> bytes:
    with MetadataClient() as client:
        return client.get(atlas_domain.CONCISE_CATALOGUE_URL)


def _atlas_store(store: Store) -> AtlasCatalogStore:
    return AtlasCatalogStore(Path(store.path).parent / "atlas-catalog.json")


def _run_sync(store: Store, args: argparse.Namespace) -> int:
    payload = sync_atlas(
        _atlas_store(store),
        fetch_catalogue=_fetch_catalogue,
        if_stale=args.if_stale,
        offline=bool(args.offline),
    )
    render.emit(payload)
    if not render.is_json():
        if payload.get("skipped"):
            render.note(
                f"图集目录 {payload['age_days']:.1f} 天前同步，未超过 {args.if_stale} 天阈值，跳过（0 请求）"
            )
        else:
            counts = payload.get("counts") or {}
            render.note(
                f"✓ 已同步图集目录 {payload['total']} 条"
                f"（现行 {counts.get('current', 0)} · 被替代 {counts.get('superseded', 0)}"
                f" · 废止 {counts.get('withdrawn', 0)}）→ {payload['path']}"
            )
    return EXIT_OK


def _render_entries(payload: dict, *, empty_label: str) -> None:
    if not payload.get("catalog_ready"):
        render.out(payload["hint"])
        return
    results = payload.get("results") or []
    total = payload.get("total", len(results))
    if not total:
        render.out(f"未命中图集记录（目录共 {payload['catalog_total']} 条）。{payload.get('hint', '')}".rstrip())
        return
    mode_tag = "（模糊匹配）" if payload.get("mode") == "fuzzy" else ""
    render.out(f"命中 {total} 条{mode_tag}（目录共 {payload['catalog_total']} 条 · {empty_label}）：")
    for item in results:
        label = _STATUS_CN.get(item["status"], item["status"])
        rep = f"  [替代 → {item['replaced_by']}]" if item.get("replaced_by") else ""
        render.out(f"  [{label}] {item['code']:<18} {item['name']}{rep}")
    if payload.get("truncated"):
        render.out(f"  还有 {payload['total'] - payload['count']} 条，用 --all 或 --limit N 查看")


def _run_search(store: Store, args: argparse.Namespace) -> int:
    query = " ".join(args.keywords)
    payload = search_atlas(
        _atlas_store(store),
        query=query,
        status=args.status,
        prefix=args.prefix,
        limit=args.limit,
        show_all=bool(args.all),
    )
    render.emit(payload)
    if not render.is_json():
        _render_entries(payload, empty_label=f"检索 {query}")
    return EXIT_OK


def _run_list(store: Store, args: argparse.Namespace) -> int:
    payload = list_atlas(
        _atlas_store(store),
        status=args.status,
        prefix=args.prefix,
        limit=args.limit,
        show_all=bool(args.all),
    )
    render.emit(payload)
    if not render.is_json():
        _render_entries(payload, empty_label=f"筛选 {payload['status']}")
    return EXIT_OK


def _run_root(_store: Store, _args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if render.is_json():
        render.emit({"ok": True, "subcommands": ["sync", "search", "list"]})
    else:
        parser.print_help()
    return EXIT_OK


def add_parser(sub, parents) -> None:
    p = sub.add_parser("atlas", parents=parents, help="标准图集完整目录同步与本地检索（国标/中南/省标图集）")
    p.set_defaults(func=lambda store, args, _p=p: _run_root(store, args, _p))
    sub_atlas = p.add_subparsers(dest="atlas_cmd")

    p_sync = sub_atlas.add_parser("sync", parents=parents, help="单次同步全站标准图集简明目录（不下载正文）")
    p_sync.add_argument(
        "--if-stale",
        type=int,
        default=None,
        metavar="DAYS",
        help="仅当图集目录超过 DAYS 天未更新时才同步（0 = 仅目录为空时同步）",
    )
    p_sync.add_argument("--offline", action="store_true", help="离线模式下拒绝发请求")
    p_sync.set_defaults(func=_run_sync)

    p_search = sub_atlas.add_parser("search", parents=parents, help="检索本地已同步图集目录（不联网）")
    p_search.add_argument("keywords", nargs="+", help="图集号或名称关键词")
    p_search.add_argument(
        "--status",
        default="all",
        choices=("all", "current", "withdrawn", "superseded", "upcoming"),
        help="按状态过滤（默认 all；withdrawn 含废止与被替代）",
    )
    p_search.add_argument("--prefix", default="", help="按省别/专业前缀筛选（如 粤、G101、ZJ）")
    p_search.add_argument("--limit", type=int, default=20, help="最多显示条数")
    p_search.add_argument("--all", action="store_true", help="显示全部命中")
    p_search.set_defaults(func=_run_search)

    p_list = sub_atlas.add_parser("list", parents=parents, help="浏览本地完整图集目录清单（不联网）")
    p_list.add_argument(
        "--status",
        default="all",
        choices=("all", "current", "withdrawn", "superseded", "upcoming"),
        help="按状态过滤（默认 all；withdrawn 含废止与被替代）",
    )
    p_list.add_argument("--prefix", default="", help="按省别/专业前缀筛选（如 粤、G101、ZJ）")
    p_list.add_argument("--limit", type=int, default=50, help="最多显示条数")
    p_list.add_argument("--all", action="store_true", help="显示全部条目")
    p_list.set_defaults(func=_run_list)
