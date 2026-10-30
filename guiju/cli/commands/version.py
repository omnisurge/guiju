"""version —— 版本与目录台账（人读与 JSON 双形态）。"""

from __future__ import annotations

import argparse

from ..._version import __version__
from ...store.sqlite import Store
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    stats = store.stats()
    payload = {
        "ok": True,
        "name": "guiju",
        "version": __version__,
        "catalog_synced_at": stats["synced_at"] or "未同步",
        "announcements": stats["announcements"],
        "editions": stats["editions"],
        "artifacts": stats["artifacts"],
        "db": str(store.path),
    }
    render.emit(payload)
    if not render.is_json():
        print(
            f"guiju {__version__} · catalog {payload['catalog_synced_at']} · "
            f"{payload['announcements']} 条公告 / {payload['editions']} 个版本"
        )
    return 0


def add_parser(sub, parents) -> None:
    sub.add_parser("version", parents=parents, help="显示版本与目录台账").set_defaults(func=run)
