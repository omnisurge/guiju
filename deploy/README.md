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

    # 服务以 guiju 专用用户/组运行；数据目录只给它写权限，代码目录保持 root 只读。
    ssh hk 'getent group guiju >/dev/null || groupadd --system guiju'
    ssh hk 'id -u guiju >/dev/null 2>&1 || useradd --system --gid guiju --home-dir /var/lib/guiju \
      --shell /usr/sbin/nologin guiju'
    ssh hk 'install -d -o root -g root -m 0755 /opt/guiju'
    tar --exclude='__pycache__' --exclude='*.pyc' -C . -cf - guiju \
      | ssh hk 'tar -C /opt/guiju -xf -'
    ssh hk 'install -d -o guiju -g guiju -m 0750 /var/lib/guiju /var/lib/guiju/currency-cache'
    ssh hk 'chown -R guiju:guiju /var/lib/guiju'
    scp deploy/guiju-mcp.service hk:/etc/systemd/system/guiju-mcp.service
    # enable --now 对已在运行的旧 unit 不会重启；必须 restart 才让 User/Group/ProtectSystem 生效。
    ssh hk 'systemctl daemon-reload && systemctl enable guiju-mcp && systemctl restart guiju-mcp'

## 验证

    ssh hk 'systemctl status --no-pager guiju-mcp'
    ssh hk 'systemctl show -p User,Group,ProtectSystem guiju-mcp'
    ssh hk 'curl -fsS http://127.0.0.1:18083/health'
    # MCP 查询（JSON-RPC）
    ssh hk 'curl -fsS -X POST http://127.0.0.1:18083/mcp -H "Content-Type: application/json" \
      -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"query\",\"arguments\":{\"code\":\"DG/TJ08-2007\"}}}"'

## 边界

- 服务只读规范元数据/正文索引，本项目不把 NAS 当依赖；NAS spec-currency 已于 2026-11-01 所有者授权后停用（历史事实；cron 自启动移除，文件保留可回退）。任何 NAS 操作须遵守 kb-system 的 ADR0048 外部冻结约束。
- 18083 已仅绑定 127.0.0.1；公网只暴露 Caddy 的 80/443，MCP 路径要求 `Authorization: Bearer <token>`。
- 非回环绑定必须显式传 `--allow-remote` 并设置 `GUIJU_MCP_BEARER_TOKEN`，否则进程拒绝启动；Caddy 后方的 127.0.0.1 监听不需要该 token（外层 Bearer 由 Caddy 负责）。
- 公网 Caddy 保留原始 Host 转发到 127.0.0.1:18083：GUIJU_MCP_ALLOWED_HOSTS / GUIJU_MCP_ALLOWED_ORIGINS 必须包含公网域名（Unicode 与 punycode 都写）；命令行可用 `--allowed-host` / `--allowed-origin`（可重复）追加。
- 消费者已于 2026-11-01 切换：desktop/web 的 guiju MCP URL 指向 https://guiju.守夜人.中国/mcp，并带 Bearer header；desktop 的 MCP id/serverName 已改为 mcp-guiju/guiju。
- 切换后已做 hk health + GB/JGJ/CJJ/JTG/DB 冒烟；重启 DSH 后以 hk 端点生效。
- 本服务不读取 ~/.tencentcloud 凭据，不做正文下载；`batch` 仅接受内联 `codes[]`（1.3.0 起已移除服务端文件路径读写）；1.4.0 新增只读 `atlas_search`（只读 `/var/lib/guiju/atlas-catalog.json`，不下载图集 PDF；升级部署时可用 `GUIJU_HOME=/var/lib/guiju guiju atlas sync` 预热索引）。
