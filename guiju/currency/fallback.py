"""主源报错才启用备源；主源成功空结果仍是最终结论。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ..errors import NeedsHuman, SourceUnreachable
from .model import Currency, QueryCancelled, Record


class FallbackCurrency:
    def __init__(self, name: str, primary: Currency | None, *backups: Currency | None,
                 cancelled: Callable[[], bool] | None = None) -> None:
        self.name = name
        self.primary = primary
        self.backups = tuple(backup for backup in backups if backup is not None)
        self.cancelled = cancelled
        self.source_names = tuple(source.name for source in (primary, *self.backups) if source is not None)
        self.cache_namespace = "fallback:" + ">".join(self.source_names)

    def _check(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise QueryCancelled("查询已取消，不再访问备源")

    def lookup(self, code: str) -> list[Record]:
        self._check()
        if self.primary is None:
            raise SourceUnreachable("主源未配置")
        try:
            rows = self.primary.lookup(code)
        except (SourceUnreachable, NeedsHuman) as exc:
            cause = exc
            reasons = [f"{self.primary.name}: {exc}"]
        else:
            self._check()
            return rows
        # ComplianceDenied / QueryCancelled / KeyboardInterrupt 不在可恢复异常白名单。
        for backup in self.backups:
            self._check()
            try:
                rows = backup.lookup(code)
            except (SourceUnreachable, NeedsHuman) as exc:
                reasons.append(f"{backup.name}: {exc}")
                continue
            self._check()
            if rows:
                return [replace(row, fallback=backup.name) for row in rows]
            reasons.append(f"{backup.name} 无命中")
        self._check()
        error_type = NeedsHuman if isinstance(cause, NeedsHuman) else SourceUnreachable
        raise error_type("主源失败且备用源均无结果（" + "；".join(reasons) + "）") from cause
