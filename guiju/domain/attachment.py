"""附件裁定：从公告页多个附件里选出「真正的规范正文」。

铁律：**没有把握就报歧义，绝不兜底取第一个。**

实测（2026-10-30）：2022-01-18 一份"等 4 项工程標準英文版"公告有 4 个附件，
旧的 `alive[0]` 兜底逻辑选中了《建筑设计防火规范（2018年版）》—— 与公告毫无关系。
抓不到是空，抓错是事故。

多名称公告（ADR 0004 D4 / P1-3）：标题含多本书时，「要哪一本」只能由用户查询的
名称指明。按编号/公告ID/URL 查询时没有这个信息，就报 33（candidates），
绝不拿 doc_name（目录里第一个名称）兜底 —— 那会静默下载另一本书。
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass

from .._compat import StrEnum
from .announcement import extract_names, match_name

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
    #: 公告标题里的全部书名（多本合订时人能据此把附件对上号）
    book_names: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict is Verdict.OK and self.choice is not None

    def as_payload(self) -> dict:
        """注意：键名用 pick_reason，绝不能用 reason —— 那会覆盖错误的 reason 字段。"""
        data: dict = {"verdict": self.verdict.value}
        if self.reason:
            data["pick_reason"] = self.reason
        if self.book_names:
            data["names"] = list(self.book_names)
        if self.candidates:
            data["candidates"] = [
                {"label": c.label, "url": c.url} for c in self.candidates
            ]
        return data


def _norm(s: str) -> str:
    s = _NAME_NOISE.sub("", s or "")
    return _VERSION_SUFFIX.sub("", s)


def _is_pdf(att: Attachment) -> bool:
    # 「是不是 PDF」只看文件扩展：URL 路径以 .pdf 结尾（剥掉 query/fragment），
    # 或标签里出现独立的 ".pdf" 字样。裸 "pdf" 子串会误伤：实测「X规范PDF版.zip」、
    # 「xxpdfyy」这类名字都含 pdf 却不是 PDF。
    path = urllib.parse.urlsplit(att.url).path.lower()
    if path.endswith(".pdf"):
        return True
    return re.search(r"\.pdf\b", att.label.lower()) is not None


def _book_names(target_names: Sequence[str] | str, announcement_title: str) -> tuple[str, ...]:
    """公告的全部名称：标题里的书名优先，其次调用方传入的名称（去重保序）。"""
    passed = (target_names,) if isinstance(target_names, str) else tuple(target_names or ())
    merged: list[str] = []
    for name in extract_names(announcement_title) + passed:
        clean = (name or "").strip()
        if clean and clean not in merged:
            merged.append(clean)
    return tuple(merged)


def _ambiguous(alive: list[Attachment], names: tuple[str, ...], reason: str) -> Pick:
    return Pick(Verdict.AMBIGUOUS, candidates=tuple(alive), reason=reason, book_names=names)


def pick(
    atts: list[Attachment],
    target_names: Sequence[str] | str = (),
    *,
    ref_name: str = "",
    announcement_title: str = "",
) -> Pick:
    """裁定正文附件。返回 Pick，调用方据 verdict 决定继续还是报错。

    target_names 是公告标题里的全部名称（单个字符串等价于只有一本）；
    ref_name 是用户**按名称**查询时给的名字 —— 只有它能指明多本书里的哪一本。
    ref_name 为空（按编号/公告ID/URL 查询）而标题含多本书时，报 33 交人，
    不用 doc_name 兜底。
    """
    if any(w in (announcement_title or "") for w in NOT_APPLICABLE_WORDS):
        return Pick(Verdict.NOT_APPLICABLE, candidates=tuple(atts), reason="英文版公告，不是官方中文正文")

    if not atts:
        return Pick(Verdict.NO_ATTACHMENT, reason="公告页没有附件")

    alive = [a for a in atts if not any(w in a.label for w in DEPRECATED_WORDS)]
    if not alive:
        return Pick(Verdict.NO_ATTACHMENT, candidates=tuple(atts), reason="附件均为废止条文类附属文件")

    names = _book_names(target_names, announcement_title)
    multi = len(names) > 1

    target = ""
    if ref_name:
        chosen, matched = match_name(names, ref_name)
        if chosen:
            target = chosen
        elif len(matched) > 1:
            # 用户名称同时对上多本：多本公告下没有依据挑
            return _ambiguous(
                alive, names,
                f"用户名称同时命中 {len(matched)} 本规范（{'、'.join(matched)}），无法锁定",
            )
        elif multi:
            # 用户名称对不上标题里的任何一本：多本公告下没有依据猜
            return _ambiguous(alive, names, "公告标题含多本规范，用户名称与其中任何一本都不吻合，无法锁定")
        elif names:
            # 单本公告：保持原行为（名称不完全匹配也可以靠"唯一候选"接受）
            target = names[0]
    elif multi:
        # 按编号/公告ID/URL 查询：标题含多本又没有目标名称时，页面上任何附件
        # （哪怕只有一份）都可能是另一本书的正文 —— 一律 33 交人。
        return _ambiguous(
            alive, names,
            f"公告标题含 {len(names)} 本规范（{'、'.join(names)}），按编号/ID 无法唯一映射到一本",
        )
    elif names:
        target = names[0]

    if target:
        exact = [a for a in alive if target in _norm(a.label)]
        if len(exact) == 1:
            return Pick(
                Verdict.OK, choice=exact[0], candidates=(exact[0],),
                reason="名称完全匹配", book_names=names,
            )
        if len(exact) > 1:
            pdfs = [a for a in exact if _is_pdf(a)]
            if len(pdfs) == 1:
                return Pick(
                    Verdict.OK, choice=pdfs[0], candidates=tuple(exact),
                    reason="名称匹配且唯一 PDF", book_names=names,
                )
        if multi:
            # 多本公告 + 已指定某一本：标签对不上时不许靠"唯一候选/唯一 PDF"
            # 改拿另一本 —— 那是 P1-3 的另一种形态。
            return _ambiguous(alive, names, "多本公告下无法把指定名称映射到唯一附件")

    # 候选只剩一个时不存在"选错"，名称不完全匹配只作提示：多标准合并公告的附件名
    # 常被截断（实测「城市给水工程项目建设」vs 标准名「城市给水工程项目建设标准」）。
    if len(alive) == 1:
        reason = "唯一候选附件" if target else "唯一附件"
        if target and target not in _norm(alive[0].label):
            reason = "唯一候选附件（名称不完全匹配，已按数量唯一性接受）"
        return Pick(Verdict.OK, choice=alive[0], candidates=(alive[0],), reason=reason, book_names=names)

    pdfs = [a for a in alive if _is_pdf(a)]
    if len(pdfs) == 1:
        return Pick(
            Verdict.OK, choice=pdfs[0], candidates=(pdfs[0],),
            reason="唯一 PDF 附件", book_names=names,
        )

    return _ambiguous(alive, names, "多个候选且名称无法锁定，需人工指定（工具不代猜）")
