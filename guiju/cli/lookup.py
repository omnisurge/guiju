"""把用户给的引用解析成确定性目标。

四种形态，且**只有一种**会用到本地编号表（而它已经不再是必需的地基）：

  1. URL          → 目录
  2. 纯数字        → 公告 ID
  3. 规范名（含汉字）→ 本地检索（不需要任何预建索引）
  4. 规范号        → ①本地已索引则直接用；②否则问时效服务拿名称 → 再走 3
                     ↑ 这一步替代了原来的"必须先跑 guiju index"

铁律不变：多候选报歧义（32），绝不替用户挑。
"""

from __future__ import annotations

import re

from .. import currency as currency_mod
from ..domain.search import rank, tokens_of
from ..errors import AmbiguousCode, AmbiguousTarget, GuijuError, NotIndexed
from ..sources.mohurd import migrate_legacy_url
from ..store.sqlite import Store

_CJK = re.compile(r"[\u4e00-\u9fff]")
_CODEISH = re.compile(r"^[A-Za-z]")
_DECISIVE_ROLES = ("primary", "target_of_revision")


def resolve(store: Store, ref: str, currency: currency_mod.Currency | None = None) -> dict:
    ref = (ref or "").strip()
    if not ref:
        raise GuijuError("引用为空")
    currency = currency or currency_mod.CachedCurrency()

    if ref.lower().startswith("http"):
        return _by_url(store, ref)
    if ref.isdigit():
        return _by_id(store, int(ref))
    if _CJK.search(ref) and not _CODEISH.match(ref):
        return _by_name(store, ref)
    return _by_code(store, ref, currency)


# ------------------------------------------------------------------ 三种入口


def _by_url(store: Store, ref: str) -> dict:
    row = store.get_ann_by_url(ref)
    if row is None:
        legacy = migrate_legacy_url(ref)
        if legacy:
            row = store.get_ann_by_url(legacy)
    if row is None:
        raise GuijuError(
            "该链接不在本地目录中，请先运行 guiju sync",
            hint="先 guiju sync 同步目录，再重试",
        )
    return _wrap(row, via="url")


def _by_id(store: Store, ann_id: int) -> dict:
    row = store.get_ann(ann_id)
    if row is None:
        raise GuijuError(f"本地目录中没有 ID={ann_id} 的公告")
    return _wrap(row, via="ann_id")


def _by_name(store: Store, ref: str) -> dict:
    hits, mode = rank(store.catalog_entries(), tokens_of(ref))
    if not hits:
        raise NotIndexed(
            f"本地目录没有匹配「{ref}」的公告",
            hint="先 guiju sync；若目录里确实没有这本标准，说明它没有公开正文公告",
        )
    ids = {h.entry["id"] for h in hits}
    decisive = [h for h in hits if not h.entry["codes"] or True]  # 名称检索不依赖编号
    if len(ids) > 1:
        raise AmbiguousTarget(
            f"「{ref}」命中 {len(ids)} 条公告",
            candidates=[
                {
                    "ann_id": h.entry["id"],
                    "name": h.entry["name"],
                    "published_on": h.entry["published_on"],
                    "codes": h.entry["codes"],
                    "match": h.match,
                }
                for h in decisive[:10]
            ],
            mode=mode,
        )
    best = decisive[0]
    row = store.get_ann(int(best.entry["id"]))
    result = _wrap(row, via="name")
    result["match"] = best.match
    return result


def _by_code(store: Store, ref: str, currency: currency_mod.Currency) -> dict:
    from ..domain.codes import normalize_key

    key = normalize_key(ref)
    rows = store.find_by_code(key)
    if rows:
        decisive = [r for r in rows if r["role"] in _DECISIVE_ROLES] or list(rows)
        keys = {r["code_key"] for r in decisive}
        if len(keys) > 1:
            raise AmbiguousCode(
                f"编号 {ref} 命中 {len(keys)} 个候选版本，需要人工选定",
                candidates=[
                    {
                        "code": r["code_display"],
                        "key": r["code_key"],
                        "published_on": r["published_on"],
                        "ann_id": r["id"],
                        "evidence": r["evidence"],
                    }
                    for r in _dedupe(decisive)
                ],
            )
        best = decisive[0]
        result = _wrap(store.get_ann(int(best["id"])), via="code_local")
        result.update(
            code_key=best["code_key"],
            code_display=best["code_display"],
            confidence=best["confidence"] or "parsed",
        )
        return result

    # ---- 本地没有这份映射：问权威时效服务要名称，再按名称找公告（不再需要 index）
    records = currency.lookup(ref)
    best_record = _pick_record(records, key)
    if best_record is None or not best_record.name:
        raise NotIndexed(
            f"编号 {ref} 既不在本地索引中，时效服务也未返回可识别的名称",
            hint="确认编号是否完整（含年份）；或先 guiju index --match <关键词> 建索引",
            currency=[r.as_payload() for r in records],
        )

    hits, mode = rank(store.catalog_entries(), tokens_of(best_record.name))
    if not hits:
        raise GuijuError(
            f"时效服务给了名称「{best_record.name}」，但本地目录没有对应公告",
            hint="先 guiju sync；该标准可能没有公开正文公告（例如非工程建设类国标）",
            currency=[r.as_payload() for r in records],
        )
    ids = {h.entry["id"] for h in hits}
    if len(ids) > 1:
        raise AmbiguousTarget(
            f"按名称「{best_record.name}」命中 {len(ids)} 条公告，需要人工选定",
            candidates=[
                {
                    "ann_id": h.entry["id"],
                    "name": h.entry["name"],
                    "published_on": h.entry["published_on"],
                    "codes": h.entry["codes"],
                }
                for h in hits[:10]
            ],
            currency=[r.as_payload() for r in records],
        )
    row = store.get_ann(int(hits[0].entry["id"]))
    result = _wrap(row, via="code_via_currency")
    result.update(
        code_key=best_record.code,
        code_display=best_record.code,
        confidence="from_currency",
        currency=[r.as_payload() for r in records],
        status=best_record.status,
    )
    return result


# ------------------------------------------------------------------ helpers


def _wrap(row, *, via: str) -> dict:
    if row is None:
        raise GuijuError("公告行缺失（数据库可能被外部改动）")
    return {
        "ann": row,
        "code_key": None,
        "code_display": None,
        "confidence": "unknown",
        "via": via,
        "currency": [],
        "status": "",
    }


def _pick_record(records: list, key: str):
    for r in records:
        if r.code.replace(" ", "").upper() == key:
            return r
    for r in records:
        if r.status == "current":
            return r
    return records[0] if records else None


def _dedupe(rows) -> list:
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["code_key"] in seen:
            continue
        seen.add(r["code_key"])
        out.append(r)
    return out
