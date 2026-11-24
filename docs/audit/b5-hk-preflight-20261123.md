# hk guiju-mcp 部署前只读核查（2026-11-23）

> 目的：B5 / 1.2.0 稳定版发布前的 hk 现状核查。**只读，未改 hk 任何文件/服务。**
> 关联：[b5-release-gate-20261123.md](b5-release-gate-20261123.md)、[deploy/README.md](../../deploy/README.md)。

## 1. 现场事实（2026-11-23 只读）

- host：`C202608051699087`；`guiju-mcp.service` 状态：**active**（PID 585888，启动于 2026-11-22 19:42:47 CST）。
- ExecStart：`/usr/bin/python3 -m guiju mcp --http --host 127.0.0.1 --port 18083 --path /mcp`，WorkingDirectory=`/opt/guiju`。
- 当前 unit：`/etc/systemd/system/guiju-mcp.service`，SHA-256 `c7c9e43d1b5834b1f18e7d5df215ea1183e65cd7697874fcf42bb049f775c3df`。
- 当前 unit 的 systemd 属性：`User=` 空、`Group=` 空、`ProtectSystem=no`、`ReadWritePaths=` 空；`NoNewPrivileges=yes`。
- `guiju` 用户不存在（`id: ‘guiju’: no such user`）；`/var/lib/guiju`、`/var/lib/guiju/mcp`、`/opt/guiju` 均为 `root:root 755`。
- 现有 unit 备份：`/opt/guiju/guiju-mcp.service.bak-20261122-194247`，SHA-256 `a22e685712e1e936c7ac8bc2d5836c54fd24f17f5a4a8287ca9132370619875a`。
- 本地强化 unit：`deploy/guiju-mcp.service`，SHA-256 `187d3bb33b7b2be72fb06b728376e0120214b362d0a82abd7c816e2bfaaefb73`。
- 本机 health：`curl -fsS http://127.0.0.1:18083/health` → `{"ok":true}`。

## 2. 结论

当前 hk 生产进程**仍在旧 unit 下运行**：未以 `guiju` 专用用户运行，缺少 `ProtectSystem=strict`、`ReadWritePaths=/var/lib/guiju` 等边界。因此 B4 的强化 unit 只在仓库/静态检查层面通过，**尚未落实到 hk**；B5 的 “hk systemd 重启/健康检查/回滚” 不能标记完成。

## 3. 所有者批准后的部署/回滚步骤（未执行）

1. 备份当前 unit（已有备份可复用）和 `/var/lib/guiju` 数据；记录 `systemctl show` 现状。
2. 创建 `guiju` 系统组/用户，`/opt/guiju` 保持 root 只读，`/var/lib/guiju` 及 `mcp`/`currency-cache` 改 `guiju:guiju 0750`。
3. 复制 `deploy/guiju-mcp.service` 到 `/etc/systemd/system/guiju-mcp.service`；`systemctl daemon-reload && systemctl enable guiju-mcp && systemctl restart guiju-mcp`。
4. 验证：`systemctl show -p User,Group,ProtectSystem,ReadWritePaths` 应为 `User=guiju Group=guiju ProtectSystem=strict ReadWritePaths=/var/lib/guiju`；`/health` 200；MCP `query GB 55037-2022` 返回 current。
5. 失败回滚：恢复 unit 备份 → `daemon-reload` → `restart` → health/MCP 复核；不得回退数据目录权限前不确认。

## 4. 边界

- 本次只读核查未改 hk unit、未创建用户、未重启服务、未改 `/var/lib/guiju`。
- 未触碰 NAS/kb-system；NAS 仍受 ADR0048 冻结。
