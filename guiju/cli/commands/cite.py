"""cite —— 生成标准引用串（本产品在监理场景的核心价值点）。"""

from __future__ import annotations

import argparse

from ... import localstatus
from ...domain.cite import build_citation, normalize_clause
from ...domain.currency_check import CurrencyCheck, CurrencyState, completeness_of
from ...errors import (
    AmbiguousCode,
    BadUsage,
    GuijuError,
    NoCurrencyRecord,
    NotIndexed,
    SourceUnreachable,
)
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

    # 编号来源的信任度取最诚实一档：正文/落库证据 > 解析链路的先验值。
    # P2-11：名称/公告ID/URL 路径的 target.confidence 恒为 "unknown"（那是"没有先验值"），
    # 不能盖掉 primary mention 的 parsed —— 否则编号明明来自正文解析也报 unknown。
    if code_conf is not None:
        confidence = {"arg": "unknown"}.get(code_conf, code_conf)
    else:
        confidence = target.get("confidence") or "unknown"
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

    # 权威时效校验：宁可多一次查询，也不能引用一个已被替代的版本。
    # 拿不到结论时按 CurrencyCheck 的 state 分流（ADR 0004 D4）：
    #   非 strict：照常输出引用串 + warning（status=unknown）；
    #   strict：不可达 → 20、未覆盖/无记录 → 34、其他 warning 维持 32。
    check = _check_currency(code_display, offline=args.offline, store=store)
    status = check.status
    current_hint = check.current
    records = list(check.records)
    if status in ("superseded", "withdrawn"):
        warnings.append(
            f"该版本状态为 {status}"
            + (f"，现行版为 {current_hint}" if current_hint else "")
        )
    check_warning = check.warning_text()
    if check_warning:
        warnings.append(check_warning)

    if args.strict:
        if check.state is CurrencyState.UNREACHABLE:
            raise SourceUnreachable(
                check.message or "时效服务不可达",
                hint="稍后重试；或加 --offline / 去掉 --strict，接受一份带 warning 的引用串",
                cite=text,
                currency_check=check.as_payload(),
            )
        if check.state in (CurrencyState.UNSUPPORTED, CurrencyState.NO_RECORD):
            raise NoCurrencyRecord(
                check.message or "时效侧没有该编号的记录",
                hint="时效路由未覆盖该编号体系；去掉 --strict 可输出引用串 + warning，或加 --offline",
                cite=text,
                currency_check=check.as_payload(),
            )
        if warnings:
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
        "currency_check": check.as_payload(),
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


def _check_currency(code_display: str | None, *, offline: bool, store=None) -> CurrencyCheck:
    """问权威时效服务：这个编号现在是什么状态。

    返回类型化结果（ADR 0004 D4）：「拿不到结论」也要说清是哪一种 ——
    不可达（20）、路由未覆盖（34）、没有记录（34）都不能伪装成"已校验为现行"。
    """
    from ... import currency as currency_mod
    from ...domain.codes import normalize_key

    if offline:
        return CurrencyCheck(CurrencyState.OFFLINE)
    if not code_display:
        return CurrencyCheck(
            CurrencyState.NO_RECORD, message="编号未核实：本引用无从做现行性校验"
        )

    state = CurrencyState.OK
    records: tuple = ()
    message = ""
    complete: bool | None = None
    incomplete_reason = ""
    try:
        port = currency_mod.CachedCurrency(store=store)
        records = tuple(port.lookup(code_display))
        # W3/P1-4：没取全的答案不能被当成权威结论（尤其是空结果）
        complete, incomplete_reason = completeness_of(port)
    except SourceUnreachable:
        # 引用串本身来自本地目录，时效服务挂了只影响"是否现行"，不该整条失败
        state = CurrencyState.UNREACHABLE
        message = "时效服务不可达：本引用未做现行性校验"
    except NoCurrencyRecord:
        # 34：编号体系不在时效路由覆盖内；地方标准仍可由源站回答（见下）
        state = CurrencyState.UNSUPPORTED
        message = f"时效服务未覆盖该编号体系（{code_display}）：本引用未做现行性校验"

    if not records and localstatus.is_local_code(code_display):
        # 地方标准：源站自己有现行/废止分栏，用它兜底。问谁交给
        # lookup.local_records（按 api_mode+find 挑源，不再写死 shzjw，M4）；
        # 前缀闸门 localstatus.is_local_code 仍是唯一地方口径常量。
        try:
            records = tuple(local_records(code_display))
            state = CurrencyState.OK
            message = ""
        except GuijuError as exc:
            if state is CurrencyState.OK and isinstance(exc, SourceUnreachable):
                state = CurrencyState.UNREACHABLE
                message = "时效服务不可达：本引用未做现行性校验"

    # mine + current 的挑法与 lookup 共用 match_currency（M13）：mine 按规范化
    # 编号精确匹配；现行版先挑同族（同前缀同基号只差年份），再退服务给的第一个。
    key = normalize_key(code_display)
    mine, current_rec = match_currency(list(records), key)
    if mine is None:
        # 只有"完整"的空结果/同族结果才能断言"没有这条编号"；不完整时保持 OK，
        # 由 complete=False 走 warning（strict 32），绝不冒充权威否定（P1-4）。
        if state is CurrencyState.OK and complete is not False:
            state = CurrencyState.NO_RECORD
            message = (
                f"时效服务没有该编号的记录（{code_display}）：本引用未做现行性校验"
                if not records
                else f"时效服务只返回了同族其他版本、没有 {code_display}：本引用未做现行性校验"
            )
        return CurrencyCheck(
            state,
            message=message,
            records=records,
            complete=complete,
            incomplete_reason=incomplete_reason,
        )
    current = current_rec.code if current_rec and normalize_key(current_rec.code) != key else ""
    return CurrencyCheck(
        CurrencyState.OK,
        status=mine.status,
        current=current,
        records=records,
        complete=complete,
        incomplete_reason=incomplete_reason,
    )


def add_parser(sub, parents) -> None:
    p = sub.add_parser("cite", parents=parents, help="生成标准引用串（含权威状态校验）")
    p.add_argument("ref", help="编号 / 规范名 / 公告ID / 公告URL")
    p.add_argument("clause", nargs="?", default=None, help="条款号，如 5.2.1")
    p.add_argument("--strict", action="store_true", help="存在未核实项或被替代时非零退出")
    p.add_argument("--offline", action="store_true", help="不访问时效服务")
    p.set_defaults(func=run)
