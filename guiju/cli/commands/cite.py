"""cite —— 生成标准引用串（本产品在监理场景的核心价值点）。"""

from __future__ import annotations

import argparse

from ...domain.cite import build_citation, normalize_clause
from ...errors import AmbiguousCode, GuijuError, NotIndexed
from ...store.sqlite import Store
from .. import render
from ..lookup import resolve


def run(store: Store, args: argparse.Namespace) -> int:
    target = resolve(store, args.ref)
    ann = target["ann"]
    code_key = target.get("code_key")
    code_display = target.get("code_display")

    if not code_key:
        mentions = store.mentions_for(int(ann["id"]))
        primary = next((m for m in mentions if m["role"] in ("primary", "target_of_revision")), None)
        if primary is None:
            raise NotIndexed(
                "该公告尚未解析出标准编号",
                hint="先 guiju index --match <关键词>；若仍无编号，说明该公告正文确实没有编号",
                page_url=ann["url"],
            )
        code_key, code_display = primary["code_key"], primary["code_display"]

    clause = normalize_clause(args.clause)
    if args.clause and clause is None:
        raise GuijuError(
            f"条款号无法识别：{args.clause}",
            hint="接受 5.2.1 / 第5.2.1条 / 5.2.1条 三种写法",
        )

    confidence = target.get("confidence") or "parsed"
    text, warnings = build_citation(
        ann["doc_name"] or "", code_display, clause, confidence=confidence
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


def add_parser(sub, parents) -> None:
    p = sub.add_parser("cite", parents=parents, help="生成标准引用串")
    p.add_argument("ref", help="编号 / 公告ID / 公告URL")
    p.add_argument("clause", nargs="?", default=None, help="条款号，如 5.2.1")
    p.add_argument("--strict", action="store_true", help="存在未核实项时非零退出")
    p.set_defaults(func=run)
