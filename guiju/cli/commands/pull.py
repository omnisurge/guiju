"""pull —— 下载正文（单个或批量），幂等，绝不把非 PDF 写进归档。

批量形态是为真实工作流准备的：监理部一次要取一整份规范台账。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from ...domain import attachment as att_domain
from ...domain import availability, naming
from ...domain.codes import classify
from ...errors import (
    EXIT_EXISTS,
    EXIT_OK,
    AmbiguousAttachment,
    BadAttachment,
    GuijuError,
    NoAttachment,
)
from ...files import sha256_bytes, sha256_file, write_atomic
from ...sources import registry
from ...store.sqlite import Store, default_home, persist_codes
from .. import render
from ..lookup import resolve

#: 这些退出码表示"需要人或环境干预"，批量时视为未达成
_SEVERE = (1, 20, 40, 50)


def default_out_dir() -> Path:
    return default_home() / "library"


def _pull_one(store: Store, args: argparse.Namespace, ref: str, source, out_dir: Path) -> dict:
    target = resolve(store, ref)
    ann = target["ann"]
    ann_id = int(ann["id"])

    # 可得性事实先算出来：本命令任何失败都要带上它，agent 才能区分"历史如此"与"出问题了"
    window = availability.state(ann["published_on"])
    avail_note = availability.note(window, ann["published_on"])

    from ...domain.codes import normalize_key

    code_key = normalize_key(target["code_key"]) if target.get("code_key") else None
    code_display = target.get("code_display")
    confidence = target.get("confidence") or "unknown"
    status = target.get("status") or ""

    # 幂等快路径：已知编号且归档完好时，连详情页都不必抓（批量复跑的关键）
    if code_key and not args.force:
        existing = store.artifact_by_code(code_key)
        if existing:
            path = Path(existing["local_path"])
            if path.exists() and sha256_file(path) == existing["sha256"]:
                return {
                    "ok": True, "already": True, "code": code_display or code_key,
                    "path": str(path), "sha256": existing["sha256"], "bytes": existing["bytes"],
                    "exit": EXIT_EXISTS,
                }

    html, text = source.detail(ann["url"])
    codes = classify(text, ann["doc_name"] or "")
    persist_codes(store, ann_id, codes, ann["doc_name"] or "")
    store.commit()

    code_key = code_key or (normalize_key(codes.primary_key) if codes.primary_key else None)
    code_display = code_display or codes.primary_display
    confidence = confidence if target.get("confidence") else codes.confidence.value

    if not code_key:
        render.warn("公告正文未解析出编号：按公告名称归档，cite 将不可用")

    if status in ("superseded", "withdrawn"):
        render.warn(f"该版本状态为 {status}；现行版用 guiju which {code_key} 查")

    existing = store.artifact_by_code(code_key) if code_key else None
    if existing and not args.force:
        path = Path(existing["local_path"])
        if path.exists() and sha256_file(path) == existing["sha256"]:
            return {
                "ok": True, "already": True, "code": code_display, "path": str(path),
                "sha256": existing["sha256"], "bytes": existing["bytes"], "exit": EXIT_EXISTS,
            }

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
                hint=avail_note, availability=window.value, availability_note=avail_note,
                **picked.as_payload(),
            )
        raise NoAttachment(
            "公告页没有可用正文附件",
            hint=avail_note, availability=window.value, availability_note=avail_note,
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
        ann_id=ann_id, code_key=code_key, source=source.id, page_url=ann["url"],
        file_url=choice.url, label=choice.label, sha256=digest, size=len(body),
        magic=magic, local_path=str(dest),
    )
    store.log_probe(source.id, choice.url, 200, len(body), "ok", choice.label)
    store.commit()

    return {
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
        "exit": EXIT_OK,
    }


def _read_refs(spec: str) -> list[str]:
    raw = (
        sys.stdin.read()
        if spec == "-"
        else Path(spec).expanduser().read_text(encoding="utf-8")
    )
    refs = []
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()   # 允许行尾注释，清单可直接带标准名
        if not line:
            continue
        refs.append(line)
    return refs


def _batch(store: Store, args: argparse.Namespace, source) -> int:
    refs = _read_refs(args.list)
    if not refs:
        raise GuijuError("清单为空：每行一个编号/名称/公告ID，以 # 开头的行为注释")

    out_dir = Path(args.out).expanduser() if args.out else default_out_dir()
    results: list[dict] = []
    for index, ref in enumerate(refs, 1):
        render.note(f"[{index}/{len(refs)}] {ref}")
        try:
            payload = _pull_one(store, args, ref, source, out_dir)
            exit_code = int(payload.get("exit", 0))
            results.append(
                {
                    "ref": ref,
                    "exit": exit_code,
                    "outcome": "already" if payload.get("already") else "ok",
                    "code": payload.get("code"),
                    "path": payload.get("path"),
                    "bytes": payload.get("bytes"),
                }
            )
            render.note(f"  → {'已存在' if exit_code == EXIT_EXISTS else '完成'} {payload.get('code') or ''}")
        except GuijuError as exc:
            results.append(
                {"ref": ref, "exit": exc.exit, "outcome": exc.reason,
                 "error": exc.message, "hint": exc.hint}
            )
            render.note(f"  → 未达成 exit={exc.exit} {exc.reason}：{exc.message[:60]}")

    counts = Counter(r["exit"] for r in results)
    severe = [r for r in results if r["exit"] in _SEVERE]
    payload = {
        "ok": not severe,
        "total": len(refs),
        "done": len(results),
        "counts": {str(k): v for k, v in sorted(counts.items())},
        "bytes_total": sum(r.get("bytes") or 0 for r in results),
        "out_dir": str(out_dir),
        "results": results,
    }
    render.emit(payload)
    if not render.is_json():
        render.note(
            f"✓ 完成 {counts.get(0, 0)} / 已存在 {counts.get(EXIT_EXISTS, 0)} / "
            f"未达成 {len(results) - counts.get(0, 0) - counts.get(EXIT_EXISTS, 0)}，"
            f"共 {payload['bytes_total'] / 1048576:.1f} MB → {out_dir}"
        )
        for r in results:
            if r["exit"] not in (0, EXIT_EXISTS):
                render.warn(f"  {r['ref']} → exit={r['exit']} {r['outcome']}")
    return severe[0]["exit"] if severe else EXIT_OK


def run(store: Store, args: argparse.Namespace) -> int:
    source = registry()[args.source]
    if args.list:
        return _batch(store, args, source)

    if not args.ref:
        raise GuijuError("缺少引用：给一个编号/名称/公告ID/URL，或用 --list 批量")

    out_dir = Path(args.out).expanduser() if args.out else default_out_dir()
    payload = _pull_one(store, args, args.ref, source, out_dir)
    render.emit(payload)
    if not render.is_json():
        if payload.get("already"):
            render.note(f"= 已存在且校验通过：{payload['path']}")
        else:
            label = payload.get("code") or payload.get("name") or ""
            render.note(f"✓ {label} → {payload['path']}（{payload['bytes'] / 1048576:.1f} MB）")
            render.note(f"  sha256 {payload['sha256'][:16]}…")
    return int(payload.get("exit", 0))


def add_parser(sub, parents) -> None:
    p = sub.add_parser("pull", parents=parents, help="下载规范正文（单条或批量，幂等）")
    p.add_argument("ref", nargs="?", default=None, help="编号 / 规范名 / 公告ID / 公告URL")
    p.add_argument("--list", default=None, help="批量清单文件（每行一个 ref，- 表示 stdin）")
    p.add_argument("--out", default=None, help="归档目录（默认 ~/.guiju/library）")
    p.add_argument("--readable", action="store_true", help="用人类可读文件名（默认内容寻址）")
    p.add_argument("--force", action="store_true", help="已存在也重新下载")
    p.add_argument("--source", default="mohurd")
    p.set_defaults(func=run)
