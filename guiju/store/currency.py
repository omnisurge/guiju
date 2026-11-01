"""原生时效元数据的 TTL 磁盘后端；适配器本身不接触文件系统。

目录只能由调用者显式传入：构造、get 和 get_any 均不创建目录，只有
put 可以写入。schema / 文件名前缀与旧 Go 缓存故意隔离，不承诺互读。
缓存是尽力而为的优化；损坏或 IO 失败不应破坏查询，也不应伪装成负缓存。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import fields
from pathlib import Path

from guiju.currency.cache import CACHE_NAMESPACE, DEFAULT_TTL, CacheEntry
from guiju.currency.model import Record

_SCHEMA = 1
_MAX_CACHE_BYTES = 4 * 1024 * 1024
_ENVELOPE_FIELDS = frozenset({"schema", "namespace", "query", "queried_at", "results"})
_RECORD_FIELDS = tuple(field.name for field in fields(Record))
_RECORD_FIELD_SET = frozenset(_RECORD_FIELDS)
_STATUSES = frozenset({"current", "superseded", "upcoming", "withdrawn", "unknown"})


def _timestamp(value: object) -> float | None:
    # bool 虽是 int 的子类，也不能充当 schema 或时间。极大 JSON 整数可能溢出。
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        stamp = float(value)
    except OverflowError:
        return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def _valid_record(row: object) -> bool:
    return (
        isinstance(row, dict)
        and row.keys() == _RECORD_FIELD_SET
        and all(isinstance(row[key], str) for key in _RECORD_FIELDS)
        and row["status"] in _STATUSES
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """拒绝重复键，避免 JSON 解析器对污染数据作最后一个值获胜的裁决。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cache field")
        result[key] = value
    return result


class DiskCurrencyCache:
    """原始查询和路由 namespace 隔离的 schema-1 缓存。

    None / 空字符串禁用缓存；不猜测 GUIJU_HOME 或任何默认目录。
    get_any 只忽略 TTL，不放宽结构、时间及查询身份校验；30 天降级上限
    属于上层查询策略，不在持久化后端中偷偷截断。
    """

    def __init__(
        self,
        directory: str | Path | None,
        ttl: float = DEFAULT_TTL,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.directory = None if directory is None or directory == "" else Path(directory)
        self.ttl = ttl
        self._clock = clock

    def _path(self, code: str, namespace: str) -> Path | None:
        if self.directory is None or not isinstance(code, str) or not isinstance(namespace, str):
            return None
        # JSON 数组有明确边界，不会因冒号、NUL、路径分隔符等拼接而串键。
        key = json.dumps([namespace, code], ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        return self.directory / f"native-v1-{digest}.json"

    def _read(self, path: Path, code: str, namespace: str, now: float) -> CacheEntry | None:
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_CACHE_BYTES + 1)
            if len(raw) > _MAX_CACHE_BYTES:
                return None
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(data, dict) or data.keys() != _ENVELOPE_FIELDS:
            return None
        if type(data["schema"]) is not int or data["schema"] != _SCHEMA:
            return None
        if data["namespace"] != namespace or data["query"] != code:
            return None
        stamp = _timestamp(data["queried_at"])
        if stamp is None or stamp > now or not isinstance(data["results"], list):
            return None
        if not all(_valid_record(row) for row in data["results"]):
            return None
        return CacheEntry(stamp, tuple(Record(**row) for row in data["results"]))

    def get(self, code: str, *, namespace: str = CACHE_NAMESPACE) -> CacheEntry | None:
        path = self._path(code, namespace)
        if path is None or self.ttl <= 0:
            return None
        now = _timestamp(self._clock())
        if now is None:
            return None
        entry = self._read(path, code, namespace, now)
        return entry if entry is not None and entry.fresh(self.ttl, now) else None

    def get_any(self, code: str, *, namespace: str = CACHE_NAMESPACE) -> CacheEntry | None:
        path = self._path(code, namespace)
        if path is None:
            return None
        now = _timestamp(self._clock())
        return None if now is None else self._read(path, code, namespace, now)

    def put(self, code: str, records: list[Record], *, namespace: str = CACHE_NAMESPACE) -> None:
        path = self._path(code, namespace)
        if path is None:
            return
        now = _timestamp(self._clock())
        if now is None or not isinstance(records, list):
            return
        rows = []
        for record in records:
            if not isinstance(record, Record):
                return
            row = {key: getattr(record, key) for key in _RECORD_FIELDS}
            if not _valid_record(row):
                return
            rows.append(row)
        data = {
            "schema": _SCHEMA,
            "namespace": namespace,
            "query": code,
            "queried_at": now,
            "results": rows,
        }
        try:
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except ValueError:
            return
        if len(raw) > _MAX_CACHE_BYTES:
            return

        temporary = None
        try:
            # _path 有返回值时目录已显式给定；只在完整验证成功后建目录。
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError):
            pass
        finally:
            # 不扫目录、不清理他人临时文件；replace 成功时此路径已不存在。
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink()
