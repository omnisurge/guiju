"""mcp —— guiju 的 MCP 服务面（stdio 或 HTTP）。"""

from __future__ import annotations

import argparse

from ... import mcp as mcp_service
from ...store.sqlite import Store


def run(store: Store, args: argparse.Namespace) -> int:
    server = mcp_service.McpServer(mcp_service.default_backend())
    if args.http:
        return mcp_service.serve_http(server, args.host, args.port, args.path)
    return mcp_service.serve_stdio(server)


def add_parser(sub, parents) -> None:
    p = sub.add_parser("mcp", parents=parents, help="MCP 服务面：stdio 或 HTTP（query/verify/batch）")
    p.add_argument("--http", action="store_true", help="启动 HTTP 端点；默认 stdio")
    p.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    p.add_argument("--port", type=int, default=18083, help="HTTP 端口")
    p.add_argument("--path", default="/mcp", help="HTTP MCP 路径")
    p.set_defaults(func=run)
