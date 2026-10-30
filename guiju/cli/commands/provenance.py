"""provenance —— 反查来源。按内容指纹查，不依赖路径。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...domain.availability import note as availability_note
from ...domain.availability import state
from ...errors import NoAttachment
from ...files import sha256_file
from ...store.sqlite import Store
from .. import render


def run(store: Store, args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    if not path.exists():
        raise NoAttachment(f"文件不存在：{path}", hint="检查路径；本命令只对已下载的归档文件溯源")

    digest = sha256_file(path)
    rec = store.artifact_by_sha(digest)
    if rec is None:
        raise NoAttachment(
            "本地归档中查无此文件（内容指纹未登记）",
            hint="该文件可能是手工放入或已损坏；sha256 是唯一可信归属依据",
            sha256=digest,
        )

    ann = store.get_ann(int(rec["ann_id"]))
    window = state(ann["published_on"]) if ann else None
    payload = {
        "ok": True,
        "path": str(path),
        "sha256": digest,
        "bytes": rec["bytes"],
        "magic": rec["magic"],
        "fetched_at": rec["fetched_at"],
        "code_key": rec["code_key"],
        "attachment_url": rec["file_url"],
        "attachment_label": rec["label"],
        "announcement": {
            "id": ann["id"] if ann else None,
            "title": ann["title"] if ann else None,
            "published_on": ann["published_on"] if ann else None,
            "url": ann["url"] if ann else None,
            "availability": window.value if window else None,
            "availability_note": availability_note(window, ann["published_on"]) if window else "",
        },
    }
    render.emit(payload)
    if not render.is_json():
        render.note(f"编号：{payload['code_key']}")
        render.note(f"sha256：{digest}")
        render.note(f"获取：{payload['fetched_at']}")
        render.note(f"公告：{payload['announcement']['url']}")
        render.note(f"附件：{payload['attachment_url']}")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("provenance", parents=parents, help="反查文件来源（按 sha256）")
    p.add_argument("path")
    p.set_defaults(func=run)
