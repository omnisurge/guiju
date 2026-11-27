# guiju 1.5.0 发版记录

- 版本：`1.5.0`（分支 `feat/v1.5.0` 本地冻结收口，S0–S4 闭环 + 红队代码审查 `451f1d9`；待所有者按 `AGENTS.md §4` 明确授权合入 `main`、打 tag `v1.5.0` 推送与升级 `hk`）
- 验证：
  - Python 3.10.21 / 3.13.5 全量各 **1318 项单测**（`-W error::ResourceWarning`）通过；
  - `ruff check .`、`GUIJU_CURRENCY=off PYTHON=/usr/local/bin/python3 bash scripts/ci.sh`（含 `check-release.py --docs-only` 与离线 e2e 4/4）、`scripts/check-release.py --artifacts --dist-dir dist --skip-tag`、文档相对链接核验与 `git diff --check` 全绿；
  - 完整联网 e2e：`python3 scripts/e2e.py --require-currency` **39/39 通过**，exit 0（2026-11-27 实测，同步 `db=3257 上游=3257`，二次幂等 `new=0`；日志 `/tmp/guiju-e2e-20261127-v150.log`，2,781 字节，SHA-256 `f7fc898a2e06fa6ba53bfb212b127fb5847fc7c4262fa4fba9394aaaeb3f2756`）；
  - 发版前全量红队代码审查（依据 `.agents/skills/red-team-review/SKILL.md`）发现 3 项可复现缺陷（`AgentReceiptStore.load` 符号链接分类与先备份后改盘顺序、已登记目标祖先目录 symlink 掉包及 Python 3.10/3.13 symlink loop 差异、`check_wheel_contents` 空目录条目逃逸与历史 tag 异常捕获），已于 `451f1d9` 全数修复并补齐回归单测，修复 diff Mode 2 轻量复查结论为 **通过**。
- 主要交付（S0–S4）：新增包内使用态单一真源 `guiju/agent_assets/`（`contract.md` / `skill_frontmatter.yaml` / `history_hashes.json`）、纯领域状态机 `guiju/domain/agent_assets.py`、带外持久化与 `fcntl.flock` `guiju/store/agent_receipts.py`、应用编排与显式 CLI 分发器 `guiju agent {show|install|uninstall|status}`、`EXIT_CONFLICT = 11`（`AgentConflict`）、`McpServer.initialize` 同源 `instructions` 直读、5 处消费方可见输出净化守卫及 `scripts/check-release.py --artifacts` wheel 与历史哈希 tag 门禁；详见 [ADR 0008](../adr/0008-agent-skills-and-usage-decoupling.md) 与 [spec-1.5.0.md](../spec-1.5.0.md)。
- 产物（清理 `build/` 后经 `uv build --wheel --sdist --out-dir dist`，零 `SetuptoolsDeprecationWarning`）：
  - wheel `guiju-1.5.0-py3-none-any.whl` SHA-256 `8da078b33b0be1e80436d98d7fc97311b5fc609bac7a3599ed2b30dcb45ae54d`（254,417 个字节 / 108 个文件，`Metadata-Version: 2.4` / `Name: guiju` / `Version: 1.5.0` / `License-Expression: MIT` / 零运行时第三方依赖 / 含 `guiju/agent_assets/*`、不含 `AGENTS.md`/`.agents/`/`docs/`/`scripts/`/`tests/`）；
  - sdist `guiju-1.5.0.tar.gz` SHA-256 `bd95649973cc593861cb06ebb3f6dd5d1431d95555acb9db687569ec09dfb485`（426,047 个字节 / 199 个条目，含 `guiju/_version.py`，不含 `docs/` 与 `AGENTS.md`）；
  - `dist/SHA256SUMS` 与上述及 [ACCEPTANCE.md](../ACCEPTANCE.md) 一致。
- 本地回退演练（`/tmp/guiju-rollback-drill-1.5.0-20261127`）：离仓安装 `1.5.0` wheel 后实测 `guiju version --json`（`1.5.0`）、`guiju agent show --kind contract`、`guiju agent install --json`（`installed`）、`guiju agent status --json`（`clean`）与 `McpServer.initialize.instructions`（1,114 字符同源摘录）；完成 `1.5.0 → 1.4.0 → 1.5.0` 临时 venv wheel 切换、`catalog.db` `SCHEMA_VERSION=2` 读写与标记保留（`before-rollback-1.5.0`）、`atlas-catalog.json`（`16G101-1` / `replaced_by=22G101-1`）与 `agent-receipts.json` / `.agents/skills/guiju-standards/SKILL.md` 共存验证（回到 `1.5.0` 后 `agent status` 为 `clean`、`agent install` 为 `unchanged`）；本机 editable 实测 `guiju --version` 返回 `guiju 1.5.0`。
- hk：尚未部署 `1.5.0`（线上当前运行 `1.4.0`，`v1.5.0` 不改变 `catalog.db` schema 与 systemd unit；遵照 `AGENTS.md §4` 权限红线，远端打 tag 推送、GitHub Release 发布与 `hk` `guiju-mcp` 升级待所有者明确授权后另行执行，不访问 NAS）。
