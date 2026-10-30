"""verify —— 校验本地归档：缺失 / 被改 / 与登记不一致。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...errors import EXIT_NEEDS_HUMAN
from ...files import sha256_file
from ...store.sqlite import Store
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    rows = store.all_artifacts()
    results, bad = [], 0
    for rec in rows:
        path = Path(rec["local_path"])
        if not path.exists():
            status = "missing"
        elif sha256_file(path) != rec["sha256"]:
            status = "modified"
        else:
            status = "ok"
        bad += status != "ok"
        results.append(
            {"key": rec["code_key"], "path": str(path), "status": status, "sha256": rec["sha256"]}
        )
        if status != "ok":
            render.warn(f"{status}: {rec['code_key']} {path}")

    payload = {"ok": bad == 0, "total": len(results), "bad": bad, "files": results}
    render.emit(payload)
    if not render.is_json():
        render.note(f"{'✓' if bad == 0 else '✗'} 共 {len(results)} 个文件，异常 {bad} 个")
    return 0 if bad == 0 else EXIT_NEEDS_HUMAN


def add_parser(sub, parents) -> None:
    sub.add_parser("verify", parents=parents, help="校验本地归档完整性").set_defaults(func=run)
