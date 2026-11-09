"""show —— 一条规范的完整事实：名称 / 编号 / 角色 / 可得性 / 本地文件。"""

from __future__ import annotations

import argparse

from ...domain.availability import note as availability_note
from ...domain.availability import state
from ...files import default_out_dir, find_cached
from ...store.sqlite import Store
from .. import render
from ..lookup import resolve


def run(store: Store, args: argparse.Namespace) -> int:
    target = resolve(store, args.ref)
    ann = target["ann"]
    ann_id = int(ann["id"])
    mentions = store.mentions_for(ann_id)
    code_key = target.get("code_key")

    # 本地文件只看下载目录（默认 ~/.guiju/library）：pull 的命名是可复现的，
    # 按编号/公告名 glob 回来即可；带指纹的文件还会顺手校验内容没被改过。
    keys = [code_key, *(m["code_key"] for m in mentions), None]
    local = None
    for key in dict.fromkeys(keys):
        for readable in (False, True):
            found = find_cached(default_out_dir(), key, ann["doc_name"] or "", readable=readable)
            if found:
                path, digest = found
                local = {
                    "path": str(path),
                    "sha256": digest,
                    "bytes": path.stat().st_size,
                    "exists": True,
                }
                break
        if local:
            break

    window = state(ann["published_on"])
    payload = {
        "ok": True,
        "id": ann_id,
        "published_on": ann["published_on"],
        "name": ann["doc_name"],
        "category": ann["category"],
        "title": ann["title"],
        "url": ann["url"],
        "codes": [
            {
                "code": m["code_display"],
                "key": m["code_key"],
                "role": m["role"],
                "status": m["status"],
                "confidence": m["confidence"],
                "evidence": m["evidence"],
            }
            for m in mentions
        ],
        "hint": ""
        if mentions
        else "该公告尚未解析编号：guiju which <编号> 可直接查，或 guiju pull 时自动解析",
        "availability": window.value,
        "availability_note": availability_note(window, ann["published_on"]),
        "local": local,
    }
    render.emit(payload)
    if not render.is_json():
        render.note(f"名称：{payload['name']}")
        render.note(f"公告：{payload['published_on']}　分类：{payload['category']}")
        if payload["codes"]:
            for c in payload["codes"]:
                render.note(f"编号：{c['code']}　角色：{c['role']}　置信：{c['confidence']}")
        else:
            render.note("编号：（尚未解析 → 跑一次 guiju pull <公告ID> 会顺带解析并落库）")
        render.note(f"正文：{local['path'] if local else '未下载'}")
        render.note(f"可得性：{payload['availability']}")
        render.note(f"  {payload['availability_note']}")
        render.note(f"链接：{payload['url']}")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("show", parents=parents, help="查看某条规范的详情")
    p.add_argument("ref", help="编号 / 公告ID / 公告URL")
    p.set_defaults(func=run)
