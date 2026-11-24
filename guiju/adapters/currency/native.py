"""NativeCurrency 适配器入口；应用层只依赖 ports.currency。"""

from ...currency.native import NativeCurrency, Outcome

__all__ = ["NativeCurrency", "Outcome"]
