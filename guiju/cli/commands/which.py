"""which —— 一个编号（或名称）到底是什么：权威时效 + 本地公告 + 是否有正文。

这个命令是 index 的替代品：它不需要任何预建索引就能回答"GB 55037-2022 是哪本、现在什么状态、
我本地有没有它的公告和正文"。
"""

from __future__ import annotations

import argparse

from ... import currency as currency_mod
from ... import localstatus
from ... import report as render
from ...application.resolve import ref_is_code, resolve
from ...domain.availability import note as availability_note
from ...domain.availability import state
from ...domain.currency_check import completeness_of, empty_authoritative_of
from ...errors import AmbiguousTarget, NoCurrencyRecord, NotIndexed, SourceUnreachable
from ...store.sqlite import Store


class _ReplayCurrency:
    """同一编号只问一次时效服务 —— 第二次查询会把 from_cache 谎报成"服务不可达"。

    which 自己先查一次；resolve 的 _by_code 在没有本地编号时会再查一次，第二次落到
    native 的 TTL 缓存上，from_cache 变 True、--json 就报 currency_from_cache=true。
    这里按规范化编号记住第一次的结果（异常也记住），重复查询不再打网，也不覆盖
    from_cache / stale / cache_age_days（它们留在内层对象上，只由真实查询改写）。
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self._memo: dict[str, object] = {}

    def __getattr__(self, name):  # name / from_cache / stale / cache_age_days / upstream_error
        return getattr(self.inner, name)

    def lookup(self, code: str) -> list:
        from ...domain.codes import normalize_key

        key = normalize_key(code or "")
        if key in self._memo:
            got = self._memo[key]
            if isinstance(got, Exception):
                raise got
            return list(got)  # type: ignore[arg-type]
        try:
            records = self.inner.lookup(code)
        except Exception as exc:
            self._memo[key] = exc
            raise
        self._memo[key] = records
        return records


def _currency_inner(cur):
    """CachedCurrency 把真正的服务藏在 .inner；对其它实现返回自身。"""
    return getattr(cur, "inner", cur)


def _currency_stale(cur):
    """上游刚失败、靠旧缓存回答 → True；未命中缓存 → False；老适配器没有这个字段 → None。"""
    inner = _currency_inner(cur)
    stale = getattr(cur, "stale", None)
    if stale is None:
        stale = getattr(inner, "stale", None)
    return stale


def _currency_upstream_error(cur) -> str | None:
    """最近一次上游失败的描述；没失败 / 字段不存在 → None。可能是异常对象，转 str。"""
    inner = _currency_inner(cur)
    err = getattr(cur, "upstream_error", None)
    if err is None:
        err = getattr(inner, "upstream_error", None)
    if err is None:
        return None
    return err if isinstance(err, str) else str(err)


def run(store: Store, args: argparse.Namespace) -> int:
    ctx = getattr(args, "app_context", None)
    base_currency = (
        ctx.currency
        if ctx is not None and getattr(ctx, "currency", None) is not None
        else currency_mod.CachedCurrency(store=store)
    )
    cur = _ReplayCurrency(base_currency)
    # 名称（CJK）输入时效服务必然"没收录"：只有编号形态才问时效。
    # W3-6：地标代码的 20/34 不是命令失败 —— 时效路由可能没覆盖上海体系，
    # 源站自己有现行/废止结论，继续走下面的 localstatus 分支。
    records = []
    if not args.offline and ref_is_code(args.ref):
        try:
            records = cur.lookup(args.ref)
        except (SourceUnreachable, NoCurrencyRecord):
            if not localstatus.is_local_code(args.ref):
                raise
            records = []
    source_name = cur.name
    endpoint = currency_mod.endpoint_of(_currency_inner(base_currency))
    if not records and not args.offline and localstatus.is_local_code(args.ref):
        # 地方标准：源站自己就有现行/废止结论，不必依赖外部时效服务
        try:
            records = localstatus.records(args.ref)
            source_name = "shzjw"
        # 只有"源没收录 / 源不可达"才退回空记录；合规拦截等必须照常传播
        except (NotIndexed, SourceUnreachable):
            records = []

    sources = getattr(ctx, "sources", None) if ctx is not None else None
    if sources is None:
        from ...sources import registry

        sources = registry()
    target = None
    error = None
    try:
        target = resolve(
            store,
            args.ref,
            currency=currency_mod.NullCurrency() if args.offline else cur,
            allow_network=not args.offline,
            sources=sources,
        )
    except (NotIndexed, AmbiguousTarget) as exc:
        # 本地定位失败不算命令失败：时效结论仍然要输出，错误只在提示里说明
        error = exc
    except (SourceUnreachable, NoCurrencyRecord) as exc:
        # W3-6：resolve 内部的 20/34 在地标场景降级为 local（时效记录已在上面
        # 由 localstatus 填好）；其他编号体系仍按原契约上抛。
        if not localstatus.is_local_code(args.ref):
            raise
        error = exc

    ann = target["ann"] if target else None
    # P1-3：多名称公告按第二本名称查询时，展示名也不能固定用第一本 doc_name。
    ann_name = ann["doc_name"] if ann else None
    if ann is not None and target.get("via") in ("name", "name_current_version"):
        from ...domain.announcement import match_name, names_of

        chosen, _matched = match_name(names_of(ann["title"], ann["doc_name"]), args.ref)
        if chosen:
            ann_name = chosen
    elif ann is not None and target.get("code_key"):
        # 按编号查多名称公告时，展示名也按该编号的 mention 证据选书。
        from ...domain.announcement import contains_name, names_of
        from ...domain.codes import normalize_key

        want_key = normalize_key(target["code_key"])
        for row in store.mentions_for(int(ann["id"])):
            if row["code_key"] != want_key:
                continue
            for candidate_name in names_of(ann["title"], ann["doc_name"]):
                if contains_name(row["evidence"], candidate_name):
                    ann_name = candidate_name
                    break
            if ann_name != ann["doc_name"]:
                break
    # W3/P1-4：complete=False 表示这次答案没取全（空结果可能是漏记录，不是"没有"）。
    # W3-2：empty_authoritative=True 时例外 —— 源明确回答"没有这条编号"，
    # 只是明文回退导致 complete=False，不能再说"可能漏判"。
    currency_complete, incomplete_reason = completeness_of(cur)
    currency_empty_authoritative = empty_authoritative_of(cur)
    payload = {
        "ok": True,
        "query": args.ref,
        "currency_source": source_name,
        "currency_endpoint": endpoint,
        "currency_from_cache": bool(getattr(cur, "from_cache", False)),
        "currency_cache_age_days": getattr(cur, "cache_age_days", None),
        "currency_stale": _currency_stale(cur),
        "currency_upstream_error": _currency_upstream_error(cur),
        "currency_complete": currency_complete,
        "currency_empty_authoritative": currency_empty_authoritative,
        "currency_incomplete_reason": incomplete_reason or None,
        "currency": [r.as_payload() for r in records],
        "resolved": bool(target),
        "announcement": None
        if ann is None
        else {
            "id": ann["id"],
            "name": ann_name,
            "published_on": ann["published_on"],
            "url": ann["url"],
            "availability": state(ann["published_on"]).value,
            "availability_note": availability_note(state(ann["published_on"]), ann["published_on"]),
        },
        "via": target["via"] if target else None,
        "code": target["code_display"] if target else None,
        "hint": getattr(error, "hint", "") if error else "",
    }
    if isinstance(error, AmbiguousTarget):
        payload["candidates"] = error.extra.get("candidates", [])
    render.emit(payload)
    if payload["currency_stale"]:
        # 上游刚失败过、结论靠旧缓存撑着 —— 与"命中 TTL 缓存"严格区分：
        # TTL 命中不算服务不可达，不发这条告警。
        age = payload["currency_cache_age_days"]
        suffix = f"；原因：{payload['currency_upstream_error']}" if payload["currency_upstream_error"] else ""
        render.note(
            "时效服务不可达、结论基于旧缓存"
            + (f"（{age} 天前）" if age is not None else "")
            + "：现行性可能已变化"
            + suffix
        )
    elif (
        payload["currency_stale"] is None
        and payload["currency_from_cache"]
        and payload["currency_source"] != "native"
    ):
        # 老适配器没有 stale 标记：沿用原有口径 —— 缓存命中且非 native 时提醒一次。
        render.note(
            f"时效服务不可达，使用本地缓存（{payload['currency_cache_age_days']} 天前）："
            "现行性可能已变化"
        )

    if payload["currency_complete"] is False and not payload["currency_empty_authoritative"]:
        # 不完整 ≠ 不可达：服务是通的，但这次没取全（翻页截断/无总页数证据/明文回退）。
        # 与 stale 分开说，agent 才知道该重查还是该复核。
        reason = (
            f"；原因：{payload['currency_incomplete_reason']}"
            if payload["currency_incomplete_reason"]
            else ""
        )
        render.note("时效结果不完整（未取全、未写缓存）：现行性可能漏判，重要结论建议复核" + reason)

    if not render.is_json():
        status_cn = {
            "current": "现行",
            "superseded": "被替代",
            "upcoming": "即将实施",
            "withdrawn": "废止",
            "unknown": "无法判定",
        }
        for r in records:
            render.out(
                f"{r.code}　{status_cn.get(r.status, r.status)}"
                f"　{r.name}" + (f"　实施 {r.implement_date}" if r.implement_date else "")
            )
        if ann is not None:
            render.out(f"本地公告：#{ann['id']} {ann['doc_name']}（{ann['published_on']}）")
            render.out(f"  {payload['announcement']['availability_note']}")
        elif error is not None:
            render.out(f"本地未定位：{getattr(error, 'message', str(error))}")
    return 0


def add_parser(sub, parents) -> None:
    p = sub.add_parser("which", parents=parents, help="查一个编号/名称：权威状态 + 本地公告")
    p.add_argument("ref", help="编号 或 规范名")
    p.add_argument("--offline", action="store_true", help="不访问时效服务")
    p.set_defaults(func=run)
