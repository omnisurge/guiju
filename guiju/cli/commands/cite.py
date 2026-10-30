"""cite —— 生成标准引用串（本产品在监理场景的核心价值点）。"""

from __future__ import annotations

import argparse

from ...domain.cite import build_citation, normalize_clause
from ...errors import AmbiguousCode, BadUsage, NotIndexed, SourceUnreachable
from ...store.sqlite import Store
from .. import render
from ..lookup import resolve


def run(store: Store, args: argparse.Namespace) -> int:
    # 先校验输入，再解析目标：否则非法条款号会被"解析失败"抢先，错误信息误导
    clause = normalize_clause(args.clause)
    if args.clause and clause is None:
        raise BadUsage(
            f"条款号无法识别：{args.clause}",
            hint="接受 5.2.1 / 第5.2.1条 / 5.2.1条 三种写法",
        )

    from ... import currency as currency_mod

    # --offline 的语义是"完全不碰网络"：连时效端口都不用，而不是只跳过详情页核实
    target = resolve(
        store,
        args.ref,
        currency=currency_mod.NullCurrency() if args.offline else None,
        allow_network=not args.offline,
    )
    ann = target["ann"]
    code_key = target.get("code_key")
    code_display = target.get("code_display")

    if not code_key:
        mentions = store.mentions_for(int(ann["id"]))
        if not mentions and not args.offline:
            # 没有本地编号记录就现场解析一次（1 个请求），这样 cite 也能按名称/公告ID直接工作
            from ...domain.codes import classify
            from ...sources import registry
            from ...store.sqlite import persist_codes

            _, text = registry()["mohurd"].detail(ann["url"])
            codes = classify(text, ann["doc_name"] or "")
            persist_codes(store, int(ann["id"]), codes, ann["doc_name"] or "")
            store.commit()
            mentions = store.mentions_for(int(ann["id"]))
        primary = next((m for m in mentions if m["role"] in ("primary", "target_of_revision")), None)
        if primary is None:
            raise NotIndexed(
                "该公告尚未解析出标准编号",
                hint="先 guiju index --match <关键词>；若仍无编号，说明该公告正文确实没有编号",
                page_url=ann["url"],
            )
        code_key, code_display = primary["code_key"], primary["code_display"]

    confidence = target.get("confidence") or "parsed"
    text, warnings = build_citation(
        ann["doc_name"] or "", code_display, clause, confidence=confidence
    )

    # 权威时效校验：宁可多一次查询，也不能引用一个已被替代的版本
    status, current_hint, records = _check_currency(code_display, offline=args.offline)
    if status in ("superseded", "withdrawn"):
        warnings.append(
            f"该版本状态为 {status}"
            + (f"，现行版为 {current_hint}" if current_hint else "")
        )

    if args.strict and warnings:
        raise AmbiguousCode(
            "引用串存在未核实项，--strict 下拒绝输出",
            hint="人工确认编号后去掉 --strict",
            warnings=warnings,
            cite=text,
        )

    payload = {
        "ok": True,
        "cite": text,
        "name": ann["doc_name"],
        "code": code_display,
        "code_key": code_key,
        "clause": clause,
        "confidence": confidence,
        "status": status,
        "currency": [r.as_payload() for r in records],
        "warnings": warnings,
        "published_on": ann["published_on"],
        "url": ann["url"],
    }
    render.emit(payload)
    if not render.is_json():
        print(text)
        for w in warnings:
            render.warn(w)
    return 0


def _check_currency(code_display: str | None, *, offline: bool):
    """问权威时效服务：这个编号现在是什么状态。拿不到就返回 unknown，不阻断引用。"""
    from ... import currency as currency_mod
    from ...domain.codes import normalize_key

    if offline or not code_display:
        return "unknown", "", []
    try:
        records = currency_mod.CachedCurrency().lookup(code_display)
    except SourceUnreachable:
        # 引用串本身来自本地归档，时效服务挂了只影响"是否现行"，不该整条失败
        return "unknown", "时效服务不可达：本引用未做现行性校验", []
    key = normalize_key(code_display)
    mine = next((r for r in records if normalize_key(r.code) == key), None)
    if mine is None:
        return "unknown", "", records
    current = next((r.code for r in records if r.status == "current" and r.code != mine.code), "")
    return mine.status, current, records


def add_parser(sub, parents) -> None:
    p = sub.add_parser("cite", parents=parents, help="生成标准引用串（含权威状态校验）")
    p.add_argument("ref", help="编号 / 规范名 / 公告ID / 公告URL")
    p.add_argument("clause", nargs="?", default=None, help="条款号，如 5.2.1")
    p.add_argument("--strict", action="store_true", help="存在未核实项或被替代时非零退出")
    p.add_argument("--offline", action="store_true", help="不访问时效服务")
    p.set_defaults(func=run)
