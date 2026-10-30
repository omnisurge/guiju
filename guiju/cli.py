"""guiju 命令行入口。

面向 agent 的三条规矩：
  1. `--json` 时 stdout 只有 JSON，日志一律走 stderr
  2. 退出码语义化，agent 可据此自主决策（见 EXIT_*）
  3. 全程无交互，绝不出现 input() / 确认提示
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from . import compliance
from .db import Store, now
from .sources import mohurd

VERSION = "0.1.0"

EXIT_OK = 0
EXIT_EXISTS = 10
EXIT_UNREACHABLE = 20
EXIT_BAD_ATTACH = 30
EXIT_NOT_INDEXED = 31
EXIT_DENIED = 40
EXIT_NEEDS_HUMAN = 50

_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(code: int, message: str, as_json: bool, **extra) -> int:
    if as_json:
        emit({"ok": False, "exit": code, "error": message, **extra})
    else:
        log(f"✗ {message}")
    return code


# ---------------------------------------------------------------- resolve


def resolve(store: Store, ref: str) -> dict:
    """把用户给的引用解析成确定性目标。

    支持三种形态：公告 URL / 公告 ID / 标准编号（编号需已建立索引）。
    """
    ref = ref.strip()
    if ref.lower().startswith("http"):
        row = store.get_ann_by_url(ref) or store.get_ann_by_url(
            mohurd.migrate_legacy_url(ref) or ""
        )
        if row is None:
            return {"error": f"该链接不在本地目录中，请先运行 guiju sync：{ref}"}
        return {"ann": row, "code": None}

    if ref.isdigit():
        row = store.get_ann(int(ref))
        if row is None:
            return {"error": f"本地目录中没有 ID={ref} 的公告"}
        return {"ann": row, "code": None}

    key = mohurd.normalize_key(ref)
    rows = store.find_codes(key)
    if not rows:
        return {
            "error": (
                f"编号 {ref} 尚未建立索引。先运行：guiju index --match <关键词> "
                f"（编号写在公告正文里，需开详情页才能解析）"
            ),
            "not_indexed": True,
        }
    best = rows[0]
    ann = store.get_ann(best["ann_id"])
    return {"ann": ann, "code": best["code"], "key": best["key"]}


# ---------------------------------------------------------------- commands


def cmd_sync(store: Store, args) -> int:
    page, page_size = 1, args.page_size
    total_new = 0
    total_seen = 0
    while True:
        try:
            items, total = mohurd.fetch_catalog_page(page, page_size)
        except compliance.ComplianceError as exc:
            return die(EXIT_DENIED, str(exc), args.json)
        except compliance.Unreachable as exc:
            return die(EXIT_UNREACHABLE, str(exc), args.json, page=page)
        except mohurd.AdapterError as exc:
            return die(EXIT_NEEDS_HUMAN, str(exc), args.json, page=page)

        if not items:
            break
        fresh = sum(
            1 for it in items
            if store.upsert_announcement(it["url"], it["date"], it["title"])
        )
        store.commit()
        total_new += fresh
        total_seen += len(items)
        log(f"第 {page:>3} 页：{len(items):>3} 条，新增 {fresh:>3}（累计 {total_seen}/{total}）")

        if fresh == 0:
            log("本页无新条目，增量同步完成")
            break
        if args.pages and page >= args.pages:
            break
        if total and page * page_size >= total:
            break
        page += 1

    store.set_meta("synced_at", now())
    store.commit()
    payload = {
        "ok": True,
        "new": total_new,
        "scanned": total_seen,
        "total_in_db": store.count_announcements(),
        "synced_at": store.get_meta("synced_at"),
    }
    if args.json:
        emit(payload)
    else:
        log(f"✓ 新增 {total_new} 条，库中共 {payload['total_in_db']} 条")
    return EXIT_OK


def cmd_search(store: Store, args) -> int:
    keywords = [k for k in args.keywords if k]
    rows = store.search(keywords, args.limit)
    results = []
    for row in rows:
        codes = store.codes_for_ann(row["id"])
        results.append(
            {
                "id": row["id"],
                "date": row["date"],
                "name": row["name"],
                "kind": row["kind"],
                "title": row["title"],
                "url": row["url"],
                "codes": [c["code"] for c in codes],
                "local": bool(codes and store.get_file(codes[0]["code"])),
            }
        )
    if args.json:
        emit(results)
        return EXIT_OK if results else EXIT_OK
    if not results:
        log(f"没有匹配「{' '.join(keywords)}」的公告。若目录为空，请先运行 guiju sync")
        return EXIT_OK
    log(f"命中 {len(results)} 条：")
    for r in results:
        code = r["codes"][0] if r["codes"] else "（未索引）"
        mark = " ●" if r["local"] else ""
        log(f"  [{r['id']:>4}] {r['date']}  {code:<18} {r['name'][:34]}{mark}")
    return EXIT_OK


def cmd_index(store: Store, args) -> int:
    anns = store.anns_without_code(args.match, args.limit)
    if not anns:
        log("没有待索引的条目" + ("（匹配条件内）" if args.match else ""))
        if args.json:
            emit([])
        return EXIT_OK

    out = []
    for row in anns:
        try:
            page = mohurd.fetch_detail(row["url"])
        except compliance.ComplianceError as exc:
            return die(EXIT_DENIED, str(exc), args.json)
        except compliance.Unreachable as exc:
            log(f"  跳过 [{row['id']}] {exc}")
            continue
        codes = mohurd.extract_codes(page)
        for display in codes:
            store.add_code(mohurd.normalize_key(display), display, row["id"])
        if not codes:
            store.mark_scanned(row["id"])
        store.commit()
        out.append({"id": row["id"], "name": row["name"], "codes": codes})
        log(f"  [{row['id']:>4}] {row['name'][:30]:<30} → {', '.join(codes) or '（正文无编号）'}")

    if args.json:
        emit(out)
    log(f"✓ 索引完成，库中编号 {store.count_codes()} 个")
    return EXIT_OK


def cmd_show(store: Store, args) -> int:
    target = resolve(store, args.ref)
    if "error" in target:
        code = EXIT_NOT_INDEXED if target.get("not_indexed") else EXIT_BAD_ATTACH
        return die(code, target["error"], args.json)

    ann = target["ann"]
    codes = store.codes_for_ann(ann["id"])
    local = None
    if target.get("code"):
        row = store.get_file(target["code"])
        if row:
            local = {"path": row["path"], "sha256": row["sha256"], "exists": Path(row["path"]).exists()}

    payload = {
        "id": ann["id"],
        "date": ann["date"],
        "name": ann["name"],
        "kind": ann["kind"],
        "title": ann["title"],
        "url": ann["url"],
        "codes": [c["code"] for c in codes],
        "local": local,
    }
    if args.json:
        emit(payload)
        return EXIT_OK
    log(f"名称：{payload['name']}")
    log(f"编号：{', '.join(payload['codes']) or '（未索引，运行 guiju index）'}")
    log(f"类别：{payload['kind']}　发布：{payload['date']}")
    log(f"公告：{payload['url']}")
    log(f"本地：{local['path'] if local else '未下载'}")
    return EXIT_OK


def _target_path(out_dir: Path, code: str, name: str) -> Path:
    safe_name = _UNSAFE.sub("", name)[:60]
    safe_code = _UNSAFE.sub("", code).replace("/", "-")
    return out_dir / f"{safe_code}_{safe_name}.pdf"


def cmd_pull(store: Store, args) -> int:
    target = resolve(store, args.ref)
    if "error" in target:
        code = EXIT_NOT_INDEXED if target.get("not_indexed") else EXIT_BAD_ATTACH
        return die(code, target["error"], args.json)

    ann = target["ann"]
    wanted = target.get("code")

    try:
        page = mohurd.fetch_detail(ann["url"])
    except compliance.ComplianceError as exc:
        return die(EXIT_DENIED, str(exc), args.json)
    except compliance.Unreachable as exc:
        return die(EXIT_UNREACHABLE, str(exc), args.json, url=ann["url"])

    found = mohurd.extract_codes(page)
    for display in found:
        store.add_code(mohurd.normalize_key(display), display, ann["id"])
    store.commit()

    if wanted:
        code_display = wanted
        if found and mohurd.normalize_key(wanted) not in [mohurd.normalize_key(c) for c in found]:
            log(f"提示：页面正文编号为 {', '.join(found)}，与请求的 {wanted} 不一致，仍按请求记录")
    elif found:
        code_display = found[0]
    else:
        return die(
            EXIT_NEEDS_HUMAN,
            "公告正文未解析出标准编号，无法生成规范文件名。请人工确认后手工下载。",
            args.json,
            url=ann["url"],
        )

    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = _target_path(out_dir, code_display, ann["name"])

    existing = store.get_file(code_display)
    if existing and Path(existing["path"]).exists():
        if _sha256(Path(existing["path"])) == existing["sha256"]:
            payload = {
                "ok": True,
                "already": True,
                "code": code_display,
                "path": existing["path"],
                "sha256": existing["sha256"],
                "bytes": existing["bytes"],
            }
            if args.json:
                emit(payload)
            else:
                log(f"= 已存在且校验通过：{existing['path']}")
            return EXIT_EXISTS

    atts = mohurd.attachments(page)
    if not atts:
        return die(
            EXIT_BAD_ATTACH,
            "公告页没有附件（早期公告常见）。可改用地方住建部门转载版本。",
            args.json,
            url=ann["url"],
        )
    chosen = mohurd.pick_attachment(atts, ann["name"])
    log(f"  附件 {len(atts)} 个，选定：{chosen['label'][:50] or chosen['url'][-40:]}")

    try:
        data = mohurd.download(chosen["url"], referer=ann["url"])
    except compliance.ComplianceError as exc:
        return die(EXIT_DENIED, str(exc), args.json)
    except compliance.Unreachable as exc:
        return die(EXIT_UNREACHABLE, str(exc), args.json, url=chosen["url"])
    except mohurd.AdapterError as exc:
        return die(EXIT_BAD_ATTACH, str(exc), args.json, attachments=[a["label"] for a in atts])

    dest.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    store.record_file(code_display, str(dest), digest, len(data), ann["id"], chosen["url"])
    store.commit()

    payload = {
        "ok": True,
        "code": code_display,
        "name": ann["name"],
        "path": str(dest),
        "sha256": digest,
        "bytes": len(data),
        "source_page": ann["url"],
        "attachment": chosen["url"],
    }
    if args.json:
        emit(payload)
    else:
        log(f"✓ {code_display} → {dest}（{len(data)/1048576:.1f} MB）")
        log(f"  sha256 {digest[:16]}…")
    return EXIT_OK


def cmd_cite(store: Store, args) -> int:
    target = resolve(store, args.ref)
    if "error" in target:
        code = EXIT_NOT_INDEXED if target.get("not_indexed") else EXIT_BAD_ATTACH
        return die(code, target["error"], args.json)

    display = target.get("code") or (store.codes_for_ann(target["ann"]["id"]) or [{}])[0].get("code")
    if not display:
        return die(EXIT_NOT_INDEXED, "该公告尚未解析出标准编号", args.json)

    name = target["ann"]["name"]
    clause = args.clause.strip() if args.clause else None
    cite = f"《{name}》{display}" + (f" 第{clause}条" if clause else "")

    if args.json:
        emit(
            {
                "cite": cite,
                "name": name,
                "code": display,
                "clause": clause,
                "date": target["ann"]["date"],
                "url": target["ann"]["url"],
            }
        )
    else:
        print(cite)
    return EXIT_OK


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_verify(store: Store, args) -> int:
    records = store.all_files()
    results = []
    bad = 0
    for rec in records:
        path = Path(rec["path"])
        if not path.exists():
            status = "missing"
            bad += 1
        elif _sha256(path) != rec["sha256"]:
            status = "modified"
            bad += 1
        else:
            status = "ok"
        results.append({"code": rec["code"], "path": rec["path"], "status": status})
        if status != "ok":
            log(f"  ✗ {status}: {rec['code']} {rec['path']}")

    if args.json:
        emit({"ok": bad == 0, "total": len(results), "bad": bad, "files": results})
    else:
        log(f"{'✓' if bad == 0 else '✗'} 共 {len(results)} 个文件，异常 {bad} 个")
    return EXIT_OK if bad == 0 else EXIT_NEEDS_HUMAN


def cmd_provenance(store: Store, args) -> int:
    path = Path(args.path).expanduser().resolve()
    rec = store.get_file_by_path(str(path))
    if rec is None:
        for candidate in (Path(args.path).expanduser(), Path(args.path)):
            rec = store.get_file_by_path(str(candidate))
            if rec:
                break
    if rec is None:
        return die(EXIT_BAD_ATTACH, f"本地归档中查无此文件：{path}", args.json)

    ann = store.get_ann(rec["ann_id"]) if rec["ann_id"] else None
    payload = {
        "code": rec["code"],
        "path": rec["path"],
        "sha256": rec["sha256"],
        "bytes": rec["bytes"],
        "fetched_at": rec["fetched_at"],
        "attachment_url": rec["src_url"],
        "announcement": {
            "title": ann["title"] if ann else None,
            "date": ann["date"] if ann else None,
            "url": ann["url"] if ann else None,
        },
    }
    if args.json:
        emit(payload)
    else:
        log(f"编号：{payload['code']}")
        log(f"文件：{payload['path']}")
        log(f"sha256：{payload['sha256']}")
        log(f"获取：{payload['fetched_at']}")
        log(f"公告：{payload['announcement']['url']}")
        log(f"附件：{payload['attachment_url']}")
    return EXIT_OK


def cmd_version(store: Store, args) -> int:
    payload = {
        "name": "guiju",
        "version": VERSION,
        "catalog_synced_at": store.get_meta("synced_at", "未同步"),
        "announcements": store.count_announcements(),
        "codes": store.count_codes(),
        "db": str(store.path),
    }
    if args.json:
        emit(payload)
    else:
        print(
            f"guiju {VERSION} · catalog {payload['catalog_synced_at']} · "
            f"{payload['announcements']} 条公告 / {payload['codes']} 个编号"
        )
    return EXIT_OK


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
    common.add_argument("--db", default=None, help="指定目录库路径")

    p = argparse.ArgumentParser(
        prog="guiju",
        description="规矩 guiju —— 以规范为准绳，让依据随手可取。工程建设规范采集工具。",
    )
    p.add_argument("--version", action="store_true", help="显示版本与目录台账")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("sync", parents=[common], help="同步标准公告目录（增量）")
    s.add_argument("--pages", type=int, default=0, help="最多抓取页数，0 为不限")
    s.add_argument("--page-size", type=int, default=20, help="每页条数")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("search", parents=[common], help="检索本地目录（不联网）")
    s.add_argument("keywords", nargs="+", help="关键词，多个为「与」关系")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("index", parents=[common], help="为公告解析标准编号（建立编号索引）")
    s.add_argument("--match", default=None, help="只处理标题含该关键词的公告")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("show", parents=[common], help="查看某条规范的详情")
    s.add_argument("ref", help="编号 / 公告ID / 公告URL")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("pull", parents=[common], help="下载规范正文（幂等）")
    s.add_argument("ref", help="编号 / 公告ID / 公告URL")
    s.add_argument("--out", default="./规范", help="归档目录")
    s.set_defaults(func=cmd_pull)

    s = sub.add_parser("cite", parents=[common], help="生成标准引用串")
    s.add_argument("ref", help="编号 / 公告ID / 公告URL")
    s.add_argument("clause", nargs="?", default=None, help="条款号，如 5.2.1")
    s.set_defaults(func=cmd_cite)

    s = sub.add_parser("verify", parents=[common], help="校验本地归档完整性")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("provenance", parents=[common], help="反查文件来源")
    s.add_argument("path")
    s.set_defaults(func=cmd_provenance)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.db if getattr(args, "db", None) else None)
    try:
        if getattr(args, "version", False):
            args.json = getattr(args, "json", False)
            return cmd_version(store, args)
        if not getattr(args, "func", None):
            parser.print_help()
            return EXIT_OK
        return args.func(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
