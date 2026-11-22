"""时效适配器的解析与完整性契约（W3 / P1-4）。

两件事必须能区分开，否则会静默给出否定结论：

1. **空结果的来源**。只有"源明确回答没有"才可以返回 []。缺必需字段、结构不符、
   页面有数据痕迹却解析不出记录，一律抛 NeedsHuman(50)：这些是"我们没读懂"，
   不是"没有这本规范"。
2. **结果的完整性**。complete=False 表示没取全（无总页数证据、翻页上限截断、
   明文回退……）。NativeCurrency 只把 complete 的结果写成缓存——不完整的空结果
   一旦落盘，就是 24 小时的假阴性。

本模块只做数据契约与错误分类，不含网络与存储。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import NeedsHuman
from .model import Record

_HINT = "检查对应源的响应结构；解析失败、有数据痕迹但 0 条都不能当作『没有这条规范』"


def json_error(source: str, detail: str) -> NeedsHuman:
    return NeedsHuman(f"{source} 元数据 JSON 结构异常：{detail}", hint=_HINT)


def page_error(source: str, detail: str) -> NeedsHuman:
    return NeedsHuman(f"{source} 元数据页面结构异常：{detail}", hint=_HINT)


def require_keys(payload: dict, keys, source: str) -> None:
    """必需顶层字段必须存在。

    键存在但值为 null 不算"缺字段"（Go 解码到零值，历史响应确实这么给）；
    键不存在才是结构不符——WAF 错误体、接口改名都会命中这一条。
    """
    missing = [key for key in keys if key not in payload]
    if not missing:
        return
    seen = ", ".join(sorted(str(key) for key in payload)[:10]) or "无"
    raise json_error(source, f"缺少必需字段 {', '.join(missing)}；顶层字段：{seen}")


def parse_or_fail(rows: list, records: list[Record], source: str, *, where: str = "") -> list[Record]:
    """有原始行却解析不出任何记录 = 结构改版，不是权威空结果。"""
    if records:
        return records
    if rows:
        raise json_error(source, f"{where or '响应'} 返回 {len(rows)} 条记录但 0 条可解析")
    return records


@dataclass(frozen=True)
class Completeness:
    """一次 lookup 的完整性证据；complete 是"可以当权威答案并写缓存"的唯一判据。"""

    fetched_pages: int = 0
    total: int | None = None
    has_more: bool | None = None
    truncated: bool = False
    reason: str = ""
    insecure_transport: bool = False
    #: 源明确回答"没有这条记录"的权威空结果。与 complete 正交：明文回退的空结果
    #: 仍 complete=False（不可作权威缓存），但它不是"没读懂/没取全"，调用方据此
    #: 把未知编号映射成 31（本地没收录）而不是 50（需人工排查适配器）。
    empty_authoritative: bool = False

    @property
    def complete(self) -> bool:
        return (
            not self.truncated
            and self.has_more is False
            and not self.insecure_transport
        )

    @property
    def cacheable(self) -> bool:
        """可以写入缓存：取全且传输可信。明文回退（含空结果）一律不写。"""
        return self.complete and not self.insecure_transport

    def as_pagination(self) -> dict:
        return {
            "fetched_pages": self.fetched_pages,
            "total": self.total,
            "has_more": self.has_more,
            "truncated": self.truncated,
            "reason": self.reason,
        }


class CompletenessMixin:
    """适配器共享的完整性投影：子类只需在 lookup 末尾 _record_completeness(...)。

    没有这个属性的老适配器由调用方按 complete=True 处理（旧契约不变）。
    """

    _completeness: Completeness = Completeness()

    @property
    def complete(self) -> bool:
        return self._completeness.complete

    @property
    def incomplete_reason(self) -> str:
        return self._completeness.reason

    @property
    def empty_authoritative(self) -> bool:
        return self._completeness.empty_authoritative

    @property
    def insecure_transport(self) -> bool:
        return self._completeness.insecure_transport

    @property
    def truncated(self) -> bool:
        return self._completeness.truncated

    @property
    def has_more(self) -> bool | None:
        return self._completeness.has_more

    @property
    def cacheable(self) -> bool:
        return self._completeness.cacheable

    @property
    def pagination(self) -> dict:
        return self._completeness.as_pagination()

    def _record_completeness(self, completeness: Completeness) -> None:
        self._completeness = completeness
