"""guiju 命令行入口：参数解析、全局开关、错误 -> 退出码的唯一投影点。

三条面向 agent 的规矩：
  1. --json 时 stdout 只有 JSON，日志一律 stderr
  2. 退出码语义化（见 errors.EXIT_TABLE），agent 可据此自主决策
  3. 全程无交互，绝不出现 input() / 确认提示
"""

from __future__ import annotations

import argparse
import sys

from .. import autosync
from ..errors import EXIT_OK, EXIT_TABLE, GuijuError
from ..store.sqlite import Store
from . import render
from .commands import (
    cite,
    mcp,
    pull,
    search,
    show,
    status,
    sync,
    version,
    which,
)

COMMANDS = (sync, search, which, show, pull, cite, status, version, mcp)


def _exit_epilog() -> str:
    lines = ["退出码（agent 据此决策，不要重试 40）:"]
    for code, (meaning, action) in sorted(EXIT_TABLE.items()):
        lines.append(f"  {code:>3}  {meaning} —— {action}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    # default=SUPPRESS 是关键：否则子命令的默认值会覆盖根命令已解析到的 --json
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="stdout 只输出 JSON（agent 主路径）")
    common.add_argument("--db", default=argparse.SUPPRESS, help="指定目录库路径")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="不输出进度日志")

    parser = argparse.ArgumentParser(
        prog="guiju",
        parents=[common],
        description="规矩 guiju —— 以规范为准绳，让依据随手可取。工程建设规范采集工具。",
        epilog=_exit_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="store_true", help="显示版本与目录台账")
    sub = parser.add_subparsers(dest="cmd")
    for module in COMMANDS:
        module.add_parser(sub, parents=[common])
    return parser


def _fail(exc: GuijuError) -> int:
    payload = exc.payload()
    if render.is_json():
        render.emit(payload)
    else:
        render.warn(f"{payload['error']}")
        if payload.get("hint"):
            render.warn(f"  下一步：{payload['hint']}")
        for cand in payload.get("candidates", []) or []:
            label = cand.get("code") or cand.get("label") or cand.get("key")
            render.warn(f"  候选：{label}")
    return exc.exit


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    render.configure(as_json=bool(getattr(args, "json", False)),
                     quiet=bool(getattr(args, "quiet", False)))

    try:
        store = Store(getattr(args, "db", None))
    except GuijuError as exc:
        return _fail(exc)

    if store.legacy_backup is not None:
        render.warn(f"检测到旧版数据库，已备份为 {store.legacy_backup} 并按 schema v2 重建")

    try:
        if getattr(args, "version", False):
            return version.run(store, args)
        if not getattr(args, "cmd", False):
            parser.print_help()
            return EXIT_OK
        # 进程内目录保鲜：过期就顺手增量刷新，不依赖设备级定时任务（见 ADR 0003）。
        # 刷新状态会注入到每份 JSON 结果，agent 不必猜"结果是不是旧的"。
        catalog = autosync.evaluate(
            store,
            command=args.cmd,
            offline=bool(getattr(args, "offline", False)),
            notify=render.note,
        )
        render.set_catalog(catalog)
        if catalog.get("message"):
            render.note(catalog["message"])
        return int(args.func(store, args))
    except GuijuError as exc:
        return _fail(exc)
    except KeyboardInterrupt:
        render.warn("已中断")
        return 130
    except Exception as exc:
        import traceback

        from ..errors import EXIT_INTERNAL

        traceback.print_exc(file=sys.stderr)
        render.emit(
            {"ok": False, "exit": EXIT_INTERNAL, "reason": "internal",
             "error": f"{type(exc).__name__}: {exc}", "hint": "这是程序缺陷，请带上 stderr 的 traceback 上报"}
        )
        return EXIT_INTERNAL
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
