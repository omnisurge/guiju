"""pull —— 下载正文（单个或批量），幂等，绝不把非 PDF 落盘。

批量形态是为真实工作流准备的：监理部一次要取一整份规范台账。
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .. import report as render
from ..domain import announcement, availability, naming
from ..domain import attachment as att_domain
from ..domain.codes import Codes, Confidence, Role, classify
from ..errors import (
    EXIT_COMPLIANCE,
    EXIT_EXISTS,
    EXIT_NEEDS_HUMAN,
    EXIT_OK,
    EXIT_UNREACHABLE,
    AmbiguousAttachment,
    AmbiguousCode,
    BadAttachment,
    BadUsage,
    GuijuError,
    NoAttachment,
)
from ..files import default_out_dir, find_cached, sha256_bytes, sha256_file
from ..ports.store import StorePort
from .resolve import resolve

#: 这些退出码表示"需要人或环境干预"，批量时视为未达成
_SEVERE = (1, 20, 40, 50)


def _out_dir(args: Any) -> Path:
    """下载目录：AppContext 优先，旧直接调用保留默认目录兼容。"""
    ctx = getattr(args, "app_context", None)
    if ctx is not None and getattr(ctx, "out_dir", None) is not None:
        return Path(ctx.out_dir)
    out = getattr(args, "out", None)
    return Path(out).expanduser() if out else default_out_dir()


def _finalize_body(part_path, dest_path) -> None:
    """正文落盘唯一入口；入口可替换，旧直接调用保留局部兼容。"""
    from ..net.body import finalize

    finalize(part_path, dest_path)


def _probe_sink(store, source_id, *, url: str | None = None):
    """把 BodyClient 的 ProbeRecord 接到 Store.log_probe；观测失败不阻断下载。"""
    log = getattr(store, "log_probe", None)

    def sink(record):
        if record is None or log is None:
            return
        if getattr(record, "outcome", None) == "ok":
            sink.ok_emitted = True
        with contextlib.suppress(Exception):
            log(source_id, url or record.url, record.status, record.bytes, record.outcome)

    sink.ok_emitted = False
    return sink

#: 批量失败体必须原样交给人/调用方的字段（W4-1）：缺一个都可能让交接断链。
_BATCH_HANDOFF_FIELDS = (
    "candidates",
    "availability",
    "availability_note",
    "attachments",
    "page_url",
    "verdict",
    "names",
    "pick_reason",
)


def _availability_of(published_on: str | None) -> tuple[availability.Window, str]:
    """公告日期 → (可得性窗口, 给 agent 的一句事实说明)。"""
    window = availability.state(published_on)
    return window, availability.note(window, published_on)


def _attach_availability(exc: GuijuError, published_on: str | None = None) -> GuijuError:
    """给任何 pull 失败体补 availability/availability_note；已有值不覆盖。"""
    window, note = _availability_of(published_on)
    exc.extra.setdefault("availability", window.value)
    exc.extra.setdefault("availability_note", note)
    return exc


def _batch_failure(exc: GuijuError, ref: str) -> dict:
    """W4-1：批量结果里的失败项必须保留人工交接字段，且不复制成功字段。"""
    payload = exc.payload()
    result = {
        "ref": ref,
        "exit": exc.exit,
        "outcome": exc.reason,
        "error": exc.message,
        "hint": exc.hint,
    }
    for key in _BATCH_HANDOFF_FIELDS:
        if key in payload:
            result[key] = payload[key]
    result.setdefault("availability", availability.Window.UNKNOWN.value)
    result.setdefault(
        "availability_note", availability.note(availability.Window.UNKNOWN)
    )
    return result


def _codes_for_book(
    codes: Codes,
    book: str,
    window: availability.Window | None = None,
    published_on: str | None = None,
) -> Codes:
    """多名称公告：只保留正文证据里出现这本书的编号。

    不能把整篇正文解析出的第一个编号当成用户点的那本书 —— 实测标题两本、
    正文只写甲编号时，按乙 pull 会把甲的编号写进乙的文件名和 cite。
    找不到唯一决定性证据就交 32，让调用方按编号/人工确认。
    """
    relevant = tuple(
        m for m in codes.mentions if announcement.contains_name(m.evidence, book)
    )

    def _candidates() -> list[dict]:
        return [
            {"code": m.code_display or m.code_key, "role": m.role.value,
             "evidence": m.evidence[:160]}
            for m in (relevant or codes.mentions)
        ]

    def _ambiguous(message: str) -> AmbiguousCode:
        extra: dict = {}
        if window is not None:
            extra["availability"] = window.value
            extra["availability_note"] = availability.note(window, published_on)
        return AmbiguousCode(
            message,
            hint="按编号 pull，或人工确认这本书的编号后再试",
            candidates=_candidates(),
            **extra,
        )

    if not relevant:
        raise _ambiguous(f"公告标题含多本规范，正文没有与「{book}」对应的编号证据")
    decisive = tuple(
        m for m in relevant
        if m.role in (Role.PRIMARY, Role.TARGET_OF_REVISION)
    )
    keys = {m.code_key for m in decisive}
    if len(keys) != 1:
        raise _ambiguous(f"正文中「{book}」对应多个编号或没有决定性编号证据")
    chosen = decisive[0]
    return Codes(
        mentions=relevant,
        primary_key=chosen.code_key,
        primary_display=chosen.code_display,
        confidence=Confidence.PARSED,
    )


def _pull_api(store: StorePort, args: Any, ref: str, source, out_dir: Path) -> dict:
    """没有公告页、只有 JSON API 的源（目前是上海地标）。

    流程：检索 → 判歧义 → 断点续传下载 → 按指纹命名落盘 → 补一条公告记录（供 cite/which 复用）。
    """
    from ..domain.codes import Mention, normalize_key
    from ..errors import AmbiguousCode, NotIndexed

    try:
        hits = source.find(ref)
    except GuijuError as exc:
        # W4-2：检索阶段没有单一 rec，不得拿候选日期冒充可得性。
        raise _attach_availability(exc) from None
    if not hits:
        window, note = _availability_of(None)
        raise NotIndexed(
            f"{source.display_name}没有匹配「{ref}」的地方标准",
            hint="换编号或名称片段再试；该源只覆盖地方标准（DG/TJ、DB31/T 等）",
            availability=window.value,
            availability_note=note,
        )
    if len(hits) > 1:
        window, note = _availability_of(None)
        raise AmbiguousCode(
            f"「{ref}」在{source.display_name}命中 {len(hits)} 条，需要人指定",
            candidates=[
                {"code": h["code"], "name": h["name"], "published_on": h["published_on"]}
                for h in hits[:10]
            ],
            availability=window.value,
            availability_note=note,
        )

    rec = hits[0]
    window, avail_note = _availability_of(rec["published_on"])
    if rec.get("status") == "withdrawn":
        render.warn(
            f"该版本在{source.display_name}已废止（{rec.get('abolish_on') or '时间未载明'}）；"
            "正文仍下载，但引用前请确认现行版"
        )
    code_key = normalize_key(rec["code"]) if rec["code"] else None
    if not rec.get("download_url"):
        raise BadAttachment(
            f"{rec['code'] or rec['name']} 在该源没有可下载的正文文件",
            availability=window.value,
            availability_note=avail_note,
        )

    def _remember() -> None:
        """API 记录落库：announcement + mention（W4-4，cite/which 复用）。

        已缓存 exit 10 路径也要走这里：1.2.0a3 只落了文件没落库，
        不补的话升级后 cite --offline 仍然查不到这条地标。
        """
        ann_id, _fresh = store.upsert_announcement(
            rec["page_url"],
            rec["published_on"],
            rec["name"],
            rec["name"],
            "local_standard",
        )
        if code_key:
            codes = Codes(
                mentions=(
                    Mention(
                        code_key,
                        rec["code"],
                        Role.PRIMARY,
                        f"{source.display_name}检索结果：{rec['code']}",
                    ),
                ),
                primary_key=code_key,
                primary_display=rec["code"],
                confidence=Confidence.PARSED,
            )
            persist_codes(store, ann_id, codes, rec["name"], status=rec.get("status"))
        store.commit()

    if not args.force:
        cached = find_cached(out_dir, code_key, rec["name"], readable=args.readable)
        if cached:
            path, digest = cached
            _remember()
            return {
                "ok": True,
                "already": True,
                "source": source.id,
                "code": rec["code"],
                "name": rec["name"],
                "path": str(path),
                "sha256": digest,
                "bytes": path.stat().st_size,
                "magic": "pdf",
                "status": rec.get("status", ""),
                "published_on": rec["published_on"],
                "implement_on": rec.get("implement_on"),
                "availability": window.value,
                "source_page": rec["page_url"],
                "attachment": rec["download_url"],
                "confidence": "parsed",
                "exit": EXIT_EXISTS,
            }

    partial_dir = out_dir / ".partial"
    key = sha256_bytes(f"{source.id}|{rec['code'] or rec['name']}|{rec['download_url']}".encode())[:10]
    part = partial_dir / f"{key}.part"
    sink = _probe_sink(store, source.id, url=rec["download_url"])
    try:
        nbytes, magic, resumed = source.download_to(
            rec["download_url"], str(part), probe_sink=sink
        )
    except GuijuError as exc:
        raise _attach_availability(exc, rec["published_on"]) from None
    if resumed:
        render.note(f"  续传：从 {resumed} 字节处继续（共 {nbytes} 字节）")

    digest = sha256_file(part)
    dest = out_dir / (
        naming.readable_name(code_key, rec["name"])
        if args.readable
        else naming.library_name(code_key, rec["name"], digest)
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    _finalize_body(part, dest)

    _remember()
    if not sink.ok_emitted:
        store.log_probe(source.id, rec["download_url"], 200, nbytes, "ok", rec["name"])
    store.commit()

    return {
        "ok": True,
        "source": source.id,
        "code": rec["code"],
        "name": rec["name"],
        "path": str(dest),
        "sha256": digest,
        "bytes": nbytes,
        "magic": magic,
        "confidence": "parsed",
        "status": rec.get("status", ""),
        "published_on": rec["published_on"],
        "implement_on": rec.get("implement_on"),
        "availability": window.value,
        "source_page": rec["page_url"],
        "attachment": rec["download_url"],
        "exit": EXIT_OK,
    }


def persist_codes(store: StorePort, ann_id: int, codes: Codes, doc_name: str, status: str | None = None) -> None:
    """兼容旧测试/旧调用名的端口函数；实现落在 StorePort 上。"""
    store.persist_codes(ann_id, codes, doc_name, status=status)


def _cached_currency(args: Any, store: StorePort):
    """取本次命令的时效端口；入口注入优先，旧直接调用才构造兼容缓存。"""
    ctx = getattr(args, "app_context", None)
    if ctx is not None and getattr(ctx, "currency", None) is not None:
        return ctx.currency
    from .. import currency as currency_mod

    return currency_mod.CachedCurrency(store=store)


def _pull_one(store: StorePort, args: Any, ref: str, source, out_dir: Path) -> dict:
    if getattr(source, "api_mode", False):
        return _pull_api(store, args, ref, source, out_dir)

    # 显式给带落库缓存的时效端口：否则 resolve 内部会建一个无 store 的
    # CachedCurrency，TTL 缓存够不到，pull 每次都直打时效服务（M9）。
    try:
        resolve_kwargs = {"currency": _cached_currency(args, store)}
        sources = getattr(getattr(args, "app_context", None), "sources", None)
        if sources is not None:
            resolve_kwargs["sources"] = sources
        target = resolve(store, ref, **resolve_kwargs)
    except GuijuError as exc:
        # W4-2：resolve 早期失败（目录/URL 等）还没有公告日期，按 unknown 交人。
        raise _attach_availability(exc) from None
    if target.get("versions"):
        picked = next(
            (v for v in target["versions"] if v["ann_id"] == int(target["ann"]["id"])), None
        )
        others = [v for v in target["versions"] if v["ann_id"] != int(target["ann"]["id"])]
        if picked:
            render.note(
                f"同名 {len(target['versions'])} 版，按时效选中 {picked['code']}"
                f"（{picked['published_on']}）；历史版本 {len(others)} 个"
            )
    ann = target["ann"]
    ann_id = int(ann["id"])

    # 多名称公告（P1-3）：doc_name 只是标题里第一个名称，不能代表整条公告。
    # 按名称 pull 时用用户要的那一本；按编号/ID/URL pull 时没有这个信息，
    # 标题多本就只能 33 交人（见 pick），连幂等快路径都不能用 ——
    # 否则会把"先落库那本"的旧文件当成"已有"返回 10。
    names = announcement.names_of(ann["title"], ann["doc_name"])
    display_name = ann["doc_name"] or ""
    ref_name = ""
    book_known = len(names) <= 1
    if target.get("via") in ("name", "name_current_version"):
        chosen, _matched = announcement.match_name(names, ref)
        if chosen:
            display_name = chosen
            ref_name = chosen
            book_known = True
        else:
            ref_name = ref

    # 可得性事实先算出来：本命令任何失败都要带上它，agent 才能区分"历史如此"与"出问题了"
    window, avail_note = _availability_of(ann["published_on"])

    from ..domain.codes import normalize_key

    code_key = normalize_key(target["code_key"]) if target.get("code_key") else None
    code_display = target.get("code_display")
    confidence = target.get("confidence") or "unknown"
    status = target.get("status") or ""

    # 幂等快路径：下载目录已有同名文件且指纹一致时，连详情页都不必抓（批量复跑的关键）
    if not args.force and book_known and (len(names) <= 1 or code_key):
        cached = find_cached(out_dir, code_key, display_name, readable=args.readable, ann_id=ann_id)
        if cached:
            path, digest = cached
            return {
                "ok": True, "already": True, "code": code_display or code_key,
                "name": display_name, "status": status,
                "path": str(path), "sha256": digest, "bytes": path.stat().st_size,
                "magic": "pdf", "confidence": confidence, "attachment": None,
                "published_on": ann["published_on"], "availability": window.value,
                "source_page": ann["url"], "exit": EXIT_EXISTS,
            }

    try:
        html, text = source.detail(ann["url"])
    except GuijuError as exc:
        # W4-2：detail 解析/抓取失败（含 BadAttachment）也要带可得性事实。
        raise _attach_availability(exc, ann["published_on"]) from None
    # 名称证据用"用户要的那一本"：doc_name（第一本）会把弱证据指向另一本书的编号
    codes = classify(text, display_name)
    if len(names) > 1 and ref_name:
        # 多名称公告必须把"用户选的那本"贯穿到编号，不能只换文件名。
        codes = _codes_for_book(
            codes, ref_name, window=window, published_on=ann["published_on"]
        )
        selected_key = normalize_key(codes.primary_key) if codes.primary_key else None
        if code_key and selected_key and code_key != selected_key:
            raise AmbiguousCode(
                f"名称证据与编号先验不一致（{ref_name} vs {code_display or code_key}）",
                hint="按编号 pull，或人工确认这本书的编号后再试",
                candidates=[
                    {"code": codes.primary_display, "key": codes.primary_key},
                    {"code": code_display, "key": code_key},
                ],
                availability=window.value,
                availability_note=avail_note,
            )
        code_key = code_key or selected_key
        code_display = code_display or codes.primary_display
        confidence = codes.confidence.value

    code_key = code_key or (normalize_key(codes.primary_key) if codes.primary_key else None)
    code_display = code_display or codes.primary_display
    # confidence 以正文解析结果为准：target 先验值（编号直查/API 源）只在
    # 解析没定版时保留——库存按 parsed 记而 payload 报 unknown 的口径分叉即出此。
    if codes.confidence is Confidence.PARSED:
        confidence = codes.confidence.value

    if not code_key:
        render.warn("公告正文未解析出编号：按公告名称命名文件，cite 将不可用")

    if status in ("superseded", "withdrawn"):
        render.warn(f"该版本状态为 {status}；现行版用 guiju which {code_key} 查")

    if not args.force and book_known:
        cached = find_cached(out_dir, code_key, display_name, readable=args.readable, ann_id=ann_id)
        if cached:
            path, digest = cached
            return {
                "ok": True, "already": True, "code": code_display,
                "name": display_name, "status": status,
                "path": str(path), "sha256": digest, "bytes": path.stat().st_size,
                "magic": "pdf", "confidence": confidence, "attachment": None,
                "published_on": ann["published_on"], "availability": window.value,
                "source_page": ann["url"], "exit": EXIT_EXISTS,
            }

    try:
        atts = source.attachments(html)
    except GuijuError as exc:
        # W4-2：附件枚举失败同样带可得性；具体 30/50 由异常类型决定。
        raise _attach_availability(exc, ann["published_on"]) from None
    picked = att_domain.pick(
        atts, names, ref_name=ref_name, announcement_title=ann["title"]
    )
    if not picked.ok:
        if picked.verdict is att_domain.Verdict.AMBIGUOUS:
            message = "公告页有多个附件，且名称无法锁定唯一正文"
            if len(names) > 1:
                message = (
                    f"公告标题含 {len(names)} 本规范，按编号/ID 无法唯一映射到一本，需要人指定"
                    if not ref_name
                    else f"公告标题含 {len(names)} 本规范，名称对不上其中任何一本，需要人指定"
                )
            raise AmbiguousAttachment(
                message,
                hint=avail_note,
                availability=window.value,
                availability_note=avail_note,
                **picked.as_payload(),
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
    # 33/30 在 pick 阶段已抛出：只有附件确实选定后才写 mentions，
    # 否则一次歧义失败会先删掉旧 mentions，污染已有数据。
    persist_codes(store, ann_id, codes, display_name, status=status or None)
    store.commit()
    render.note(f"  附件 {len(atts)} 个，选定：{choice.label[:50] or choice.url[-40:]}")

    # 断点续传：`.part` 以「公告 + 附件URL」为键，中途失败重跑会接着下
    partial_dir = out_dir / ".partial"
    key = sha256_bytes(f"{ann_id}|{choice.url}".encode())[:10]
    part = partial_dir / f"{key}.part"
    sink = _probe_sink(store, source.id)
    try:
        nbytes, magic, resumed = source.download_to(
            choice.url, str(part), referer=ann["url"], probe_sink=sink
        )
    except GuijuError as exc:
        if isinstance(exc, BadAttachment):
            store.log_probe(source.id, choice.url, None, None, "bad_attachment", choice.label)
            store.commit()
            exc.extra.setdefault("attachments", picked.as_payload().get("candidates", []))
        raise _attach_availability(exc, ann["published_on"]) from None
    if resumed:
        render.note(f"  续传：从 {resumed} 字节处继续（共 {nbytes} 字节）")

    digest = sha256_file(part)
    dest = out_dir / (
        naming.readable_name(code_key, display_name, ann_id)
        if args.readable
        else naming.library_name(code_key, display_name, digest, ann_id)
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    _finalize_body(part, dest)
    if not sink.ok_emitted:
        store.log_probe(source.id, choice.url, 200, nbytes, "ok", choice.label)
    store.commit()

    return {
        "ok": True,
        "code": code_display,
        "name": display_name,
        "path": str(dest),
        "sha256": digest,
        "bytes": nbytes,
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
        else Path(spec).expanduser().read_text(encoding="utf-8-sig")
    )
    refs = []
    for line in raw.splitlines():
        line = line.strip().removeprefix("\ufeff").strip()
        if not line or line.startswith("#"):
            continue
        # 行尾注释须以空白与引用分隔（"GB 50037 # 国标"），URL 里的 #frag 不截断
        hash_at = line.find(" #")
        if hash_at > 0:
            line = line[:hash_at].rstrip()
        refs.append(line)
    return refs


def _batch(store: StorePort, args: Any, source) -> int:
    refs = _read_refs(args.list)
    if not refs:
        raise BadUsage("清单为空：每行一个编号/名称/公告ID，以 # 开头的行为注释")

    out_dir = _out_dir(args)
    results: list[dict] = []
    stopped: str | None = None
    stop_exit: int | None = None
    for index, ref in enumerate(refs, 1):
        render.note(f"[{index}/{len(refs)}] {ref}")
        try:
            payload = _pull_one(store, args, ref, source, out_dir)
            exit_code = int(payload.get("exit", 0))
            outcome = "already" if payload.get("already") else "ok"
            if exit_code == EXIT_COMPLIANCE:
                outcome = "compliance"
            results.append(
                {
                    "ref": ref,
                    "exit": exit_code,
                    "outcome": outcome,
                    "code": payload.get("code"),
                    "name": payload.get("name"),
                    "status": payload.get("status"),
                    "availability": payload.get("availability"),
                    "source_page": payload.get("source_page"),
                    "path": payload.get("path"),
                    "bytes": payload.get("bytes"),
                }
            )
            if exit_code == EXIT_COMPLIANCE:
                render.note(f"  → 停止（合规红线）{payload.get('code') or ''}")
                stopped = "compliance"
                stop_exit = EXIT_COMPLIANCE
                break
            render.note(f"  → {'已存在' if exit_code == EXIT_EXISTS else '完成'} {payload.get('code') or ''}")
        except GuijuError as exc:
            results.append(_batch_failure(exc, ref))
            render.note(f"  → 未达成 exit={exc.exit} {exc.reason}：{exc.message[:60]}")
            if exc.exit == EXIT_COMPLIANCE:
                stopped = "compliance"
                stop_exit = EXIT_COMPLIANCE
                break
        except sqlite3.DatabaseError as exc:
            # W4-3：数据库锁/只读/损坏是环境/需人工问题，不能兜成"程序缺陷 1"。
            text = str(exc).lower()
            if "locked" in text or "busy" in text:
                exit_code = EXIT_UNREACHABLE
                outcome = "unreachable"
                hint = (
                    "数据库正被其他进程占用：稍后重试一次；若持续出现，"
                    "检查是否有另一个 guiju 进程在长期写库"
                )
            else:
                exit_code = EXIT_NEEDS_HUMAN
                outcome = "needs_human"
                hint = (
                    "数据库操作失败：备份数据库并检查磁盘/目录权限；"
                    "只读库请移走或改到可写 GUIJU_HOME"
                )
            results.append(
                {
                    "ref": ref,
                    "exit": exit_code,
                    "outcome": outcome,
                    "error": f"{type(exc).__name__}: {exc}",
                    "hint": hint,
                    "availability": availability.Window.UNKNOWN.value,
                    "availability_note": availability.note(availability.Window.UNKNOWN),
                }
            )
            render.note(f"  → 数据库错误 {type(exc).__name__}：停止整批（exit={exit_code}）")
            stopped = "database"
            stop_exit = exit_code
            break
        except Exception as exc:  # 非 GuijuError 不得中断整批；批内兜底成 internal
            results.append(
                {"ref": ref, "exit": 1, "outcome": "internal",
                 "error": f"{type(exc).__name__}: {exc}", "hint": "程序缺陷：请带上该条 ref 上报维护者",
                 "availability": availability.Window.UNKNOWN.value,
                 "availability_note": availability.note(availability.Window.UNKNOWN)}
            )
            render.note(f"  → 内部错误 {type(exc).__name__}：{str(exc)[:60]}")

    counts = Counter(r["exit"] for r in results)
    severe = [r for r in results if r["exit"] in _SEVERE]
    payload = {
        "ok": not severe,
        "total": len(refs),
        "done": len(results),
        "counts": {str(k): v for k, v in sorted(counts.items())},
        "bytes_total": sum(r.get("bytes") or 0 for r in results),
        "out_dir": str(out_dir),
        "stopped": stopped,
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
    # 40 是红线：即使同一批里更早出现过 20/50/1，也必须让调用方看到 40，
    # 不能因为 severe[0] 是 20 就被当成"稍后重试"。
    if stopped == "compliance":
        return EXIT_COMPLIANCE
    if stopped == "database" and stop_exit is not None:
        return stop_exit
    return severe[0]["exit"] if severe else EXIT_OK


def run(store: StorePort, args: Any, sources=None) -> int:
    ctx = getattr(args, "app_context", None)
    if sources is None:
        sources = getattr(ctx, "sources", None)
    if sources is None:
        # 兼容直接调用（测试/嵌入）；CLI 入口会显式传入 registry()。
        from ..sources import registry as _registry

        sources = _registry()
    source_name = getattr(args, "source", None) or getattr(ctx, "source", None) or "mohurd"
    source = sources.get(source_name)
    if source is None:
        raise BadUsage(
            f"未知数据源：{source_name}",
            hint=f"可用数据源：{'、'.join(sources)}",
        )
    if getattr(args, "list", None):
        return _batch(store, args, source)

    ref = getattr(args, "ref", None)
    if not ref:
        raise BadUsage("缺少引用：给一个编号/名称/公告ID/URL，或用 --list 批量")

    out_dir = _out_dir(args)
    payload = _pull_one(store, args, ref, source, out_dir)
    render.emit(payload)
    if not render.is_json():
        if payload.get("already"):
            render.note(f"= 已有同名文件：{payload['path']}")
        else:
            label = payload.get("code") or payload.get("name") or ""
            render.note(f"✓ {label} → {payload['path']}（{payload['bytes'] / 1048576:.1f} MB）")
            render.note(f"  sha256 {payload['sha256'][:16]}…")
    return int(payload.get("exit", 0))
