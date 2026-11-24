# B5 / 1.2.0 稳定版门禁评估（2026-11-23）

> 状态：**B5 已关闭（2026-11-24）。1.2.0 已正式发布（tag `v1.2.0`），hk 已部署强化 systemd unit 与 1.2.0，并完成真实回退/再部署；未访问 NAS。**
> 依据：ADR 0004 第十节、ADR 0005 I4、`docs/remediation-2026-11-18.md` P5。
> 本记录区分“候选已构建/已演练”与“已发版/已部署”；2026-11-24 的发布、hk 部署与真实回退/再部署事实见下文和 [release-1.2.0-20261124.md](release-1.2.0-20261124.md)。

## 1. 前置：B4 已关闭

- reviewer-domain-mcp：**APPROVE**（B4 rev2，2026-11-23）。
- reviewer-cli：**APPROVE**（task-69，2026-11-23）。
- 证据：[b4-application-mcp-20261123.md](b4-application-mcp-20261123.md)；全量 1223×2、ruff、CI、docs-only、离线 e2e 4/4 通过。

## 2. 最终验证矩阵（2026-11-24）

| 门 | 结果 |
|---|---|
| Python 3.13 全量 | **1223/1223 OK**（74.2s） |
| Python 3.10 全量 | **1223/1223 OK**（72.0s） |
| `scripts/ci.sh` | exit 0（含 ruff、全量、docs-only 44、离线 e2e 4/4、入口自检） |
| `ruff check .` | All checks passed |
| `check-release.py --docs-only` | PASS（44 项，`1.2.0` 基线） |
| `check-release.py --artifacts --dist-dir dist --skip-tag` | PASS（58 项） |
| `git diff --check` | 干净 |
| 完整联网 e2e | **39/39 通过，exit 0**（2026-11-24 本机，`python3 scripts/e2e.py --require-currency`）；日志 `/tmp/guiju-e2e-20261124-release.log`，2781 bytes，SHA-256 `e5baf8f1848f3a3d5c0cb1e5211536f67b9cbb4d36f494a3047468da834e331e` |

## 3. 1.2.0 最终产物（发布包）

| 文件 | SHA-256 |
|---|---|
| `guiju-1.2.0-py3-none-any.whl` | `bf94c340ca66135747bbf7e2d33d92fd05d1274d410a0e74948fc0bec5db7b04` |
| `guiju-1.2.0.tar.gz` | `62bc9ec32ca4fff61d56cd9cc1b609f8c0e78a43790a75474ffbe1ea4c6cca86` |

- 版本源 `guiju/_version.py` 为 `1.2.0`；wheel METADATA 为 `Name: guiju` / `Version: 1.2.0`；sdist 199 个条目，含 `guiju/_version.py`，不含 `docs/`。
- `uv build --wheel --sdist --out-dir dist` 重建；`dist/SHA256SUMS` 与 `docs/ACCEPTANCE.md`、发版记录三处哈希一致。
- 临时 venv 安装 wheel 后 `guiju version --json` 返回 `1.2.0` 且 `ok=true`；`guiju --help` 正常。
- 稳定版文档已应用到主工作树：`CHANGELOG.md` / `README.md` / `HANDOFF.md` / `INSTALL.md` / `docs/ACCEPTANCE.md` / `docs/releases/v1.2.0.md` / `docs/audit/release-1.2.0-20261124.md`。
- `docs/ACCEPTANCE.md` 的稳定版段落记录本轮 1223×2、39/39 与上述产物哈希；不把候选包冒充发布物。

## 4. 回退演练（wheel / DB / systemd）

本地演练目录 `/tmp/guiju-rollback-drill-20261123b`：

1. 新建 venv，安装候选 `guiju-1.2.0` wheel；`guiju version --json` → `"version": "1.2.0"`。
2. 在临时 `GUIJU_HOME` 构造 catalog.db，写入 `rollback_drill_marker=before-rollback`，备份 DB。
3. 强制安装上一发布 wheel `guiju-1.2.0a4` 模拟回退；恢复 DB 备份。
4. `guiju version --json` → `"version": "1.2.0a4"`；读取 `rollback_drill_marker` → `before-rollback`，DB 完整。
5. **hk 真实回滚演练（2026-11-24 22:25 CST）**：先将 hk 从旧 unit/1.2.0a4 部署为强化 unit/1.2.0，记录 `User=guiju`、`Group=guiju`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/guiju`、health 200、MCP query `GB 55037-2022` current；再 stop、恢复旧包 1.2.0a4 与旧 unit，restart 后实测 `1.2.0a4`、`User=`空、`ProtectSystem=no`、health 200；最后恢复 1.2.0 包与强化 unit，restart 后实测 `1.2.0`、`User=guiju`、`ProtectSystem=strict`、health 200、同一 MCP query current。
6. hk 备份：`/opt/guiju/backup-1.2.0a4-20261124-222402/`（旧 `guiju/`、`dist-info-1.2.0a4`、旧 unit），另有 `/opt/guiju/guiju.prev-1.2.0a4-20261124-222402` 与 `/opt/guiju/guiju-mcp.service.bak-20261124-222402`；强化 unit SHA-256 `187d3bb33b7b2be72fb06b728376e0120214b362d0a82abd7c816e2bfaaefb73`。

结论：本地 wheel/DB 回退与 hk 真实 systemd 重启/回退/再部署均通过；当前 hk 已回到强化 1.2.0 的最终状态。

## 5. P2-4 / P2-5

- P2-4（csres 明文回退不落缓存）：所有者 2026-11-24 决定**保持现状**（安全默认）；不改为显式开关、不缓存明文结果。决策与多网络实测记录见 [p2-4-p2-5-decision-20261123.md](p2-4-p2-5-decision-20261123.md)。该产品债按决策关闭。
- P2-5（probe_sink 底座）：2026-11-23 round 2 已接线 `compliance/_body_client` → `sources` → `application.pull._probe_sink` → `Store.log_probe`；`body_sha256` 仍未加入 pull 成功 JSON，不能冒充 M4 完整实现。P2-5 接线不阻塞 B4/B5 工程门禁。

## 6. 关闭判断（2026-11-24 最终）

已满足/已关闭：

- B4 application/ports/adapters 与 MCP 服务面通过 reviewer-domain-mcp、reviewer-cli 双 APPROVE；
- B5 全量双版本 1223×2、ruff、CI、docs-only 44、artifacts 58、离线 e2e 4/4、完整联网 e2e 39/39；
- 1.2.0 wheel/sdist 构建、SHA256SUMS、临时安装烟测通过；
- wheel/DB 本地回退与 hk 真实 systemd 回退/再部署通过；
- P2-4 由所有者 2026-11-24 决定保持安全默认；P2-5 probe_sink 已接线；
- hk 已部署 1.2.0（`/opt/guiju`，Python 3.11.2），强化 unit `User=guiju` / `Group=guiju` / `ProtectSystem=strict` / `ReadWritePaths=/var/lib/guiju` / `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` / 空 `CapabilityBoundingSet=`；本地 health 200、MCP query current、公网无 Bearer 为 401；
- 1.2.0 已提交并打 tag `v1.2.0`、推送 GitHub 并创建 Release（见 [release-1.2.0-20261124.md](release-1.2.0-20261124.md)）。

不再有发布/部署 owner gate。保留的非阻塞观察：P2-5 的 `body_sha256` 尚未进入 pull 成功 JSON（M4 完整实现后续版本再做）；csres HTTPS 当前仍不可用，P2-4 的明文结果继续不权威、不落缓存。未访问 NAS。

**结论：ADR 0004 第十节与 ADR 0005 I4 的 1.2.0 发布条件已满足；B5 可以标记为已关闭。**
