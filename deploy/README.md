# hk 部署

目标：hk 上运行合并后的 guiju MCP 服务；本项目不依赖 NAS，NAS 的 spec-currency 已在切换确认后退役（历史事实）。

## 当前服务形态

- 代码目录：/opt/guiju（只复制 guiju/ 包；不复制测试、仓库历史）
- 数据目录：/var/lib/guiju
- Python：系统 /usr/bin/python3（Debian 12，3.11；项目要求 >=3.10）
- 内部端口：127.0.0.1:18083（只允许本机 Caddy 访问）
- HTTP 安全：POST /mcp 只接受 Content-Type: application/json；Host/Origin 按 allowlist 校验（默认绑定地址与 localhost 等价形式，防 DNS rebinding）。公网入口必须显式配置 GUIJU_MCP_ALLOWED_HOSTS / GUIJU_MCP_ALLOWED_ORIGINS（deploy/guiju-mcp.service 已含 guiju.守夜人.中国 及 punycode 形式），否则 Caddy 保留的公网 Host 会被 403 拒绝。
- 对外端点：https://guiju.守夜人.中国/mcp（Caddy HTTPS + Bearer）
- 健康检查：GET /health（同样需要 Bearer）
- 后端：NativeCurrency，缓存目录 /var/lib/guiju/currency-cache
- 首次部署预热 GB/JGJ/CJJ 共 28 个编号：其中 25 个从官方 csres 原始页面（经 ctyun 出口）生成，3 个使用旧服务当日缓存记录；
  使用产品自己的 schema/TTL，不是旧 Go 缓存文件继承。缓存过期后会按 stale 规则降级并明示。

## 安装

    ssh hk 'mkdir -p /opt/guiju /var/lib/guiju /var/lib/guiju/mcp'
    tar --exclude='__pycache__' --exclude='*.pyc' -C . -cf - guiju \
      | ssh hk 'tar -C /opt/guiju -xf -'
    scp deploy/guiju-mcp.service hk:/etc/systemd/system/guiju-mcp.service
    ssh hk 'systemctl daemon-reload && systemctl enable --now guiju-mcp'

## 验证

    ssh hk 'systemctl status --no-pager guiju-mcp'
    ssh hk 'curl -fsS http://127.0.0.1:18083/health'
    # MCP 查询（JSON-RPC）
    ssh hk 'curl -fsS -X POST http://127.0.0.1:18083/mcp -H "Content-Type: application/json" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"query\",\"arguments\":{\"code\":\"DG/TJ08-2007\"}}}"'

## 边界

- 服务只读规范元数据/正文索引，本项目不把 NAS 当依赖；NAS spec-currency 已于 2026-11-01 所有者授权后停用（历史事实；cron 自启动移除，文件保留可回退）。任何 NAS 操作须遵守 kb-system 的 ADR0048 外部冻结约束。
- 18083 已仅绑定 127.0.0.1；公网只暴露 Caddy 的 80/443，MCP 路径要求 `Authorization: Bearer <token>`。
- 公网 Caddy 保留原始 Host 转发到 127.0.0.1:18083：GUIJU_MCP_ALLOWED_HOSTS / GUIJU_MCP_ALLOWED_ORIGINS 必须包含公网域名（Unicode 与 punycode 都写）；命令行可用 `--allowed-host` / `--allowed-origin`（可重复）追加。
- 消费者已于 2026-11-01 切换：desktop/web 的 guiju MCP URL 指向 https://guiju.守夜人.中国/mcp，并带 Bearer header；desktop 的 MCP id/serverName 已改为 mcp-guiju/guiju。
- 切换后已做 hk health + GB/JGJ/CJJ/JTG/DB 冒烟；重启 DSH 后以 hk 端点生效。
- 本服务不读取 ~/.tencentcloud 凭据，不做正文下载。
