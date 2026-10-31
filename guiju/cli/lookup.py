"""把用户给的引用解析成确定性目标。

四种形态，且**只有一种**会用到本地编号表（而它已经不再是必需的地基）：

  1. URL          → 目录
  2. 纯数字        → 公告 ID
  3. 规范名（含汉字）→ 本地检索（不需要任何预建索引）
  4. 规范号        → ①本地已索引则直接用；②否则问时效服务拿名称 → 再走 3
                     ↑ 这一步替代了原来的"必须先建全量索引"（见 ADR 0001）

铁律不变：多候选报歧义（32），绝不替用户挑。
"""

from __future__ import annotations

import re

from .. import currency as currency_mod
from ..domain.search import rank, tokens_of
from ..errors import AmbiguousCode, AmbiguousTarget, GuijuError, NotIndexed, SourceUnreachable
from ..sources.mohurd import migrate_legacy_url
from ..store.sqlite import Store

_CJK = re.compile(r"[\u4e00-\u9fff]")
_CODEISH = re.compile(r"^[A-Za-z]")
_DECISIVE_ROLES = ("primary", "target_of_revision")

#: 候选里要写清"这条来自哪个源"：同名标准跨源存在（实测上海与国标有 19–20 条同名），
#: 只给 ann_id 人能选，但看不出是住建部公告还是上海地标。
_SOURCE_HOSTS = (
    ("bzjg.zjw.sh.gov.cn", "shzjw", "上海"),
    ("mohurd.gov.cn", "mohurd", "住建部"),
)


def source_of(url: str) -> dict:
    host = (url or "").split("//")[-1].split("/")[0].lower()
    for needle, sid, label in _SOURCE_HOSTS:
        if needle in host:
            return {"id": sid, "label": label}
    return {"id": "unknown", "label": "未知源"}


def resolve(
    store: Store,
    ref: str,
    currency: currency_mod.Currency | None = None,
    *,
    allow_network: bool = True,
    fetch_detail=None,
) -> dict:
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
    return _by_code(store, ref, currency, allow_network=allow_network, fetch_detail=fetch_detail)


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
                    "source": source_of(h.entry["url"]),
                    "url": h.entry["url"],
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


def _by_code(
    store: Store,
    ref: str,
    currency: currency_mod.Currency,
    *,
    allow_network: bool = True,
    fetch_detail=None,
) -> dict:
    from ..domain.codes import normalize_key

    key = normalize_key(ref)
    rows = store.find_by_code(key)
    # 只有「本次公告发布的版本」才算这个编号的家。
    # 危险案例（实测）：GB 50016-2014 只出现在《建筑防火通用规范》的"代替…"句里，
    # 若把 mentioned/superseded_ref 也当候选，就会把用户带到代替它的那本规范上去。
    decisive = [r for r in rows if r["role"] in _DECISIVE_ROLES]
    if decisive:
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
    try:
        records = currency.lookup(ref)
    except SourceUnreachable as exc:
        raise SourceUnreachable(
            f"编号 {ref} 的权威名称取不到：{exc}",
            hint="检查 GUIJU_CURRENCY_URL / NAS 是否可达；或加 --offline 后改用 guiju search 按名称查",
        ) from None
    best_record = _pick_record(records, key)
    if best_record is None or not best_record.name:
        raise NotIndexed(
            f"编号 {ref} 既不在本地索引中，时效服务也未返回可识别的名称",
            hint="确认编号是否完整（含年份）；或 guiju sync 后再试；仍无则本地目录里没有这本规范",
            currency=[r.as_payload() for r in records],
        )

    # 上游名称常带装饰（「（2026年局部修订）」「(2018年版)」「（英文版）」），
    # 这些字样在住建部公告里并不存在，直接检索会把召回打成 0 → 先剥掉再检索。
    search_name = _strip_decorations(best_record.name)
    hits, _mode = rank(store.catalog_entries(), tokens_of(search_name))
    hits = _filter_by_version_window(hits, best_record.implement_date)
    if not hits:
        raise NotIndexed(
            f"时效服务给到名称「{best_record.name}」，但本地目录没有对应公告",
            hint="先 guiju sync；若目录已同步仍无，说明该标准没有公开正文公告（如非工程建设类国标）",
            currency=[r.as_payload() for r in records],
            resolved_name=best_record.name,
        )
    ids = {h.entry["id"] for h in hits}
    ann_name = hits[0].entry.get("name") or hits[0].entry.get("doc_name")
    suspicious = len(ids) > 1 or not _name_matches(ann_name, search_name)
    if allow_network and suspicious:
        verified = _verify_by_primary_code(store, hits[:_VERIFY_MAX], key, fetch_detail)
        if verified:
            hits = verified
            ids = {h.entry["id"] for h in hits}
        elif len(ids) == 1:
            # 名称像、但正文编号对不上：宁可不给，也不把别的标准递出去
            raise NotIndexed(
                f"候选公告 #{hits[0].entry['id']}「{ann_name}」的正文编号不是 {ref}",
                hint="该编号可能没有独立公告，或条文已并入通用规范；可用 guiju search 按名称查",
                currency=[r.as_payload() for r in records],
                resolved_name=best_record.name,
            )
    if len(ids) > 1:
        raise AmbiguousTarget(
            f"按名称「{best_record.name}」命中 {len(ids)} 条公告，需要人工选定",
            candidates=[
                {
                    "ann_id": h.entry["id"],
                    "name": h.entry["name"],
                    "published_on": h.entry["published_on"],
                    "codes": h.entry["codes"],
                    "source": source_of(h.entry["url"]),
                    "url": h.entry["url"],
                }
                for h in hits[:10]
            ],
            currency=[r.as_payload() for r in records],
        )
    row = store.get_ann(int(hits[0].entry["id"]))
    result = _wrap(row, via="code_via_currency")
    result.update(
        code_key=normalize_key(best_record.code),
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


#: 有界核实上限：超过这个数量就交还人工，避免把"查证"变成"全库扫描"
_VERIFY_MAX = 6


def _verify_by_primary_code(store: Store, hits: list, key: str, fetch_detail=None) -> list:
    """候选不多时，逐条抓详情页看正文编号 —— 这是查证，不是猜测。

    实测：GB 55006-2021《钢结构通用规范》按名称命中 6 条公告；每页 1.2 秒即可确定
    哪一条的正文编号就是 GB 55006-2021。相比让用户手工翻 6 条，这个代价是值得的；
    超过 6 条则退回人工（32），不让"查证"退化成全库扫描。
    """
    from ..domain.codes import classify, normalize_key

    if len(hits) > _VERIFY_MAX:
        return []
    if fetch_detail is None:
        from ..sources import registry

        source = registry()["mohurd"]
        fetch_detail = lambda url: source.detail(url)[1]  # noqa: E731

    matched = []
    for hit in hits:
        ann = store.get_ann(int(hit.entry["id"]))
        if ann is None:
            continue
        try:
            text = fetch_detail(ann["url"])
        except Exception:
            continue
        codes = classify(text, ann["doc_name"] or "")
        primary = normalize_key(codes.primary_key or "")
        if primary and primary == key:
            matched.append(hit)
    return matched


def _pick_record(records: list, key: str):
    exact = [r for r in records if r.code.replace(" ", "").upper() == key]
    if exact:
        return exact[0]
    # 英文版（编号以 E 结尾 / 名称含"英文版"）不是官方中文正文，不参与定位
    chinese = [r for r in records if not _is_english(r)]
    pool = chinese or list(records)
    for r in pool:
        if r.status == "current":
            return r
    return pool[0] if pool else None


def _norm_name(name: str | None) -> str:
    return "".join(ch for ch in (name or "") if ch.isalnum())


def _name_matches(doc_name: str | None, search_name: str) -> bool:
    """公告名与权威名称是否指向同一本书（互含即视作一致）。"""
    left, right = _norm_name(doc_name), _norm_name(search_name)
    if not left or not right:
        return False
    return left in right or right in left


def _is_english(record) -> bool:
    return record.code.upper().rstrip().endswith("E") or "英文" in (record.name or "")


def _strip_decorations(name: str) -> str:
    """去掉上游名称里的括号补充，只留检索用的主体名。"""
    import re

    cleaned = re.sub(r"[（(][^）)]*[）)]", "", name or "")
    return cleaned.strip() or (name or "")


def _filter_by_version_window(hits: list, implement_date: str) -> list:
    """用一个不猜的原则消歧：**某版本的正文公告，必然发布在该版本实施日之前**。

    实测（老年人照料设施建筑设计标准）：同名公告有 2018-07-02（原版）与 2026-09-23（局部修订），
    JGJ/T 450-2018 的实施日是 2018-10-01，据此排除 2026 那条，无需凭"看起来像"去猜。
    """
    if len(hits) <= 1 or not implement_date:
        return hits
    dated = [h for h in hits if (h.entry.get("published_on") or "9999-99-99") <= implement_date]
    return dated or hits


def _dedupe(rows) -> list:
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["code_key"] in seen:
            continue
        seen.add(r["code_key"])
        out.append(r)
    return out
