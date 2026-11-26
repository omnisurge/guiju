# guiju 1.3.0 发版记录

- 版本：`1.3.0`（发布 tag `v1.3.0` 指向 `main` 最终发布提交；基线 `5838b6a` + S0–S6 收口）
- 验证：
  - Python 3.10.21 / 3.13.5 全量各 **1224 项单测**通过；
  - `ruff check .`、`GUIJU_CURRENCY=off bash scripts/ci.sh`（含 docs-only 45 项与离线 e2e 4/4）、完整 `scripts/check-release.py --artifacts --dist-dir dist`、`git diff --check` 全绿；
  - 完整联网 e2e：`python3 scripts/e2e.py --require-currency` **39/39 通过**，exit 0（同步 `db=3257 上游=3257`，二次幂等 `new=0`）；日志 `/tmp/guiju-e2e-20261126-release.log`（2781 bytes，SHA-256 `dc94b38cafd5a608ced816705a085b1d483d101607842b28b276c452290a4f56`）。
- 主要交付（S0–S5）：M4 改版观测闭环（`body_sha256`、`probe_sink`、`guiju sources`）、冗余清理与锁收窄、`guiju/compliance.py`/`guiju/currency/httpx.py`/`guiju/cli/{lookup,render}.py` 与 `application/` 隐式构造兜底退场、MCP 服务面纯净化（`batch` 仅内联 `codes[]`、强制 `query(code) -> Outcome`、`_ReadOnlyStore` 白名单化）及 33 份批次回归测试零删减收敛；详见 [ADR 0006](../adr/0006-v1-3-0-architecture-and-mcp-purification.md)。
- 产物（`uv build --wheel --sdist --out-dir dist`）：
  - wheel `guiju-1.3.0-py3-none-any.whl` SHA-256 `cb3d7ec00569412f8d559b2568472b1d4ed353ed41192c8bd88d5ec9ab73a187`（99 个文件，`Name: guiju` / `Version: 1.3.0` / 零运行时第三方依赖 / POSIX-only）；
  - sdist `guiju-1.3.0.tar.gz` SHA-256 `adfa515d1e4165bf084c8307581dd804a61eaf2a92e2adf272e1e1d773299bda`（179 个条目，含 `guiju/_version.py`，不含 `docs/`）；
  - `dist/SHA256SUMS` 与上述及 [ACCEPTANCE.md](../ACCEPTANCE.md) 一致。
- 本地回退演练（`/tmp/guiju-rollback-drill-20261126`）：完成 `1.3.0 → 1.2.0 → 1.3.0` 临时 venv 安装切换及 `SCHEMA_VERSION=2` `catalog.db` 直读兼容与备份回滚验证（`rollback_drill_marker=before-rollback-1.3.0` 无损）；本机 editable `guiju --version` 实测 `1.3.0`。
- systemd unit 变更：`deploy/guiju-mcp.service` 已移除 `ExecStartPre=/bin/mkdir -p /var/lib/guiju/mcp`（SHA-256 `a91fcd79d9fec2b726aabc586bca2f514b96ed3785d656fe2ce3ba4d238ffc6c`）。
- hk：2026-11-26 20:00 CST 已升级 `1.3.0`（`/opt/guiju`，Python 3.11.2）并同步强化 unit（`User=guiju`、`Group=guiju`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/guiju`、`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`），本地 health `{"ok":true}`、MCP `query` `GB 55037-2022` 返回 `current`、MCP `batch` 内联 `codes[]` 通过且 `list` 明确拒绝（`isError=True`）、公网无 Bearer 返回 `401`；完成 `1.2.0 ↔ 1.3.0` 真实 systemd 回退（回退期实测 `1.2.0` / 旧 unit `187d3bb3...` / health 200 / MCP `query` current）与再部署；备份 `/opt/guiju/backup-1.2.0-20261126-195727/`（另留 `guiju.prev-1.2.0-20261126-195727` 与 `guiju-mcp.service.bak-20261126-195727`）；`/var/lib/guiju` 保持原样未动，未访问 NAS。
