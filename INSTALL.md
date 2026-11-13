# guiju v1.0.0 安装与自部署

适用：Linux / macOS；Python 3.10+；运行时零第三方依赖。

## 1. 环境

- Python 3.10+（推荐 3.11/3.12/3.13）
- 可写数据目录：默认 ~/.guiju，可用 GUIJU_HOME 覆盖
- 不需要 Node、数据库或 Docker
- MCP 服务需要能启动一个本地 HTTP 端口（默认 18083）

## 2. 本机安装（推荐 wheel）

从发布页下载 wheel：

    https://github.com/omnisurge/guiju/releases/tag/v1.0.0

安装到独立虚拟环境：

    python3 -m venv /opt/guiju-venv
    curl -L -o /tmp/guiju-1.0.0-py3-none-any.whl \
      https://github.com/omnisurge/guiju/releases/download/v1.0.0/guiju-1.0.0-py3-none-any.whl
    /opt/guiju-venv/bin/python -m pip install /tmp/guiju-1.0.0-py3-none-any.whl
    /opt/guiju-venv/bin/guiju --version

如果使用 uv：

    uv tool install /tmp/guiju-1.0.0-py3-none-any.whl
    guiju --version

## 3. 源码安装

    git clone https://github.com/omnisurge/guiju.git
    cd guiju
    python3 -m venv .venv
    .venv/bin/python -m pip install .
    .venv/bin/guiju --version

开发模式可用：

    uv tool install --editable .
    guiju --version

## 4. 第一次运行

    export GUIJU_HOME="$HOME/.guiju"
    guiju sync
    guiju search 防火 --json
    guiju which GB55037-2022 --json
    guiju pull GB55037-2022 --json
    guiju status --json

数据默认是目录库 catalog.db 加正文 library/；GUIJU_HOME 可改位置，--db 可改库路径。

默认时效适配器是原生 native；需要显式切换时可用：

    GUIJU_CURRENCY=native
    GUIJU_CURRENCY=off

cmd:/mcp: 覆盖已随 spec-currency 退役删除（2026-11-13），设置非 native/off 值会报 exit 20。

## 5. MCP 服务自部署

### 5.1 传代码

在仓库根目录执行：

    ssh root@your-server 'mkdir -p /opt/guiju /var/lib/guiju'
    tar --exclude='__pycache__' --exclude='*.pyc' -C . -cf - guiju \
      | ssh root@your-server 'tar -C /opt/guiju -xf -'

### 5.2 安装 systemd 单元

    scp deploy/guiju-mcp.service root@your-server:/etc/systemd/system/guiju-mcp.service
    ssh root@your-server 'systemctl daemon-reload && systemctl enable --now guiju-mcp'

当前单元默认使用 /usr/bin/python3、/opt/guiju、/var/lib/guiju，并只监听 127.0.0.1:18083。
如果服务器路径/用户不同，编辑 unit 中的 WorkingDirectory、Environment、User 和端口。
直接对外提供时请在前面加 Caddy/nginx 反向代理和认证，不要把 18083 绑到 0.0.0.0。

### 5.3 验证

    ssh root@your-server 'systemctl status --no-pager guiju-mcp'
    ssh root@your-server 'curl -fsS http://127.0.0.1:18083/health'
    # 返回 {"ok":true}

MCP 查询示例：

    curl -fsS -X POST http://127.0.0.1:18083/mcp \
      -H 'Content-Type: application/json' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"query","arguments":{"code":"GB50011"}}}'

### 5.4 客户端配置

MCP 客户端配置 streamable HTTP（假设已配置反向代理和 Bearer）：

    name: guiju
    url: https://your-domain.example/mcp
    transport: streamable-http
    headers:
      Authorization: Bearer <token>

## 6. 升级与回退

升级：替换 /opt/guiju/guiju 后执行

    systemctl restart guiju-mcp

回退：保留上一版目录并切换 systemd 的 WorkingDirectory，或恢复上一版发布包。
发布包和源码都以版本 tag 区分，回退不要覆盖数据目录。

## 7. 已知边界

- hk 的 GB/JGJ/CJJ 依赖预热缓存和 TTL；缓存过期后需要重新预热或换出口。
- 服务本身没有认证；unit 默认只监听 127.0.0.1，对外必须放在 Caddy/nginx 的反向代理和访问控制后面。
- 正文下载只允许官方公开 .gov.cn 内容，不提供分发或镜像。
