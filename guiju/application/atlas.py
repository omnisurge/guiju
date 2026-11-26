"""应用层 atlas：标准图集完整目录同步、检索与清单查询编排。

零隐式适配器或存储构造；AtlasCatalogStore 与 fetch_catalogue 均由调用方显式注入。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from ..domain import atlas
from ..domain.atlas import AtlasCatalogEntry
from ..domain.search import tokens_of
from ..errors import BadUsage, NeedsHuman
from ..store.atlas import AtlasCatalogStore

VALID_STATUSES = frozenset({"all", "current", "withdrawn", "superseded", "upcoming"})

_STATUS_GROUPS: dict[str, frozenset[str]] = {
    "all": frozenset({"current", "upcoming", "superseded", "withdrawn", "unknown"}),
    "current": frozenset({"current"}),
    "upcoming": frozenset({"upcoming"}),
    "superseded": frozenset({"superseded"}),
    "withdrawn": frozenset({"withdrawn", "superseded"}),
}

_STATUS_RANK = {"current": 0, "upcoming": 1, "superseded": 2, "withdrawn": 3, "unknown": 4}


def _entry_dict(entry: AtlasCatalogEntry) -> dict[str, str]:
    return {
        "book_id": entry.book_id,
        "code": entry.code,
        "code_key": entry.code_key,
        "name": entry.name,
        "status": entry.status,
        "replaces": entry.replaces,
        "replaced_by": entry.replaced_by,
        "url": entry.url,
    }


def _validate_status(status: str) -> str:
    clean = str(status or "all").strip().lower()
    if clean not in VALID_STATUSES:
        allowed = " / ".join(sorted(VALID_STATUSES))
        raise BadUsage(f"不支持的图集状态筛选：{status}（可选：{allowed}）")
    return clean


def sync_atlas(
    atlas_store: AtlasCatalogStore,
    *,
    fetch_catalogue: Callable[[], bytes],
    if_stale: int | None = None,
    offline: bool = False,
    now: float | None = None,
) -> dict:
    """单次同步国标电子书库《简明目录》至独立 JSON 快照（不改动 catalog.db）。"""
    if if_stale is not None and if_stale < 0:
        raise BadUsage("--if-stale 不能为负数")

    existing = atlas_store.load()
    if if_stale is not None and existing is not None and not existing.corrupt and existing.entries:
        age = existing.age_days(now)
        if if_stale == 0 or (age is not None and age <= if_stale):
            return {
                "ok": True,
                "skipped": True,
                "reason": "fresh",
                "age_days": round(age, 2) if age is not None else 0.0,
                "synced_at": existing.synced_at,
                "total": len(existing.entries),
                "path": str(atlas_store.path),
            }

    if offline:
        raise BadUsage(
            "离线模式不能同步图集目录",
            hint="去掉 --offline 后执行 guiju atlas sync；或直接用 guiju atlas search / list 查本地已同步目录",
        )

    raw = fetch_catalogue()
    try:
        html_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NeedsHuman(
            "国标电子书库《简明目录》编码异常，无法按 UTF-8 解码",
            hint="请检查 ebook.chinabuilding.com.cn 响应编码",
        ) from exc

    entries = atlas.parse_concise_catalogue(html_text)
    saved = atlas_store.save(entries)
    counts = dict(sorted(Counter(e.status for e in saved.entries).items()))
    return {
        "ok": True,
        "skipped": False,
        "source": saved.source,
        "source_url": saved.source_url,
        "path": str(saved.path),
        "synced_at": saved.synced_at,
        "total": len(saved.entries),
        "counts": counts,
    }


def _match_prefix(entry: AtlasCatalogEntry, want_prefix: str) -> bool:
    if not want_prefix:
        return True
    return entry.code_key.startswith(want_prefix) or atlas.atlas_family(entry.code_key).startswith(want_prefix)


def _rank_entries(entries: list[AtlasCatalogEntry], query: str) -> tuple[list[AtlasCatalogEntry], str]:
    q_norm = atlas.normalize_code(query)
    tokens = [tok.lower() for tok in tokens_of(query)]
    and_hits: list[tuple[int, int, AtlasCatalogEntry]] = []
    fuzzy_hits: list[tuple[int, int, AtlasCatalogEntry]] = []

    for entry in entries:
        haystack = f"{entry.code} {entry.code_key} {entry.name} {entry.replaces} {entry.replaced_by}".lower()
        rank = _STATUS_RANK.get(entry.status, 9)
        if q_norm and entry.code_key == q_norm:
            and_hits.append((1000, rank, entry))
        elif atlas.matches(query, entry.code):
            and_hits.append((900, rank, entry))
        elif q_norm and (q_norm in entry.code_key or q_norm in atlas.atlas_family(entry.code_key)):
            and_hits.append((700, rank, entry))
        elif tokens and all(tok in haystack for tok in tokens):
            and_hits.append((500, rank, entry))
        elif tokens:
            matched_count = sum(1 for tok in tokens if tok in haystack)
            if matched_count > 0:
                fuzzy_hits.append((matched_count * 100, rank, entry))

    if and_hits:
        and_hits.sort(key=lambda item: item[2].code_key, reverse=True)
        and_hits.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in and_hits], "and"

    if fuzzy_hits:
        fuzzy_hits.sort(key=lambda item: item[2].code_key, reverse=True)
        fuzzy_hits.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in fuzzy_hits], "fuzzy"

    return [], "and"


def search_atlas(
    atlas_store: AtlasCatalogStore,
    *,
    query: str = "",
    status: str = "all",
    prefix: str = "",
    limit: int = 20,
    show_all: bool = False,
    now: float | None = None,
) -> dict:
    """只读检索本地图集目录（绝不联网、绝不建目录或写盘）。"""
    clean_status = _validate_status(status)
    if not show_all and limit < 0:
        raise BadUsage("--limit 不能为负数（要全部结果用 --all）")

    raw_query = (query or "").strip()
    clean_prefix = (prefix or "").strip()
    snap = atlas_store.load()

    if snap is None:
        return {
            "ok": True,
            "catalog_ready": False,
            "catalog_state": "missing",
            "catalog_path": str(atlas_store.path),
            "synced_at": None,
            "age_days": None,
            "catalog_total": 0,
            "query": raw_query,
            "status": clean_status,
            "prefix": clean_prefix,
            "mode": "missing",
            "total": 0,
            "count": 0,
            "truncated": False,
            "results": [],
            "hint": (
                f"本地尚未同步图集索引（{atlas_store.path} 不存在）。"
                "请先运行 guiju atlas sync 同步完整图集清单；或直接用 guiju which <图集号> / MCP query 实时查单本。"
            ),
        }

    if snap.corrupt:
        return {
            "ok": True,
            "catalog_ready": False,
            "catalog_state": "corrupt",
            "catalog_path": str(atlas_store.path),
            "synced_at": None,
            "age_days": None,
            "catalog_total": 0,
            "query": raw_query,
            "status": clean_status,
            "prefix": clean_prefix,
            "mode": "corrupt",
            "total": 0,
            "count": 0,
            "truncated": False,
            "results": [],
            "hint": (
                f"本地图集索引文件已损坏（{snap.error or '格式异常'}）。"
                "请运行 guiju atlas sync 重新同步以修复；单本急查可用 guiju which <图集号>。"
            ),
        }

    allowed_statuses = _STATUS_GROUPS[clean_status]
    want_prefix = atlas.normalize_code(clean_prefix) if clean_prefix else ""
    filtered = [
        entry
        for entry in snap.entries
        if entry.status in allowed_statuses and _match_prefix(entry, want_prefix)
    ]

    if raw_query:
        hits, mode = _rank_entries(filtered, raw_query)
    else:
        hits, mode = filtered, "list"

    total = len(hits)
    shown = hits if show_all else hits[:limit]
    results = [_entry_dict(entry) for entry in shown]
    age = snap.age_days(now)
    age_days = None if age is None else round(age, 2)

    hint = ""
    if not results:
        hint = "本地图集索引暂无匹配；可用 --status all 放宽状态、调整关键词，或 guiju which <图集号> 在线核对。"

    return {
        "ok": True,
        "catalog_ready": True,
        "catalog_state": "ready",
        "catalog_path": str(snap.path),
        "synced_at": snap.synced_at,
        "age_days": age_days,
        "catalog_total": len(snap.entries),
        "query": raw_query,
        "status": clean_status,
        "prefix": clean_prefix,
        "mode": mode,
        "total": total,
        "count": len(results),
        "truncated": total > len(results),
        "results": results,
        "hint": hint,
    }


def list_atlas(
    atlas_store: AtlasCatalogStore,
    *,
    status: str = "all",
    prefix: str = "",
    limit: int = 50,
    show_all: bool = False,
    now: float | None = None,
) -> dict:
    """按状态与前缀浏览本地完整图集索引清单。"""
    return search_atlas(
        atlas_store,
        query="",
        status=status,
        prefix=prefix,
        limit=limit,
        show_all=show_all,
        now=now,
    )
