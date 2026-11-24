
from __future__ import annotations

from typing import Any

from .. import localstatus
from .. import report as render
from ..domain.announcement import contains_name, match_name, names_of
from ..domain.cite import build_citation, normalize_clause
from ..domain.currency_check import CurrencyCheck, CurrencyState, completeness_of
from ..errors import (
    AmbiguousCode,
    BadUsage,
    GuijuError,
    NoCurrencyRecord,
    NotIndexed,
    SourceUnreachable,
)
from ..ports.store import StorePort
from .resolve import local_records, match_currency, ref_is_code, resolve, source_of


def check_currency(
    code_display: str | None,
    *,
    offline: bool,
    store: object | None = None,
    currency: object | None = None,
    sources: object | None = None,
) -> CurrencyCheck:
    """问权威时效服务：这个编号现在是什么状态。

    返回类型化结果（ADR 0004 D4）：「拿不到结论」也要说清是哪一种 ——
    不可达（20）、路由未覆盖（34）、没有记录（34）都不能伪装成"已校验为现行"。
    """
    from .. import currency as currency_mod
    from ..domain.codes import normalize_key

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
        # 入口可注入同一个 currency；未注入时保留旧 CachedCurrency(store=store) 兼容桥。
        port = currency if currency is not None else currency_mod.CachedCurrency(store=store)
        records = tuple(port.lookup(code_display))
        complete, incomplete_reason = completeness_of(port)
        if bool(getattr(port, "stale", False)):
            state = CurrencyState.UNREACHABLE
            message = "时效服务不可达：本引用基于旧缓存，未做现行性校验"
    except SourceUnreachable:
        state = CurrencyState.UNREACHABLE
        message = "时效服务不可达：本引用未做现行性校验"
    except NoCurrencyRecord:
        state = CurrencyState.UNSUPPORTED
        message = f"时效服务未覆盖该编号体系（{code_display}）：本引用未做现行性校验"

    if not records and localstatus.is_local_code(code_display):
        try:
            records = tuple(local_records(code_display, sources=sources))
            state = CurrencyState.OK
            message = ""
        except GuijuError as exc:
            if state is CurrencyState.OK and isinstance(exc, SourceUnreachable):
                state = CurrencyState.UNREACHABLE
                message = "时效服务不可达：本引用未做现行性校验"

    key = normalize_key(code_display)
    mine, current_rec = match_currency(list(records), key)
    if mine is None:
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
        state,
        status=mine.status,
        current=current,
        records=records,
        message=message,
        complete=complete,
        incomplete_reason=incomplete_reason,
    )


def run(store: StorePort, args: Any) -> int:
    ctx = getattr(args, "app_context", None)
    offline = bool(getattr(args, "offline", getattr(ctx, "offline", False)))
    strict = bool(getattr(args, "strict", False))
    ref = getattr(args, "ref", "")
    # 先校验输入，再解析目标：否则非法条款号会被"解析失败"抢先，错误信息误导
    raw_clause = getattr(args, "clause", None)
    clause = normalize_clause(raw_clause)
    if raw_clause and clause is None:
        raise BadUsage(
            f"条款号无法识别：{raw_clause}",
            hint="接受 5.2.1 / 第5.2.1条 / 5.2.1条 三种写法",
        )

    from .. import currency as currency_mod

    if strict and offline:
        # --strict 承诺"宁可不给，也不给不确定的引用"；离线做不了现行性校验，
        # 两者相加只可能是静默降级 —— 按用法错误当场拒掉，不等到解析完再说。
        raise BadUsage(
            "--strict 需要时效服务做现行性校验，与 --offline 冲突",
            hint="去掉 --offline 再试；或去掉 --strict，接受一份未校验现行性的引用串",
        )

    # --offline 的语义是"完全不碰网络"：连时效端口都不用，而不是只跳过详情页核实。
    # 在线时优先用入口注入的端口；旧直接调用才构造带落库缓存的兼容实例。
    ctx = getattr(args, "app_context", None)
    if offline:
        currency = currency_mod.NullCurrency()
    elif ctx is not None and getattr(ctx, "currency", None) is not None:
        currency = ctx.currency
    else:
        currency = currency_mod.CachedCurrency(store=store)
    target = resolve(
        store,
        ref,
        currency=currency,
        allow_network=not offline,
        sources=getattr(ctx, "sources", None),
    )
    ann = target["ann"]
    # 多名称公告（P1-3）：按名称引用时用用户查询的那一本，不能固定写标题里的第一本。
    citation_name = ann["doc_name"] or ""
    book_name = ""
    if target.get("via") in ("name", "name_current_version"):
        chosen, _matched = match_name(names_of(ann["title"], ann["doc_name"]), ref)
        if chosen:
            citation_name = book_name = chosen
    code_key = target.get("code_key")
    code_display = target.get("code_display")
    # 按编号查询时 resolve 已给出 code_key；若公告标题多本，仍要按该编号
    # 的 mention 证据选书名，不能固定用第一本 doc_name（P1-3 跨面）。
    if code_key:
        from ..domain.codes import normalize_key as _normalize_key

        want_key = _normalize_key(code_key)
        for row in store.mentions_for(int(ann["id"])):
            if row["code_key"] != want_key:
                continue
            for candidate_name in names_of(ann["title"], ann["doc_name"]):
                if contains_name(row["evidence"], candidate_name):
                    citation_name = book_name = candidate_name
                    break
            if book_name:
                break
    # 兜底编号来源的置信度（None=正文解析所得，自带核实）；
    # 必须在 if 外声明，解析路径下 build_citation 后的 strict 检查也要读它。
    code_conf: str | None = None

    if not code_key:
        mentions = store.mentions_for(int(ann["id"]))
        if not mentions and not offline:
            # 没有本地编号记录就现场解析一次（1 个请求），这样 cite 也能按名称/公告ID直接工作。
            # 详情页按 URL 归属源路由（M4），api_mode 源没有 detail 时跳过现场解析。
            from ..domain.codes import classify

            ctx = getattr(args, "app_context", None)
            sources = getattr(ctx, "sources", None) if ctx is not None else None
            if sources is None:
                sources = getattr(args, "_sources", None)
            if sources is None:
                from ..sources import registry as _registry

                sources = _registry()
            sid = source_of(ann["url"])["id"]
            source = sources.get(sid) or sources["mohurd"]
            detail = getattr(source, "detail", None)
            if detail is not None:
                _, text = detail(ann["url"])
                codes = classify(text, citation_name or ann["doc_name"] or "")
                if book_name and len(names_of(ann["title"], ann["doc_name"])) > 1:
                    # 多名称公告按名称引用：只落这本书证据里的编号，
                    # 否则会把另一本的编号写进库（P1-A 的落库侧）。
                    from ..domain.codes import Codes, Confidence, Role

                    relevant = tuple(
                        m for m in codes.mentions
                        if contains_name(m.evidence, book_name)
                    )
                    decisive = tuple(
                        m for m in relevant
                        if m.role in (Role.PRIMARY, Role.TARGET_OF_REVISION)
                    )
                    keys = {m.code_key for m in decisive}
                    if len(keys) != 1:
                        raise AmbiguousCode(
                            f"公告标题含多本规范，正文没有与「{book_name}」"
                            "对应的唯一编号证据",
                            hint="按编号引用，或人工确认这本书的编号后再试",
                            candidates=[
                                {"code": m.code_display, "role": m.role.value,
                                 "evidence": m.evidence[:160]}
                                for m in (relevant or codes.mentions)
                            ],
                        )
                    chosen = decisive[0]
                    codes = Codes(
                        mentions=relevant,
                        primary_key=chosen.code_key,
                        primary_display=chosen.code_display,
                        confidence=Confidence.PARSED,
                    )
                store.persist_codes(int(ann["id"]), codes, citation_name or ann["doc_name"] or "")
                store.commit()
                mentions = store.mentions_for(int(ann["id"]))
        # 多名称公告按名称引用时，只认证据里出现这本书的编号；否则正文里
        # 第一个编号会被静默写成另一本书的引用（P1-3 / P1-A）。
        if book_name and len(names_of(ann["title"], ann["doc_name"])) > 1:
            relevant = [m for m in mentions if contains_name(m["evidence"], book_name)]
            if not relevant:
                raise AmbiguousCode(
                    f"公告标题含多本规范，正文没有与「{book_name}」对应的编号证据",
                    hint="按编号引用，或人工确认这本书的编号后再试",
                    candidates=[
                        {"code": m["code_display"], "role": m["role"],
                         "confidence": m["confidence"]}
                        for m in mentions
                    ],
                )
            mentions = relevant
        target_rows = [m for m in mentions if m["role"] == "target_of_revision"]
        parsed_targets = [m for m in target_rows if m["confidence"] == "parsed"]
        if len(target_rows) > 1 or (target_rows and not parsed_targets):
            # 多 target（或落库的 target 本身 ambiguous）不能按排序取第一条：
            # 那会把 P1-9 的歧义重新变成确定引用。交 32，让人选。
            raise AmbiguousCode(
                "公告含多个局部修订目标，无法确定引用哪一本"
                if len(target_rows) > 1
                else "局部修订目标未经正文核实，无法确定引用",
                hint="从 candidates 里人工选定版本，禁止按年份/排序自行猜测",
                candidates=[
                    {"code": m["code_display"], "role": m["role"],
                     "confidence": m["confidence"]}
                    for m in target_rows
                ],
            )
        primary_candidates = [
            m for m in mentions if m["role"] in ("primary", "target_of_revision")
        ]
        parsed = [m for m in primary_candidates if m["confidence"] == "parsed"]
        if len(parsed) == 1:
            primary = parsed[0]
        elif len(parsed) > 1:
            # 同一本规范的两个编号写法（JGJ450-2018 与 JGJ/T450-2018）不是两个版本：
            # 取年份更新的一条；同年优先 target_of_revision。跨主体才交 32。
            from ..domain.codes import _code_subject

            def _year_of(key: str) -> str:
                import re
                match = re.search(r"-(\d{4})$", key or "")
                return match[1] if match else ""

            subjects = {_code_subject(m["code_key"]) for m in parsed}
            if len(subjects) == 1:
                primary = max(
                    parsed,
                    key=lambda m: (
                        _year_of(m["code_key"]),
                        m["role"] == "target_of_revision",
                    ),
                )
            else:
                raise AmbiguousCode(
                    "公告有多个已核实编号，无法确定引用哪一本",
                    candidates=[
                        {"code": m["code_display"], "role": m["role"],
                         "confidence": m["confidence"]}
                        for m in primary_candidates
                    ],
                )
        elif len(primary_candidates) == 1:
            # 单条 primary 但 confidence=ambiguous：仍可出串，但 confidence 会带
            # ambiguous，warnings 非空；不把它伪装成已核实。
            primary = primary_candidates[0]
        elif len(primary_candidates) > 1:
            raise AmbiguousCode(
                "公告有多个编号候选，无法确定引用哪一本",
                candidates=[
                    {"code": m["code_display"], "role": m["role"],
                     "confidence": m["confidence"]}
                    for m in primary_candidates
                ],
            )
        else:
            primary = None
        # 多名称公告按 ID/编号引用时，引用书名取"编号证据所在的那本书"，
        # 不能固定写 doc_name（第一本）；否则会出现《甲》+乙编号的错配。
        if primary is not None and not book_name:
            for candidate_name in names_of(ann["title"], ann["doc_name"]):
                if contains_name(primary["evidence"], candidate_name):
                    citation_name = book_name = candidate_name
                    break
        if primary is None and ref_is_code(ref):
            # M14：公告正文没解析出编号，但 ref 本身就是编号——用 ref 出引用串，
            # 标 confidence=unknown + warning（--strict 会因 warning 硬失败），
            # 而不是抛"未收录"把用户已给对的编号也拒掉。
            from ..domain.codes import normalize_key

            code_key, code_display, code_conf = normalize_key(ref), ref, "arg"
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
        citation_name, code_display, clause, confidence=confidence
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
    check = _check_currency(
        code_display, offline=offline, store=store, currency=currency,
        sources=getattr(ctx, "sources", None),
    )
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

    if strict:
        # P3-7：--strict 的失败体不携带候选引用串；需要看候选请去掉 --strict，
        # 由非 strict 输出带 warning 的 cite。
        if check.state is CurrencyState.UNREACHABLE:
            raise SourceUnreachable(
                check.message or "时效服务不可达",
                hint="稍后重试；或加 --offline / 去掉 --strict，接受一份带 warning 的引用串",
                currency_check=check.as_payload(),
            )
        if check.state in (CurrencyState.UNSUPPORTED, CurrencyState.NO_RECORD):
            raise NoCurrencyRecord(
                check.message or "时效侧没有该编号的记录",
                hint="时效路由未覆盖该编号体系；去掉 --strict 可输出引用串 + warning，或加 --offline",
                currency_check=check.as_payload(),
            )
        if warnings:
            raise AmbiguousCode(
                "引用串存在未核实项，--strict 下拒绝输出",
                hint="人工确认编号后去掉 --strict",
                warnings=warnings,
            )

    payload = {
        "ok": True,
        "cite": text,
        "name": citation_name,
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
        render.out(text)
        for w in warnings:
            render.warn(w)
    return 0


def _check_currency(
    code_display: str | None, *, offline: bool, store=None, currency=None, sources=None
) -> CurrencyCheck:
    """兼容名字：实现已迁到 guiju.application.cite，测试仍可 patch 本模块。"""
    return check_currency(
        code_display, offline=offline, store=store, currency=currency, sources=sources
    )
