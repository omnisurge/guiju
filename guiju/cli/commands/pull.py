"""pull —— 下载正文，幂等，绝不把非 PDF 写进归档。"""

from __future__ import annotations

import argparse
from pathlib import Path

from ...domain import attachment as att_domain
from ...domain import availability, naming
from ...domain.codes import classify
from ...errors import EXIT_EXISTS, AmbiguousAttachment, BadAttachment, NoAttachment
from ...files import sha256_bytes, sha256_file, write_atomic
from ...sources import registry
from ...store.sqlite import Store, default_home, persist_codes
from .. import render
from ..lookup import resolve


def default_out_dir() -> Path:
    return default_home() / "library"


def run(store: Store, args: argparse.Namespace) -> int:
    source = registry()[args.source]
    target = resolve(store, args.ref)
    ann = target["ann"]
    ann_id = int(ann["id"])

    # 先把「可得性事实」算出来：本命令的任何失败都必须把它带给调用方，
    # 这样 agent 才能区分「历史公告本就如此」与「工具/站点出问题了」。
    window = availability.state(ann["published_on"])
    avail_note = availability.note(window, ann["published_on"])

    html, text = source.detail(ann["url"])
    codes = classify(text, ann["doc_name"] or "")
    persist_codes(store, ann_id, codes, ann["doc_name"] or "")
    store.commit()

    code_key = target.get("code_key") or codes.primary_key
    code_display = target.get("code_display") or codes.primary_display
    confidence = target.get("confidence") or codes.confidence.value
    status = target.get("status") or ""

    # 顺带（不额外发请求）提示：拿到的是不是已被替代的版本
    if status in ("superseded", "withdrawn"):
        render.warn(f"该版本状态为 {status}；现行版用 guiju which {code_key} 查")

    if not code_key:
        # 不是阻塞项：schema 允许「无编号但有正文」。以公告名称归档，仅提示引用不可用。
        render.warn("公告正文未解析出编号：按公告名称归档，cite 将不可用")

    out_dir = Path(args.out).expanduser() if args.out else default_out_dir()

    existing = store.artifact_by_code(code_key) if code_key else None
    if existing and not args.force:
        path = Path(existing["local_path"])
        if path.exists() and sha256_file(path) == existing["sha256"]:
            payload = {
                "ok": True,
                "already": True,
                "code": code_display,
                "path": str(path),
                "sha256": existing["sha256"],
                "bytes": existing["bytes"],
            }
            render.emit(payload)
            if not render.is_json():
                render.note(f"= 已存在且校验通过：{path}")
            return EXIT_EXISTS

    atts = source.attachments(html)
    picked = att_domain.pick(atts, ann["doc_name"] or "", announcement_title=ann["title"])

    if not picked.ok:
        if picked.verdict is att_domain.Verdict.AMBIGUOUS:
            raise AmbiguousAttachment(
                "公告页有多个附件，且名称无法锁定唯一正文", hint=avail_note, **picked.as_payload()
            )
        if picked.verdict is att_domain.Verdict.NOT_APPLICABLE:
            raise NoAttachment(
                "该公告不是官方中文正文（英文版/译本），不做下载",
                hint=avail_note,
                availability=window.value,
                availability_note=avail_note,
                **picked.as_payload(),
            )
        raise NoAttachment(
            "公告页没有可用正文附件",
            hint=avail_note,
            availability=window.value,
            availability_note=avail_note,
            **picked.as_payload(),
        )

    choice = picked.choice
    assert choice is not None
    render.note(f"  附件 {len(atts)} 个，选定：{choice.label[:50] or choice.url[-40:]}")

    try:
        body, magic = source.download(choice.url, referer=ann["url"])
    except BadAttachment as exc:
        store.log_probe(source.id, choice.url, None, None, "bad_attachment", choice.label)
        store.commit()
        exc.extra.setdefault("attachments", picked.as_payload().get("candidates", []))
        raise

    digest = sha256_bytes(body)
    dest = out_dir / (
        naming.readable_name(code_key, ann["doc_name"] or "")
        if args.readable
        else naming.library_name(code_key, ann["doc_name"] or "", digest)
    )
    write_atomic(dest, body)
    store.record_artifact(
        ann_id=ann_id,
        code_key=code_key,
        source=source.id,
        page_url=ann["url"],
        file_url=choice.url,
        label=choice.label,
        sha256=digest,
        size=len(body),
        magic=magic,
        local_path=str(dest),
    )
    store.log_probe(source.id, choice.url, 200, len(body), "ok", choice.label)
    store.commit()

    payload = {
        "ok": True,
        "code": code_display,
        "name": ann["doc_name"],
        "path": str(dest),
        "sha256": digest,
        "bytes": len(body),
        "magic": magic,
        "confidence": confidence,
        "status": status,
        "source_page": ann["url"],
        "attachment": choice.url,
        "published_on": ann["published_on"],
        "availability": window.value,
    }
    render.emit(payload)
    if not render.is_json():
        label = code_display or ann["doc_name"] or ann["url"]
        render.note(f"✓ {label} → {dest}（{len(body) / 1048576:.1f} MB）")
        render.note(f"  sha256 {digest[:16]}…")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("pull", parents=parents, help="下载规范正文（幂等）")
    p.add_argument("ref", help="编号 / 公告ID / 公告URL")
    p.add_argument("--out", default=None, help="归档目录（默认 ~/.guiju/library）")
    p.add_argument("--readable", action="store_true", help="用人类可读文件名（默认内容寻址）")
    p.add_argument("--force", action="store_true", help="已存在也重新下载")
    p.add_argument("--source", default="mohurd")
    p.set_defaults(func=run)
