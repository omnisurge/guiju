"""cite / which 的时效检查结果 —— 把「做没做现行性校验」变成可判定的状态。

P1-6/P1-7 的根因是「没做成」被塞进一段 note：note 不进 warnings，--strict 看不到；
非 strict 又把 34 当成整条失败。状态化之后，调用方（CLI 及未来的 application 层）
按 state 决定输出 warning 还是映射退出码，不再靠字符串匹配。

完整性（W3 / P1-4）是正交的一维：state 说明「为什么没校验成」，complete 说明
「这次答案是否取全」。complete=False 的空结果不能当作权威的「没有这条编号」，
所以它既不进 no_record，也不写缓存；调用方必须给出 warning。
"""

from __future__ import annotations

from dataclasses import dataclass

from .._compat import StrEnum

#: 沿 .inner 往上找完整性证据的层数（_ReplayCurrency → CachedCurrency → NativeCurrency）
_INNER_DEPTH = 3


class CurrencyState(StrEnum):
    OK = "ok"                    # 服务给了结论（含 superseded/withdrawn）
    UNREACHABLE = "unreachable"  # 服务不可达，没做成校验（strict → 20）
    UNSUPPORTED = "unsupported"  # 时效路由未覆盖该编号体系（strict → 34）
    NO_RECORD = "no_record"      # 服务明确没有这条编号 / 返回里没有它（strict → 34）
    OFFLINE = "offline"          # 用户显式 --offline，不做校验


def completeness_of(currency: object) -> tuple[bool | None, str]:
    """读时效端口的完整性证据（W3 的 complete / incomplete_reason）。

    只做 duck-typing，不 import currency 层：
      · complete=False：没取全（无总页数证据 / 翻页截断 / 明文回退），答案可能缺记录；
      · complete=None：内层适配器没有完整性契约（老实现），按 W3 的约定视作完整；
      · reason 依次从端口自身、内层适配器、最近一次 Outcome 上取。
    """
    chain: list[object] = []
    node: object | None = currency
    for _ in range(_INNER_DEPTH):
        if node is None:
            break
        chain.append(node)
        node = getattr(node, "inner", None)

    complete = getattr(chain[0], "complete", None) if chain else None
    for holder in chain:
        reason = getattr(holder, "incomplete_reason", "")
        if reason:
            return complete, str(reason)
        outcome = getattr(holder, "last_outcome", None)
        reason = getattr(outcome, "incomplete_reason", "") if outcome is not None else ""
        if reason:
            return complete, str(reason)
    return complete, ""


@dataclass(frozen=True)
class CurrencyCheck:
    state: CurrencyState
    status: str = "unknown"
    current: str = ""
    records: tuple = ()
    message: str = ""
    #: None = 内层适配器没有完整性契约（老实现，按完整处理）；False = 没取全
    complete: bool | None = None
    incomplete_reason: str = ""

    @property
    def verified(self) -> bool:
        """查询完成且结果完整才算"校验过"；None 按老契约视作完整。"""
        return self.state is CurrencyState.OK and self.complete is not False

    @property
    def needs_warning(self) -> bool:
        """不可达 / 未覆盖 / 无记录 / 结果不完整 都必须进 warnings。"""
        return (
            self.state
            in (
                CurrencyState.UNREACHABLE,
                CurrencyState.UNSUPPORTED,
                CurrencyState.NO_RECORD,
            )
            or self.complete is False
        )

    def warning_text(self) -> str:
        notes: list[str] = []
        if self.message:
            notes.append(self.message)
        if self.complete is False:
            reason = f"：{self.incomplete_reason}" if self.incomplete_reason else ""
            notes.append(
                f"时效结果不完整{reason}；空结果不能当作「没有这条编号」，"
                "本引用未做完整现行性校验"
            )
        return "；".join(notes)

    def as_payload(self) -> dict:
        data: dict = {"state": self.state.value, "verified": self.verified}
        if self.complete is not None:
            data["complete"] = self.complete
        if self.complete is False and self.incomplete_reason:
            data["incomplete_reason"] = self.incomplete_reason
        if self.message:
            data["message"] = self.message
        if self.status != "unknown":
            data["status"] = self.status
        return data
