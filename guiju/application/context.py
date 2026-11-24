"""应用层显式上下文。

D3：store / currency / out_dir / offline / allow_network / read_only / home 由入口构造，
深处禁止 new Store()/default_backend()。旧 CLI 命令暂时通过 from_args 获得同一份
上下文；后续逐步把参数传递改成显式 context 参数。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AppContext:
    """一次命令/服务的显式运行上下文。"""

    store: Any
    currency: Any | None = None
    out_dir: Path | None = None
    offline: bool = False
    allow_network: bool = True
    read_only: bool = False
    home: Path | None = None
    source: str = "mohurd"
    sources: Any | None = None

    @classmethod
    def from_args(
        cls, store: Any, args: Any, currency: Any | None = None, sources: Any | None = None
    ) -> AppContext:
        """从现有 argparse Namespace 构造；这是 CLI 兼容桥，不是新的业务入口。"""
        from ..files import default_out_dir
        from ..store.sqlite import default_home

        offline = bool(getattr(args, "offline", False))
        out_dir = getattr(args, "out", None)
        return cls(
            store=store,
            # 惰性：只有真正需要时效服务的命令才构造；避免 --version/search/sync 白建一次。
            currency=currency,
            out_dir=Path(out_dir).expanduser() if out_dir else default_out_dir(),
            offline=offline,
            allow_network=not offline,
            read_only=False,
            home=default_home(),
            source=str(getattr(args, "source", "mohurd") or "mohurd"),
            sources=sources,
        )

    @classmethod
    def for_readonly_mcp(cls, store: Any, currency: Any | None = None) -> AppContext:
        """MCP 服务面：只读 Store；缓存/网络由 currency 自己负责，不在这里 new。"""
        from ..store.sqlite import default_home

        return cls(
            store=store,
            currency=currency,
            out_dir=None,
            offline=False,
            allow_network=True,
            read_only=True,
            home=default_home(),
        )

    def currency_or_default(self) -> Any:
        if self.currency is not None:
            return self.currency
        from .. import currency as currency_mod

        return currency_mod.default_currency()
