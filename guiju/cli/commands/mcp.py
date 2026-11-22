"""mcp —— guiju 的 MCP 服务面（stdio 或 HTTP）。"""

from __future__ import annotations

import argparse

from ... import mcp as mcp_service
from ...store.sqlite import Store


def run(store: Store, args: argparse.Namespace) -> int:
    # --db 必须贯通到 MCP：announcement 读的应是同一个目录库，而不是默认 GUIJU_HOME。
    server = mcp_service.McpServer(mcp_service.default_backend(), store_path=store.path)
    if args.http:
        return mcp_service.serve_http(
            server,
            args.host,
            args.port,
            args.path,
            allowed_hosts=getattr(args, "allowed_host", None),
            allowed_origins=getattr(args, "allowed_origin", None),
        )
    return mcp_service.serve_stdio(server)


def add_parser(sub, parents) -> None:
    p = sub.add_parser("mcp", parents=parents, help="MCP 服务面：stdio 或 HTTP（query/verify/batch/announcement）")
    p.add_argument("--http", action="store_true", help="启动 HTTP 端点；默认 stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    p.add_argument("--port", type=int, default=18083, help="HTTP 端口")
    p.add_argument("--path", default="/mcp", help="HTTP MCP 路径")
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
