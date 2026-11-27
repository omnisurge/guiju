"""agent —— 使用态规范契约查看与跨 Harness Skill 分发（不触碰目录库）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ... import report as render
from ...application.agent import (
    install_agent_skill,
    show_agent_asset,
    status_agent_skills,
    uninstall_agent_skill,
)
from ...errors import EXIT_OK
from ...store.agent_receipts import AgentReceiptStore
from ...store.sqlite import default_home


def _receipt_store() -> AgentReceiptStore:
    return AgentReceiptStore(default_home() / "agent-receipts.json")


def _add_location_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target",
        default=None,
        help="目标工具命名空间：agents（默认，写入 .agents/skills/guiju-standards）或 claude（.claude/skills/guiju-standards）",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="安装作用域：project（默认，当前工作目录）或 user（用户主目录）",
    )
    parser.add_argument(
        "--dir",
        default=None,
        metavar="PATH",
        help="指定 skill 父根目录（写入 <PATH>/guiju-standards；与 --target / --scope 互斥）",
    )


def _run_show(_store: object, args: argparse.Namespace) -> int:
    payload = show_agent_asset(kind=args.kind)
    render.emit(payload)
    if not render.is_json():
        sys.stdout.write(payload["content"])
        sys.stdout.flush()
    return EXIT_OK


def _run_install(_store: object, args: argparse.Namespace) -> int:
    payload = install_agent_skill(
        _receipt_store(),
        cwd=Path.cwd(),
        user_home=Path.home(),
        target=args.target,
        scope=args.scope,
        dir_path=args.dir,
        force=bool(args.force),
    )
    render.emit(payload)
    if not render.is_json():
        render.out(f"{payload['action']}: {payload['path']} (version={payload['package_version']})")
        if payload["removed_extra_files"]:
            render.out(f"已清理额外文件：{', '.join(payload['removed_extra_files'])}")
        if payload["backed_up_receipt"]:
            render.out(f"已备份损坏收据：{payload['backed_up_receipt']}")
    return EXIT_OK


def _run_uninstall(_store: object, args: argparse.Namespace) -> int:
    payload = uninstall_agent_skill(
        _receipt_store(),
        cwd=Path.cwd(),
        user_home=Path.home(),
        target=args.target,
        scope=args.scope,
        dir_path=args.dir,
        force=bool(args.force),
    )
    render.emit(payload)
    if not render.is_json():
        render.out(f"uninstalled: {payload['path']}")
        if payload["removed_files"]:
            render.out(f"已删除受管文件：{', '.join(payload['removed_files'])}")
        if payload["preserved_files"]:
            render.out(f"已保留额外文件：{', '.join(payload['preserved_files'])}")
    return EXIT_OK


def _run_status(_store: object, args: argparse.Namespace) -> int:
    payload = status_agent_skills(
        _receipt_store(),
        cwd=Path.cwd(),
        user_home=Path.home(),
        target=args.target,
        scope=args.scope,
        dir_path=args.dir,
    )
    render.emit(payload)
    if not render.is_json():
        if not payload["entries"]:
            render.out(f"尚无受管技能安装记录（收据：{payload['receipt_path']}）")
        else:
            for item in payload["entries"]:
                ver = item["receipt_version"] or "-"
                render.out(
                    f"[{item['state']}] {item['path']} (target={item['target']}, scope={item['scope']}, receipt={ver})"
                )
    return EXIT_OK


def _run_root(_store: object, _args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if render.is_json():
        render.emit({"ok": True, "subcommands": ["show", "install", "uninstall", "status"]})
    else:
        parser.print_help()
    return EXIT_OK


def add_parser(sub, parents) -> None:
    p = sub.add_parser("agent", parents=parents, help="管理使用态 Agent 使用契约与跨 Harness Skill 分发")
    p.set_defaults(func=lambda store, args, _p=p: _run_root(store, args, _p))
    sub_agent = p.add_subparsers(dest="agent_cmd")

    p_show = sub_agent.add_parser("show", parents=parents, help="只读打印使用契约原文或完整 SKILL.md")
    p_show.add_argument("--kind", default=None, help="显示内容类型：contract（默认）或 skill")
    p_show.set_defaults(func=_run_show)

    p_install = sub_agent.add_parser("install", parents=parents, help="安装、平滑升级或认领标准 Agent Skill 目录")
    _add_location_args(p_install)
    p_install.add_argument(
        "--force",
        action="store_true",
        help="覆盖非受管冲突/本地修改、清理目录内多余文件，或自动备份损坏收据后重建",
    )
    p_install.set_defaults(func=_run_install)

    p_uninstall = sub_agent.add_parser("uninstall", parents=parents, help="卸载受管 Skill 目录并清理对应收据")
    _add_location_args(p_uninstall)
    p_uninstall.add_argument(
        "--force",
        action="store_true",
        help="仅针对已登记但被修改的目录：删除受管文件并保留额外文件（未受管目录与符号链接始终拒绝）",
    )
    p_uninstall.set_defaults(func=_run_uninstall)

    p_status = sub_agent.add_parser("status", parents=parents, help="只读检查全部已登记或指定目标 Skill 状态")
    _add_location_args(p_status)
    p_status.set_defaults(func=_run_status)
