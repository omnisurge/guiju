"""附件裁定：从公告页多个附件里选出「真正的规范正文」。

铁律：**没有把握就报歧义，绝不兜底取第一个。**

实测（2026-10-30）：2022-01-18 一份"等 4 项工程標準英文版"公告有 4 个附件，
旧的 `alive[0]` 兜底逻辑选中了《建筑设计防火规范（2018年版）》—— 与公告毫无关系。
抓不到是空，抓错是事故。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

#: 这三个词出现即排除：它们是"废止条文"一类的附属文件，不是正文
DEPRECATED_WORDS = ("废止", "作废", "条文废止")

#: 英文版公告不是官方中文正文，一律不取
NOT_APPLICABLE_WORDS = ("英文版", "英文译本")

_NAME_NOISE = re.compile(r"[《》〈〉\s（）()【】\[\]]")
_VERSION_SUFFIX = re.compile(r"[（(][^）)]*年版[）)]$")


class Verdict(StrEnum):
    OK = "ok"
    NO_ATTACHMENT = "no_attachment"
    AMBIGUOUS = "ambiguous"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class Attachment:
    url: str
    label: str = ""


@dataclass(frozen=True)
class Pick:
    verdict: Verdict
    choice: Attachment | None = None
    candidates: tuple[Attachment, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.OK and self.choice is not None

    def as_payload(self) -> dict:
        """注意：键名用 pick_reason，绝不能用 reason —— 那会覆盖错误的 reason 字段。"""
        data: dict = {"verdict": self.verdict.value}
        if self.reason:
            data["pick_reason"] = self.reason
        if self.candidates:
            data["candidates"] = [
                {"label": c.label, "url": c.url} for c in self.candidates
            ]
        return data


def _norm(s: str) -> str:
    s = _NAME_NOISE.sub("", s or "")
    return _VERSION_SUFFIX.sub("", s)


def _is_pdf(att: Attachment) -> bool:
    blob = f"{att.url} {att.label}".lower()
    return ".pdf" in blob or "pdf" in blob


def pick(atts: list[Attachment], doc_name: str = "", *, announcement_title: str = "") -> Pick:
    """裁定正文附件。返回 Pick，调用方据 verdict 决定继续还是报错。"""
    if any(w in (announcement_title or "") for w in NOT_APPLICABLE_WORDS):
        return Pick(Verdict.NOT_APPLICABLE, candidates=tuple(atts), reason="英文版公告，不是官方中文正文")

    if not atts:
        return Pick(Verdict.NO_ATTACHMENT, reason="公告页没有附件")

    alive = [a for a in atts if not any(w in a.label for w in DEPRECATED_WORDS)]
    if not alive:
        return Pick(Verdict.NO_ATTACHMENT, candidates=tuple(atts), reason="附件均为废止条文类附属文件")

    target = _norm(doc_name)
    if target:
        exact = [a for a in alive if target and target in _norm(a.label)]
        if len(exact) == 1:
            return Pick(Verdict.OK, choice=exact[0], candidates=(exact[0],), reason="名称完全匹配")
        if len(exact) > 1:
            pdfs = [a for a in exact if _is_pdf(a)]
            if len(pdfs) == 1:
                return Pick(Verdict.OK, choice=pdfs[0], candidates=tuple(exact), reason="名称匹配且唯一 PDF")

    # 候选只剩一个时不存在"选错"，名称不完全匹配只作提示：多标准合并公告的附件名
    # 常被截断（实测「城市给水工程项目建设」vs 标准名「城市给水工程项目建设标准」）。
    if len(alive) == 1:
        reason = "唯一候选附件" if target else "唯一附件"
        if target and target not in _norm(alive[0].label):
            reason = "唯一候选附件（名称不完全匹配，已按数量唯一性接受）"
        return Pick(Verdict.OK, choice=alive[0], candidates=(alive[0],), reason=reason)

    pdfs = [a for a in alive if _is_pdf(a)]
    if len(pdfs) == 1:
        return Pick(Verdict.OK, choice=pdfs[0], candidates=(pdfs[0],), reason="唯一 PDF 附件")

    return Pick(
        Verdict.AMBIGUOUS,
        candidates=tuple(alive),
        reason="多个候选且名称无法锁定，需人工指定（工具不代猜）",
    )
