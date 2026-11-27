# guiju 1.4.0 发版记录

- 版本：`1.4.0`（分支 `feat/v1.4.0`，本地冻结基线 S0–S3 + `248281c` + S4 冻结；待所有者确认合入 `main` 并打发布 tag `v1.4.0`）
- 验证：
  - Python 3.10.21 / 3.13.5 全量各 **1279 项单测**通过；
  - `ruff check .`、`GUIJU_CURRENCY=off bash scripts/ci.sh`（含 `check-release.py --docs-only` 与离线 e2e 4/4）、`scripts/check-release.py --artifacts --dist-dir dist --skip-tag`、`git diff --check` 全绿；
  - 完整联网 e2e：`python3 scripts/e2e.py --require-currency` **39/39 通过**，exit 0（`2026-11-27 11:22–11:28 CST` 实测，同步 `db=3257 上游=3257`，二次幂等 `new=0`）；
  - 国标电子书库图集真链点查：`22G101-1`（`current`）、`16G101-1`（`superseded` / `replaced_by=22G101-1`，无 `B16G101-1` 误配）、`21ZJ111` / `粤25G/T116`（`current`）、`pull 22G101-1`（exit `30` / `copyright_atlas`）、`cite 16J916-1 --strict`（exit `31` / `copyright_atlas`）、`atlas sync`（`total=4252`，`SCHEMA_VERSION=2` 未变）、`atlas search` 与 MCP `atlas_search` / `announcement` 均通过；
  - 发版前红队审查与修复后轻量复查（依据 `.agents/skills/red-team-review/SKILL.md`）：**通过**。
- 主要交付（S0–S3 + `248281c`）：接入官方国标电子书库只读图集时效适配器 `chinabuilding` 与纯领域模块 `guiju/domain/atlas.py`（方案 A + `replaced_by`、`MetadataPolicy` 按端点 8 MB 上限、`DiskCurrencyCache` 兼容 `v1.3.0` 存量缓存）、独立只读目录快照 `atlas-catalog.json`、`guiju atlas {sync|search|list}` 与 MCP 只读 `atlas_search`、显式带 4 位版年防跨版替代护栏、`pyproject.toml` PEP 639 治理（消除 `SetuptoolsDeprecationWarning`）及 `AGENTS.md` / `docs/AGENT-USAGE.md` 契约拆分；详见 [ADR 0007](../adr/0007-atlas-currency-and-catalog.md)。
- 产物（清理 `build/` 后经 `uv build --wheel --sdist --out-dir dist`，零 `SetuptoolsDeprecationWarning`）：
  - wheel `guiju-1.4.0-py3-none-any.whl` SHA-256 `448aa1a5472065773fafece13baacaf40b5d222db0d74002b1c1a4ac690f8498`（226,846 个字节 / 100 个文件，`Metadata-Version: 2.4` / `Name: guiju` / `Version: 1.4.0` / `License-Expression: MIT` / `License-File: LICENSE` / 零运行时第三方依赖 / 不含已退场兼容模块与 `docs/`）；
  - sdist `guiju-1.4.0.tar.gz` SHA-256 `a6890f0606cb136a5b9595d7d2ab8f7a69ea6ae25af25747b36e7316f2d424cb`（385,803 个字节 / 187 个条目，含 `guiju/_version.py`，不含 `docs/`）；
  - `dist/SHA256SUMS` 与上述及 [ACCEPTANCE.md](../ACCEPTANCE.md) 一致。
- 本地回退演练（`/tmp/guiju-rollback-drill-20261127`）：完成 `1.4.0 → 1.3.0 → 1.4.0` 临时 venv wheel 切换、`SCHEMA_VERSION=2` `catalog.db` 直读与保留（`rollback_drill_marker=before-rollback-1.4.0`）以及独立 `atlas-catalog.json`（`16G101-1` / `replaced_by=22G101-1`）并存验证；本机 editable 实测 `guiju --version` 返回 `guiju 1.4.0`。
- hk：尚未部署（当前运行 `1.3.0`，待所有者确认窗口执行 `/opt/guiju` 升级、`GUIJU_HOME=/var/lib/guiju guiju atlas sync` 预热及 `1.3.0 ↔ 1.4.0` 真实 systemd 回退/再部署演练后补充实测并打 `v1.4.0` tag）；未访问 NAS。
