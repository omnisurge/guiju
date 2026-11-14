"""主源报错才启用备源；主源成功空结果仍是最终结论。

裁决（2026-11-14，所有者拍板）：主源应答成功但 0 条 = 该源的权威结论，
不转备源。原因：当前不存在权威备用时效源，备源只兜底"主源故障"
（SourceUnreachable/NeedsHuman），不兜底"主源漏收"；让备源对空结果越权
补判反而可能引入形似错配的假阳性。将来若接入权威备源，再重评是否对
主源空结果也走备源链。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ..errors import NeedsHuman, SourceUnreachable
from .model import Currency, QueryCancelled, Record


class FallbackCurrency:
    def __init__(self, name: str, primary: Currency | None, *backups: Currency | None,
                 cancelled: Callable[[], bool] | None = None,
                 admit: Callable[[str], None] | None = None) -> None:
        self.name = name
        self.primary = primary
        self.backups = tuple(backup for backup in backups if backup is not None)
        self.cancelled = cancelled
        # admit 与 native._guard 分工一致：一次 lookup 计一次"上游查询"，只在入口处计。
        self.admit = admit
        self.source_names = tuple(source.name for source in (primary, *self.backups) if source is not None)
        self.cache_namespace = "fallback:" + ">".join(self.source_names)

    def _check(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise QueryCancelled("查询已取消，不再访问备源")

    def lookup(self, code: str) -> list[Record]:
        self._check()
        if self.admit is not None:
            self.admit(code)
        if self.primary is None:
            raise SourceUnreachable("主源未配置")
        try:
            rows = self.primary.lookup(code)
        except (SourceUnreachable, NeedsHuman) as exc:
            cause = exc
            reasons = [f"{self.primary.name}: {exc}"]
        else:
            self._check()
            # 主源成功应答（含 0 条）= 权威终局，不转备源。见模块 docstring。
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
