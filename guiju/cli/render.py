"""输出投影：JSON 走 stdout，人类日志走 stderr。

这是「agent 优先」契约的物理实现：--json 时 stdout 只有 JSON，可以 jq 直接吃。
"""

from __future__ import annotations

import json
import sys

_state = {"json": False, "quiet": False, "catalog": None}


def configure(*, as_json: bool, quiet: bool) -> None:
    _state["json"] = as_json
    _state["quiet"] = quiet


def is_json() -> bool:
    return bool(_state["json"])


def set_catalog(snapshot: dict | None) -> None:
    """登记本次调用看到的目录状态，由 emit 统一注入，避免各命令各写一套。"""
    _state["catalog"] = snapshot


def emit(payload: object) -> None:
    """结构化结果：只在 --json 时输出到 stdout。

    `catalog` 是**统一注入**的：任何 JSON 结果都带目录新鲜度，
    agent 不必为了"结果是不是旧的"再单独问一次。
    """
    if not _state["json"]:
        return
    if isinstance(payload, dict) and "catalog" not in payload and _state["catalog"]:
        payload = {**payload, "catalog": _state["catalog"]}
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
