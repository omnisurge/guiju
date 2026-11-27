"""工程建设标准图集完整目录的本地 JSON 快照存储（独立于 catalog.db）。

契约边界：
- 仅读写显式传入的 atlas-catalog.json（通常位于 GUIJU_HOME/atlas-catalog.json），绝不触碰 catalog.db；
- 只读查询（load）绝不建目录或写文件；写入采用临时文件 + fsync + os.replace 原子替换；
- 文件损坏时不静默删除，返回 corrupt=True 快照交由上层显式提示重建。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..domain import atlas
from ..errors import NeedsHuman

_SCHEMA = 1
_MAX_BYTES = 32 * 1024 * 1024
_ENTRY_FIELDS = ("book_id", "code", "code_key", "name", "status", "replaces", "replaced_by", "url")
_ENTRY_FIELD_SET = frozenset(_ENTRY_FIELDS)
_ENVELOPE_FIELDS = frozenset({"schema", "source", "source_url", "synced_at", "synced_epoch", "total", "entries"})
_STATUSES = frozenset({"current", "superseded", "upcoming", "withdrawn", "unknown"})


def _valid_epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        stamp = float(value)
    except OverflowError:
        return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"图集目录存在重复 JSON 字段：{key}")
        result[key] = value
    return result


def _parse_entry(row: object) -> atlas.AtlasCatalogEntry | None:
    if not isinstance(row, dict) or row.keys() != _ENTRY_FIELD_SET:
        return None
    if any(not isinstance(row[k], str) for k in _ENTRY_FIELDS):
        return None
    if row["status"] not in _STATUSES:
        return None
    return atlas.AtlasCatalogEntry(**row)


@dataclass(frozen=True)
class AtlasCatalogSnapshot:
    path: Path
    synced_at: str
    synced_epoch: float
    source: str
    source_url: str
    entries: tuple[atlas.AtlasCatalogEntry, ...]
    corrupt: bool = False
    error: str = ""
    _clock: Callable[[], float] = field(default=time.time, repr=False, compare=False)

    def age_days(self, now: float | None = None) -> float | None:
        if self.synced_epoch <= 0:
            return None
        current = self._clock() if now is None else float(now)
        age = (current - self.synced_epoch) / 86400.0
        # 未来时间戳超过 1 小时不是“新鲜”，视为异常时钟交上层重同步；小幅时钟漂移按 0 处理。
        if age < -(1.0 / 24.0):
            return None
        return max(0.0, age)


class AtlasCatalogStore:
    """atlas-catalog.json 的原子落盘与严格只读装载。"""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def _corrupt(self, message: str) -> AtlasCatalogSnapshot:
        return AtlasCatalogSnapshot(
            path=self.path,
            synced_at="",
            synced_epoch=0.0,
            source="chinabuilding",
            source_url=atlas.CONCISE_CATALOGUE_URL,
            entries=(),
            corrupt=True,
            error=message,
        )

    def load(self) -> AtlasCatalogSnapshot | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                return self._corrupt("文件体积超过 32 MB 上限")
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            return self._corrupt(f"{type(exc).__name__}: {exc}")

        if not isinstance(data, dict) or data.keys() != _ENVELOPE_FIELDS:
            return self._corrupt("顶层字段不符合 schema v1")
        if type(data["schema"]) is not int or data["schema"] != _SCHEMA:
            return self._corrupt(f"不支持的 schema 版本：{data.get('schema')!r}")
        if not isinstance(data["source"], str) or not isinstance(data["source_url"], str):
            return self._corrupt("source / source_url 必须为字符串")
        if not isinstance(data["synced_at"], str) or not data["synced_at"].strip():
            return self._corrupt("synced_at 缺失或非法")
        epoch = _valid_epoch(data["synced_epoch"])
        if epoch is None:
            return self._corrupt("synced_epoch 非法")
        raw_entries = data["entries"]
        if not isinstance(raw_entries, list) or type(data["total"]) is not int or data["total"] != len(raw_entries):
            return self._corrupt("entries 或 total 统计不一致")

        parsed: list[atlas.AtlasCatalogEntry] = []
        for idx, row in enumerate(raw_entries):
            entry = _parse_entry(row)
            if entry is None:
                return self._corrupt(f"第 {idx} 条图集目录字段或状态非法")
            parsed.append(entry)

        return AtlasCatalogSnapshot(
            path=self.path,
            synced_at=data["synced_at"],
            synced_epoch=epoch,
            source=data["source"],
            source_url=data["source_url"],
            entries=tuple(parsed),
            _clock=self._clock,
        )

    def save(
        self,
        entries: Sequence[atlas.AtlasCatalogEntry],
        *,
        synced_at: str | None = None,
        synced_epoch: float | None = None,
        source_url: str = atlas.CONCISE_CATALOGUE_URL,
    ) -> AtlasCatalogSnapshot:
        epoch = _valid_epoch(self._clock() if synced_epoch is None else synced_epoch)
        if epoch is None:
            raise ValueError("synced_epoch 必须是有限正数")
        ts_str = (
            datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
            if synced_at is None
            else str(synced_at)
        )
        rows = [{key: getattr(entry, key) for key in _ENTRY_FIELDS} for entry in entries]
        payload = {
            "schema": _SCHEMA,
            "source": "chinabuilding",
            "source_url": str(source_url),
            "synced_at": ts_str,
            "synced_epoch": epoch,
            "total": len(rows),
            "entries": rows,
        }
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")

        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise NeedsHuman(
                f"图集目录写入失败：{self.path}（{type(exc).__name__}: {exc}）",
                hint="检查 GUIJU_HOME 目录权限或磁盘空间；只读目录无法执行 guiju atlas sync",
            ) from exc
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink()

        return AtlasCatalogSnapshot(
            path=self.path,
            synced_at=ts_str,
            synced_epoch=epoch,
            source="chinabuilding",
            source_url=str(source_url),
            entries=tuple(entries),
            _clock=self._clock,
        )
