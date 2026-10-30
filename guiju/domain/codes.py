"""标准编号：归一化与角色判定。

这是全仓最容易出错、后果最严重的逻辑。实测（2026-10-30）四个公告页同日出现
新旧两个编号：JGJ 147-2016/2004、CJ/T 133-2012/2007、GB/T 50362-2022/2005；
局部修订公告原文写「标准编号修改为 JGJ/T450-2018」。

若不分角色，取"第一个"就有一半概率引用**已被代替的版本** —— 在监理文书里这是
事故，不是 bug。因此本模块的契约是：**没有原文证据，就不给出 primary**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: 长前缀必须排在短前缀前面，否则 GB/T 会被 GB 抢先匹配
_CODE_RE = re.compile(
    r"(GB/T|GB|JGJ/T|JGJ|CJJ/T|CJJ|CJ/T|DG/TJ|DGJ|DB\d{2}/T|T/CECS)"
    r"\s*(\d{2,6})\s*[-—–－]\s*(\d{4})"
)
_SENTENCE = re.compile(r"[。；;\n]+")
_TAG = re.compile(r"<[^>]+>")

#: 「编号被改写」的措辞（局部修订公告的典型句式）
_REVISION_WORDS = ("修改为", "修订为", "编号改为", "编号修改为")
#: 「这个编号已被替代」的措辞
_SUPERSEDE_WORDS = ("代替", "替代", "废止", "取消")
#: 「这个编号是本次发布的」的措辞
_PUBLISH_WORDS = ("发布", "批准发布", "印发", "施行", "实施")

_EVIDENCE_MAX = 100


class Role(StrEnum):
    PRIMARY = "primary"                    # 本次公告发布/生效的那个版本
    TARGET_OF_REVISION = "target_of_revision"  # 局部修订后改写的新编号
    SUPERSEDED_REF = "superseded_ref"      # 被代替/废止的旧编号
    MENTIONED = "mentioned"                # 提到了但判不出身份


class Confidence(StrEnum):
    PARSED = "parsed"        # 规则明确命中
    AMBIGUOUS = "ambiguous"  # 有多个候选且无决定性证据 —— 必须让人选
    NONE = "none"            # 正文里根本没有编号


@dataclass(frozen=True)
class Mention:
    code_key: str
    code_display: str
    role: Role
    evidence: str


@dataclass(frozen=True)
class Codes:
    mentions: tuple[Mention, ...] = ()
    primary_key: str | None = None
    primary_display: str | None = None
    confidence: Confidence = Confidence.NONE

    @property
    def superseded(self) -> tuple[Mention, ...]:
        return tuple(m for m in self.mentions if m.role is Role.SUPERSEDED_REF)

    @property
    def candidates(self) -> list[dict]:
        return [
            {"code": m.code_display, "key": m.code_key, "role": m.role.value, "evidence": m.evidence}
            for m in self.mentions
        ]


def html_to_text(html: str) -> str:
    """把详情页压成纯文本；只保留句子结构，供角色判定使用。"""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = _TAG.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"[ \t\u3000]+", " ", text)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def normalize_key(user_input: str) -> str:
    """用户输入 → 归一化 key：GB 55037-2022 / gb55037－2022 → GB55037-2022。"""
    s = user_input.strip().upper().replace("－", "-").replace("—", "-").replace("–", "-")
    return re.sub(r"\s+", "", s)


def _make_display(prefix: str, num: str, year: str) -> str:
    return f"{prefix.upper()} {num}-{year}"


def _evidence_of(sentence: str) -> str:
    s = sentence.strip()
    return s if len(s) <= _EVIDENCE_MAX else s[:100] + "…"


def classify(text: str, doc_name: str | None = None) -> Codes:
    """从详情页正文判定编号及其角色。

    判定优先级：
      1. 「编号修改为 X」 → X = target_of_revision，可直接作为 primary
      2. 「发布《…》… X」 → primary
      3. 只有一个编号 → primary（parsed）
      4. 多个编号无决定性证据 → 取年份最新者，但 confidence=ambiguous，禁止据此写引用
    """
    if "<" in text:  # 容错：允许直接传详情页 HTML
        text = html_to_text(text)
    sentences = split_sentences(text)
    if not sentences:
        return Codes()

    seen: dict[str, Mention] = {}
    _ROLE_RANK = {
        Role.TARGET_OF_REVISION: 4,
        Role.PRIMARY: 3,
        Role.SUPERSEDED_REF: 2,
        Role.MENTIONED: 1,
    }

    for sentence in sentences:
        hits = list(_CODE_RE.finditer(sentence))
        if not hits:
            continue
        for hit in hits:
            display = _make_display(hit.group(1), hit.group(2), hit.group(3))
            key = normalize_key(display)
            role = _role_for(sentence, hit.start(), doc_name)
            prior = seen.get(key)
            if prior is None or _ROLE_RANK[role] > _ROLE_RANK[prior.role]:
                seen[key] = Mention(key, display, role, _evidence_of(sentence))

    if not seen:
        return Codes()

    primary = _choose_primary(tuple(seen.values()))
    mentions = tuple(
        sorted(_demote_older(primary, seen.values()), key=lambda m: (m.code_key, m.role.value))
    )
    if primary is None:
        return Codes(mentions=mentions, confidence=Confidence.AMBIGUOUS)
    decisive = primary.role in (Role.TARGET_OF_REVISION, Role.PRIMARY)
    return Codes(
        mentions=mentions,
        primary_key=primary.code_key,
        primary_display=primary.code_display,
        confidence=Confidence.PARSED if decisive else Confidence.AMBIGUOUS,
    )


def _role_for(sentence: str, pos: int, doc_name: str | None) -> Role:
    """按中文公告的真实句式判定角色。

    方向性很关键：「X 代替 Y」里 X 是新的、Y 是旧的；而「Y 同时废止」直接判旧。
    历史实现只看关键词不看位置，导致被代替的旧编号被判成 primary —— 这正是
    「通知单引用错版本」事故的成因。
    """
    head = sentence[:pos]
    tail = sentence[pos:]

    if any(w in head for w in _REVISION_WORDS):
        return Role.TARGET_OF_REVISION
    if any(w in sentence for w in ("废止", "作废")):
        return Role.SUPERSEDED_REF
    if any(w in tail for w in _SUPERSEDE_WORDS):      # 「X 代替 Y」的 Y
        return Role.SUPERSEDED_REF
    if any(w in head for w in _SUPERSEDE_WORDS):      # 「X 代替 Y」的 X
        return Role.PRIMARY
    if any(w in sentence for w in _PUBLISH_WORDS):
        return Role.PRIMARY
    if doc_name and _norm_name(doc_name) and _norm_name(doc_name) in _norm_name(sentence):
        return Role.PRIMARY
    return Role.MENTIONED


def _base_number(code_key: str) -> str:
    """GB/T50362-2022 → 50362：同一标准的版本之间共享这个数。"""
    head = code_key.rsplit("-", 1)[0]
    return re.sub(r"^[A-Z/]+", "", head)


def _demote_older(primary: Mention | None, mentions) -> list[Mention]:
    """把「与 primary 同号但不同版本」的条目降级为 superseded_ref。

    实测场景：局部修订公告同页出现 JGJ450-2018（原版）与 JGJ/T450-2018（修订后）。
    两者若是同一标准的不同版本，旧的那个就不是 primary —— 引用它等于引用废止版。
    """
    if primary is None:
        return list(mentions)
    base = _base_number(primary.code_key)
    out: list[Mention] = []
    for m in mentions:
        if m.code_key != primary.code_key and m.role is not Role.SUPERSEDED_REF \
                and _base_number(m.code_key) == base:
            m = Mention(m.code_key, m.code_display, Role.SUPERSEDED_REF, m.evidence)
        out.append(m)
    return out


def _norm_name(s: str) -> str:
    return re.sub(r"[《》〈〉\s（）()【】]", "", s or "")


def _choose_primary(mentions: tuple[Mention, ...]) -> Mention | None:
    targets = [m for m in mentions if m.role is Role.TARGET_OF_REVISION]
    if targets:
        return max(targets, key=lambda m: m.code_key)
    primaries = [m for m in mentions if m.role is Role.PRIMARY]
    if len(primaries) == 1:
        return primaries[0]
    if not primaries:
        alive = [m for m in mentions if m.role is not Role.SUPERSEDED_REF]
        if len(alive) == 1:
            return alive[0]
        pool = alive or list(mentions)
        return max(pool, key=lambda m: _year(m.code_key))
    return max(primaries, key=lambda m: _year(m.code_key))


def _year(code_key: str) -> str:
    parts = code_key.rsplit("-", 1)
    return parts[-1] if len(parts) == 2 else "0"
