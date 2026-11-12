"""cite —— 生成标准引用串（本产品在监理场景的核心价值点）。"""

from __future__ import annotations

import argparse

from ... import localstatus
from ...domain.cite import build_citation, normalize_clause
from ...errors import AmbiguousCode, BadUsage, GuijuError, NotIndexed, SourceUnreachable
from ...store.sqlite import Store
from .. import render
from ..lookup import local_records, match_currency, ref_is_code, resolve, source_of


def run(store: Store, args: argparse.Namespace) -> int:
    # 先校验输入，再解析目标：否则非法条款号会被"解析失败"抢先，错误信息误导
    clause = normalize_clause(args.clause)
    if args.clause and clause is None:
        raise BadUsage(
            f"条款号无法识别：{args.clause}",
            hint="接受 5.2.1 / 第5.2.1条 / 5.2.1条 三种写法",
        )

    from ... import currency as currency_mod

    if args.strict and args.offline:
        # --strict 承诺"宁可不给，也不给不确定的引用"；离线做不了现行性校验，
        # 两者相加只可能是静默降级 —— 按用法错误当场拒掉，不等到解析完再说。
        raise BadUsage(
            "--strict 需要时效服务做现行性校验，与 --offline 冲突",
            hint="去掉 --offline 再试；或去掉 --strict，接受一份未校验现行性的引用串",
        )

    # --offline 的语义是"完全不碰网络"：连时效端口都不用，而不是只跳过详情页核实。
    # 在线时同样显式给带落库缓存的端口——否则 resolve 会自建无 store 的缓存层（M9）。
    target = resolve(
        store,
        args.ref,
        currency=currency_mod.NullCurrency() if args.offline else currency_mod.CachedCurrency(store=store),
        allow_network=not args.offline,
    )
    ann = target["ann"]
    code_key = target.get("code_key")
    code_display = target.get("code_display")
    # 兜底编号来源的置信度（None=正文解析所得，自带核实）；
    # 必须在 if 外声明，解析路径下 build_citation 后的 strict 检查也要读它。
    code_conf: str | None = None

    if not code_key:
        mentions = store.mentions_for(int(ann["id"]))
        if not mentions and not args.offline:
            # 没有本地编号记录就现场解析一次（1 个请求），这样 cite 也能按名称/公告ID直接工作。
            # 详情页按 URL 归属源路由（M4），api_mode 源没有 detail 时跳过现场解析。
            from ...domain.codes import classify
            from ...sources import registry
            from ...store.sqlite import persist_codes

            sid = source_of(ann["url"])["id"]
            source = registry().get(sid) or registry()["mohurd"]
            detail = getattr(source, "detail", None)
            if detail is not None:
                _, text = detail(ann["url"])
                codes = classify(text, ann["doc_name"] or "")
                persist_codes(store, int(ann["id"]), codes, ann["doc_name"] or "")
                store.commit()
                mentions = store.mentions_for(int(ann["id"]))
        primary = next((m for m in mentions if m["role"] in ("primary", "target_of_revision")), None)
        if primary is None and ref_is_code(args.ref):
            # M14：公告正文没解析出编号，但 ref 本身就是编号——用 ref 出引用串，
            # 标 confidence=unknown + warning（--strict 会因 warning 硬失败），
            # 而不是抛"未收录"把用户已给对的编号也拒掉。
            from ...domain.codes import normalize_key

            code_key, code_display, code_conf = normalize_key(args.ref), args.ref, "arg"
        elif primary is None:
            raise NotIndexed(
                "该公告尚未解析出标准编号",
                hint="该公告正文里可能确实没有编号（约 39% 如此）；用 guiju pull 取正文人工确认",
                page_url=ann["url"],
            )
        else:
            code_key = primary["code_key"]
            code_display = primary["code_display"]
            # edition.confidence 一并记下：非 parsed 的落库编号是"未经正文核实"，
            # --strict 不得放行（M6）
            code_conf = primary["confidence"]

    # 编号来源的信任度取最诚实一档：解析结果 > 落库标注 > unknown。
    # "arg"（ref 兜底）与缺省都归 unknown，不再伪装成 parsed。
    confidence = target.get("confidence") or (
        {"arg": "unknown"}.get(code_conf, code_conf) if code_conf else None
    ) or "unknown"
    text, warnings = build_citation(
        ann["doc_name"] or "", code_display, clause, confidence=confidence
    )
    # M6：落库兜底且标注非 parsed，或编号取自命令行 ref——都属于"未经正文核实"，
    # 必须进 warnings（--strict 靠 warnings 硬失败，见下）。"ambiguous" 由
    # build_citation 自己加警告，这里不重复。
    if code_conf == "arg":
        warnings.append("编号取自命令行参数：该公告正文未解析出标准编号")
    elif code_conf is not None and code_conf != "parsed" and confidence != "ambiguous":
        warnings.append(f"编号经目录缓存获得、未经正文核实（confidence={code_conf}）")

    # 权威时效校验：宁可多一次查询，也不能引用一个已被替代的版本
    status, current_hint, records = _check_currency(code_display, offline=args.offline, store=store)
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


def _check_currency(code_display: str | None, *, offline: bool, store=None):
    """问权威时效服务：这个编号现在是什么状态。拿不到就返回 unknown，不阻断引用。"""
    from ... import currency as currency_mod
    from ...domain.codes import normalize_key

    if offline or not code_display:
        return "unknown", "", []
    records = []
    note = ""
    try:
        records = currency_mod.CachedCurrency(store=store).lookup(code_display)
    except SourceUnreachable:
        # 引用串本身来自本地目录，时效服务挂了只影响"是否现行"，不该整条失败
        note = "时效服务不可达：本引用未做现行性校验"
    if not records and not offline and localstatus.is_local_code(code_display):
        # 地方标准：源站自己有现行/废止分栏，用它兜底。问谁交给
        # lookup.local_records（按 api_mode+find 挑源，不再写死 shzjw，M4）；
        # 前缀闸门 localstatus.is_local_code 仍是唯一地方口径常量。
        try:
            records = local_records(code_display)
            note = ""
        except GuijuError:
            pass
    # mine + current 的挑法与 lookup 共用 match_currency（M13）：mine 按规范化
    # 编号精确匹配；现行版先挑同族（同前缀同基号只差年份），再退服务给的第一个。
    key = normalize_key(code_display)
    mine, current_rec = match_currency(records, key)
    if mine is None:
        return "unknown", note, records
    current = current_rec.code if current_rec and normalize_key(current_rec.code) != key else ""
    return mine.status, current, records


def add_parser(sub, parents) -> None:
    p = sub.add_parser("cite", parents=parents, help="生成标准引用串（含权威状态校验）")
    p.add_argument("ref", help="编号 / 规范名 / 公告ID / 公告URL")
    p.add_argument("clause", nargs="?", default=None, help="条款号，如 5.2.1")
    p.add_argument("--strict", action="store_true", help="存在未核实项或被替代时非零退出")
    p.add_argument("--offline", action="store_true", help="不访问时效服务")
    p.set_defaults(func=run)
