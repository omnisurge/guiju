"""可得性判定 —— 把「2014 年前的公告基本没有正文」这个事实写进代码。

2026-10-30 实测（54 条分层抽样）：
  · 2014–2026：每年抽样 2/2 都有正文附件
  · 2000–2013：仅零星（2001/2002/2004 各 1–2 例）
  · 全库合计：28/54 ≈ 52% 有附件

产品决策（所有者 2026-10-30 确认）：接受这个现实，但**必须把事实告诉使用方**——
无论人还是 agent，都不能把「历史公告没有正文」误判成「工具坏了」或「自己用错了」。
"""

from __future__ import annotations

from enum import StrEnum

#: 该年份（含）之后发布的公告基本都能拿到正文
FULL_TEXT_SINCE_YEAR = 2014


class Window(StrEnum):
    IN_WINDOW = "in_window"    # 在正文可得窗口内，拿不到就是故障
    PRE_WINDOW = "pre_window"  # 历史上通常没有正文，拿不到属正常
    UNKNOWN = "unknown"        # 日期缺失字


def state(published_on: str | None) -> Window:
    if not published_on or len(published_on) < 4 or not published_on[:4].isdigit():
        return Window.UNKNOWN
    return Window.IN_WINDOW if int(published_on[:4]) >= FULL_TEXT_SINCE_YEAR else Window.PRE_WINDOW


def note(window: Window, published_on: str | None = None) -> str:
    """给 agent 的一句话事实说明。这是「提示事实」需求的落点。"""
    if window is Window.PRE_WINDOW:
        return (
            f"该公告发布于 {published_on or '2014 年之前'}，早于 {FULL_TEXT_SINCE_YEAR} 年；"
            f"此期间的公告通常只发布标准名称与编号，不附正文文件。"
            "这不是故障，也不需要重试：请改用地方住建部门转载版本，或人工获取纸质/商业版本。"
        )
    if window is Window.IN_WINDOW:
        return (
            f"该公告发布于 {published_on}，处于正文可得窗口（{FULL_TEXT_SINCE_YEAR} 年及以后）；"
            "拿不到正文属于异常，可按退出码决定重试或上报。"
        )
    return "公告发布日期缺失，无法判定正文可得性。"


def coverage_statement() -> str:
    """README / status 共用的口径，避免文档与代码各说一套。"""
    return (
        f"{FULL_TEXT_SINCE_YEAR} 年及以后发布的公告基本都可取到正文；"
        f"{FULL_TEXT_SINCE_YEAR} 年之前的公告通常只有名称与编号，没有正文文件。"
    )
