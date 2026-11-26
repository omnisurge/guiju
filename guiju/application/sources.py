"""应用层 sources：数据源健康度、probe 观测与按源只读探活（M4）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..autosync import MAX_AGE_DAYS
from ..errors import (
    EXIT_NEEDS_HUMAN,
    EXIT_OK,
    EXIT_UNREACHABLE,
    ComplianceDenied,
    NeedsHuman,
    SourceUnreachable,
)
from ..ports.store import StorePort


def _default_probe(_source_id: str, src: Any) -> dict[str, Any]:
    items, total = src.catalog_page(1, page_size=1)
    return {"ok": True, "items": len(items), "total": total}


def _catalog_health(store: StorePort, source_id: str) -> dict[str, Any]:
    meta = store.source_meta(source_id)
    meta["stale"] = (
        not meta["complete"]
        or meta["age_days"] is None
        or meta["age_days"] > MAX_AGE_DAYS
    )
    meta["catalog_meta_corrupt"] = store.catalog_meta_corrupt(source_id)
    return meta


def _source_health(
    catalog: dict[str, Any],
    probe_info: dict[str, Any],
    live_info: dict[str, Any] | None,
) -> str:
    if live_info is not None and not live_info.get("ok"):
        return "degraded"
    counts = probe_info.get("counts") or {}
    if (
        probe_info.get("body_changes", 0) > 0
        or counts.get("blocked", 0) > 0
        or catalog.get("catalog_meta_corrupt")
    ):
        return "degraded"
    if probe_info.get("total", 0) > 0 and counts.get("ok", 0) == 0:
        return "degraded"
    if catalog.get("complete") and not catalog.get("stale"):
        return "healthy"
    if catalog.get("synced_at"):
        return "stale"
    return "unknown"


def build_sources(
    store: StorePort,
    *,
    sources: Mapping[str, Any],
    probe: bool = False,
    probe_fn: Callable[[str, Any], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    """聚合各数据源目录水位与 probe 观测台账；probe=True 时按 1.2s 限速执行只读探活。"""
    live: dict[str, dict[str, Any]] = {}
    if probe:
        runner = probe_fn or _default_probe
        for source_id, src in sources.items():
            target_url = getattr(src, "BASE", source_id)
            try:
                res = runner(source_id, src)
                store.log_probe(source_id, target_url, 200, None, "ok", "sources --probe")
                live[source_id] = {"ok": True, **dict(res)}
            except ComplianceDenied as exc:
                store.log_probe(source_id, target_url, None, None, "blocked", exc.message)
                store.commit()
                raise
            except (NeedsHuman, SourceUnreachable) as exc:
                store.log_probe(source_id, target_url, None, None, exc.reason, exc.message)
                live[source_id] = {
                    "ok": False,
                    "exit": exc.exit,
                    "reason": exc.reason,
                    "error": exc.message,
                }
        store.commit()

    summary_by_source = store.probe_summary_by_source()
    coverage = store.body_hash_coverage()
    totals = store.probe_counts()

    entries: list[dict[str, Any]] = []
    for source_id, src in sources.items():
        catalog = _catalog_health(store, source_id)
        probe_info = summary_by_source.get(
            source_id,
            {"total": 0, "counts": {}, "last_at": "", "body_changes": 0, "recent_failures": []},
        )
        live_info = live.get(source_id)
        entry: dict[str, Any] = {
            "source": source_id,
            "display_name": getattr(src, "display_name", source_id),
            "health": _source_health(catalog, probe_info, live_info),
            "catalog": catalog,
            "probe": probe_info,
        }
        if live_info is not None:
            entry["live_probe"] = live_info
        entries.append(entry)

    if not probe:
        ok = True
        exit_code = EXIT_OK
    else:
        failed = [info for info in live.values() if not info.get("ok")]
        if not failed:
            ok = True
            exit_code = EXIT_OK
        else:
            ok = False
            exit_code = (
                EXIT_NEEDS_HUMAN
                if any(info.get("exit") == EXIT_NEEDS_HUMAN for info in failed)
                else EXIT_UNREACHABLE
            )

    payload: dict[str, Any] = {
        "ok": ok,
        "db": str(store.path),
        "probed": probe,
        "body_hash_coverage": coverage,
        "probe_totals": totals,
        "sources": entries,
    }
    return payload, exit_code
