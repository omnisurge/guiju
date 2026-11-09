"""verify —— 校验本地归档：缺失 / 被改 / 与登记不一致。

--repair 时把「挪了位置」的文件按 sha256 重新对位：local_path 只是缓存，
身份是内容指纹，重新指回不会破坏 provenance 的可信性。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...errors import EXIT_NEEDS_HUMAN
from ...files import sha256_file
from ...store.sqlite import Store
from .. import render

_SKIP_DIRS = {".partial"}


def _scan(dir_path: Path) -> dict[str, str]:
    """dir 下所有文件的 sha256 → 路径；.partial 与隐藏文件跳过。"""
    found: dict[str, str] = {}
    for p in dir_path.rglob("*"):
        if not p.is_file() or p.name.startswith(".") or p.suffix == ".part":
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            found[sha256_file(p)] = str(p)
        except OSError:
            continue
    return found


def run(store: Store, args: argparse.Namespace) -> int:
    rows = store.all_artifacts()
    known_shas = {r["sha256"] for r in rows}

    scan_dir = Path(args.dir).expanduser() if args.dir else store.path.parent / "library"
    disk: dict[str, str] = _scan(scan_dir) if args.repair and scan_dir.is_dir() else {}

    results, repairs, bad = [], [], 0
    for rec in rows:
        path = Path(rec["local_path"])
        if path.exists():
            status = "ok" if sha256_file(path) == rec["sha256"] else "modified"
        elif args.repair and rec["sha256"] in disk:
            new_path = disk[rec["sha256"]]
            store.repoint_artifact(rec["sha256"], new_path)
            status = "repaired"
            repairs.append({"key": rec["code_key"], "from": str(path), "to": new_path})
            path = Path(new_path)
        else:
            status = "missing"
        bad += status in ("missing", "modified")
        results.append(
            {"key": rec["code_key"], "path": str(path), "status": status, "sha256": rec["sha256"]}
        )
        if status != "ok":
            render.warn(f"{status}: {rec['code_key']} {path}")

    orphans = sorted(p for h, p in disk.items() if h not in known_shas) if disk else []
    if repairs:
        store.commit()

    payload = {
        "ok": bad == 0,
        "total": len(results),
        "bad": bad,
        "files": results,
        "repairs": repairs,
        "orphans": orphans,
        "scan_dir": str(scan_dir) if args.repair else None,
    }
    render.emit(payload)
    if not render.is_json():
        suffix = f"，修复 {len(repairs)}" if repairs else ""
        orphan_note = f"，库内未登记 {len(orphans)}" if orphans else ""
        render.note(
            f"{'✓' if bad == 0 else '✗'} 共 {len(results)} 个文件，异常 {bad} 个{suffix}{orphan_note}"
        )
    return 0 if bad == 0 else EXIT_NEEDS_HUMAN


def add_parser(sub, parents) -> None:
    p = sub.add_parser("verify", parents=parents, help="校验本地归档完整性")
    p.add_argument(
        "--repair",
        action="store_true",
        help="按内容指纹把挪动的文件重新对位（修 local_path），并列出未登记文件",
    )
    p.add_argument("--dir", default=None, help="--repair 的扫描目录（默认 <GUIJU_HOME>/library）")
    p.set_defaults(func=run)
