"""mcp —— guiju 的 MCP 服务面（stdio 或 HTTP）。"""

from __future__ import annotations

import argparse

from ... import mcp as mcp_service
from ...errors import BadUsage
from ...store.sqlite import Store


def run(store: Store, args: argparse.Namespace) -> int:
    # --db 必须贯通到 MCP：announcement 读的应是同一个目录库，而不是默认 GUIJU_HOME。
    # 入口已有 AppContext 时复用同一 currency，避免命令层再建一份。
    ctx = getattr(args, "app_context", None)
    backend = (
        ctx.currency
        if ctx is not None and getattr(ctx, "currency", None) is not None
        else mcp_service.default_backend()
    )
    server = mcp_service.McpServer(backend, store_path=store.path)
    if args.http:
        try:
            return mcp_service.serve_http(
                server,
                args.host,
                args.port,
                args.path,
                allowed_hosts=getattr(args, "allowed_host", None),
                allowed_origins=getattr(args, "allowed_origin", None),
                allow_remote=bool(getattr(args, "allow_remote", False)),
            )
        except ValueError as exc:
            raise BadUsage(
                str(exc),
                hint="非回环绑定需同时传 --allow-remote 与 GUIJU_MCP_BEARER_TOKEN；本机部署用 127.0.0.1",
            ) from exc
    return mcp_service.serve_stdio(server)


def add_parser(sub, parents) -> None:
    p = sub.add_parser("mcp", parents=parents, help="MCP 服务面：stdio 或 HTTP（query/verify/batch/announcement/atlas_search）")
    p.add_argument("--http", action="store_true", help="启动 HTTP 端点；默认 stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    p.add_argument("--port", type=int, default=18083, help="HTTP 端口")
    p.add_argument("--path", default="/mcp", help="HTTP MCP 路径")
    p.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许绑定非回环地址；必须同时设置 GUIJU_MCP_BEARER_TOKEN，否则拒绝启动",
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="额外允许的 Host（含端口；可重复）；也可用 GUIJU_MCP_ALLOWED_HOSTS（逗号分隔）",
    )
    p.add_argument(
        "--allowed-origin",
        action="append",
        default=None,
        help="额外允许的 Origin（scheme://host[:port]；可重复）；也可用 GUIJU_MCP_ALLOWED_ORIGINS（逗号分隔）",
    )
    p.set_defaults(func=run)
