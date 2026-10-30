"""引用串 —— 本产品在监理场景里真正的价值点。

输出必须能被直接粘进监理通知单 / 旁站记录 / 月报。因此：
  · 编号未核实时不允许静默输出一个"看起来对"的串，必须带 warnings
  · 条款号要做规范化（"第5.2.1条" / "5.2.1条" / "5.2.1" 同一结果）
"""

from __future__ import annotations

import re

_CLAUSE_RE = re.compile(r"^(?:第)?\s*(\d+(?:\.\d+)*)\s*(?:条)?$")


def normalize_clause(raw: str | None) -> str | None:
    """'5.2.1' / '第5.2.1条' / '第 5.2.1 条' → '5.2.1'；非法返回 None。"""
    if not raw:
        return None
    m = _CLAUSE_RE.match(raw.strip())
    return m.group(1) if m else None


def build_citation(
    name: str,
    code_display: str | None,
    clause: str | None = None,
    *,
    confidence: str = "parsed",
) -> tuple[str, list[str]]:
    """生成引用串，返回 (引用串, 警告列表)。

    警告不是装饰：agent 必须据 warnings 决定要不要人工复核。
    """
    warnings: list[str] = []
    # 中文排版：书名号与编号紧贴，不插空格（《建筑防火通用规范》GB 55037-2022）
    text = f"《{name}》" if name else ""

    if code_display:
        text += code_display
    else:
        warnings.append("编号未核实：该公告正文未解析出标准编号，引用串缺少编号")

    if confidence == "ambiguous":
        warnings.append("编号存在歧义：同页出现多个版本，未确认哪一个为现行版")

    if clause:
        text = f"{text} 第{clause}条"
    return text.strip(), warnings
