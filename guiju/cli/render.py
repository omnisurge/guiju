"""输出投影：JSON 走 stdout，人类日志走 stderr。

这是「agent 优先」契约的物理实现：--json 时 stdout 只有 JSON，可以 jq 直接吃。
"""

from __future__ import annotations

import json
import sys

_state = {"json": False, "quiet": False}


def configure(*, as_json: bool, quiet: bool) -> None:
    _state["json"] = as_json
    _state["quiet"] = quiet


def is_json() -> bool:
    return bool(_state["json"])


def emit(payload: object) -> None:
    """结构化结果：只在 --json 时输出到 stdout。"""
    if _state["json"]:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def out(text: str) -> None:
    """人读结果：--json 时丢弃（避免污染 stdout）。"""
    if not _state["json"]:
        print(text)


def note(message: str) -> None:
    """日志与进度：永远走 stderr。"""
    if not _state["quiet"]:
        print(message, file=sys.stderr, flush=True)


def warn(message: str) -> None:
    print(f"! {message}", file=sys.stderr, flush=True)
