# guiju v1.5.0 迭代规格（Spec）

- 状态：**已完成（S0–S4 本地冻结、构建、回退演练与发版门禁通过；2026-11-27，待所有者授权推送打 tag 与升级 `hk`）**
- 分支：`feat/v1.5.0`
- 基线：`main` @ `dc13268`（`v1.4.0` 正式版）
- 目标版本：`1.5.0`
- 关联文档：[ADR 0008](adr/0008-agent-skills-and-usage-decoupling.md) · [ADR 0007](adr/0007-atlas-currency-and-catalog.md) · [产品架构 v1](ARCHITECTURE.md) · [使用契约入口](AGENT-USAGE.md) · [交接文档](../HANDOFF.md)
- 红线约束：
  - 运行时**零第三方依赖**（`pyproject.toml` `dependencies = []`）；
  - 严格分层（`ARCHITECTURE.md` 铁律）：`domain/` 纯逻辑零 I/O 与路径解析，原子持久化与 `fcntl.flock` 锁归 `store/agent_receipts.py`，路径解析与编排归 `application/agent.py`；
  - 退出码契约按 ADR 0008 D5 仅在 `1x` 本地文件段新增 `EXIT_CONFLICT = 11`（`AgentConflict`，不污染既有 `1`/`10`/`20`/`30–34`/`40`/`50`，MCP 永不返回 `11`），**不升 DB schema v3**（保持 `SCHEMA_VERSION = 2`）；
  - **双层净化隔离 & 单一真源**：wheel 打包与 `history_hashes.json[__version__]` 哈希一致检查进 `scripts/check-release.py --artifacts`；包内仅 `guiju/agent_assets/contract.md` 存唯一规则正文；消费方 5 处可见输出全部通过内容级词法/相对死链守卫；
  - **目标目录纯净与带外收据**：收据只存于 `GUIJU_HOME/agent-receipts.json`，目标 `guiju-standards/` 目录零私有元数据文件，任意 symlink 命中（含 `--force`）一律拒绝；
  - **零安装期副作用**，绝不接管宿主 `AGENTS.md`、绝不触碰同级其他工具技能、`guiju/net/` 或 NAS（外部冻结约束见 `kb-system/ADR0048`）。

---

## 一、目标与范围

本迭代按 [ADR 0008](adr/0008-agent-skills-and-usage-decoupling.md) 落地 **开发态约束/技能** 与 **产品使用态契约/通用 Agent Skills** 的解耦与分发：

1. **包内使用态单一真源（`guiju/agent_assets/`）与文本净化**：建立唯一正文真源 `contract.md`（含 MCP 摘要标记）+ 仅含头信息的 `skill_frontmatter.yaml` + 历史哈希表 `history_hashes.json`；运行时确定性拼出 `SKILL.md` 与截取 MCP `instructions`；彻底净化原 `docs/AGENT-USAGE.md` 中的泄露文本并将该文档改为极简指针页。
2. **显式 CLI 分发器（`guiju agent`）、带外收据与完整状态机**：实现 `show`、`install`、`status`、`uninstall`（明确参数默认值、`--dir` 与 `--target`/`--scope` 互斥返回 `exit 2`、JSON 形状契约），通过 `guiju/store/agent_receipts.py` 维护 `GUIJU_HOME/agent-receipts.json`（`schema: 1`、`fcntl.flock`、`.corrupt-<ts>` 备份），完整支持平滑升级、已登记目标团队 `git pull` / `git checkout` 同步、跨机历史哈希认领（`adopted` / `adopted_and_upgraded`）、已删目录收据清理、`--force` 与 symlink 安全拒载（`EXIT_CONFLICT = 11`）。
3. **全链路守卫与打包验收**：实现 MCP `initialize.instructions` 运行时直读、5 处可见面内容净化守卫、`EXIT_TABLE | {EXIT_INTERNAL}` 退出码集合一致性测试、`history_hashes.json` 不可变与发版核验、`tests/test_hygiene.py` 卫生守卫与 `scripts/check-release.py --artifacts` wheel 检验，并通过红队代码审查。

---

## 二、分步实施计划（S0–S4）

### S0 · 分支基线、ADR 0008 与迭代 Spec
- **改动**：从 `main` @ `dc13268` 切出 `feat/v1.5.0`，立项并按 B1–B4 与 R1–R4 完成 `docs/adr/0008-agent-skills-and-usage-decoupling.md`、`docs/spec-1.5.0.md` 修订，登记 `CHANGELOG.md` `## Unreleased` 与 `docs/adr/README.md`、`docs/README.md`。
- **门禁**：`ruff check .`、`python3 scripts/check-release.py --docs-only`、`GUIJU_CURRENCY=off bash scripts/ci.sh` 全绿（不改 `guiju/_version.py`）。

### S1 · 使用态单一真源 `guiju/agent_assets/`、指针文档、存储层与纯领域状态机
- **改动**：
  1. 新增 `guiju/agent_assets/`（`__init__.py`、`contract.md`、`skill_frontmatter.yaml`、`history_hashes.json`），更新 `pyproject.toml` `[tool.setuptools.package-data]` 使用路径 glob 显式纳入资产文件（避免 `guiju-standards` 连字符子目录被当包名）；
  2. 将 `docs/AGENT-USAGE.md` 改为极简指针页（指向 `guiju/agent_assets/contract.md` 与 `guiju agent show --kind contract`）；
  3. 更新 `guiju/errors.py`：新增 `EXIT_CONFLICT = 11`（`AgentConflict`）并写入 `EXIT_TABLE`，同步修正 `guiju/errors.py:27` 单一来源注释；更新 `README.md` 退出码表，**一并补齐既有漏列的 `34`（`no_currency_record`）与新增的 `11`**；
  4. 新增纯领域模块 `guiju/domain/agent_assets.py`（`SKILL.md` 拼接与去 frontmatter 校验、按标记截取 MCP 摘要、优先当前包版本再匹配 `{收据, history_hashes}` 的纯状态机判定，零文件/环境 I/O）；
  5. 新增存储层 `guiju/store/agent_receipts.py`（对标 `store/atlas.py`，实现 `GUIJU_HOME/agent-receipts.json` `schema: 1` 读写、损坏检测与 `.corrupt-<ts>` 备份、POSIX `fcntl.flock` 写锁与原子替换）；
  6. 补齐 S1 纯领域与存储层离线单测（含 `importlib.resources` 加载、逐字节对齐、已登记目标 `{收据, 当前版, history_hashes}` 同步判定、`history_hashes.json` 已发布历史条目只增不改约束、跨机认领、坏收据与 `flock` 并发验证）。

### S2 · 应用层 `guiju/application/agent.py` 与 CLI `guiju agent`
- **改动**：
  1. 新增 `guiju/application/agent.py`（`show`/`status` 只读不建目录、`cwd` / `Path.home()` / `--dir` 路径解析、参数互斥检查、先 `lstat` 递归检测 symlink 再判存在性、快照采集、持锁执行 `install`/`uninstall` 与刷新/清理收据）与 `guiju/cli/commands/agent.py`，接入 `guiju/cli/__init__.py`；
  2. 补齐应用层与 CLI 端到端单测：覆盖全部 `show`/`status`/`install`/`uninstall` 参数默认值与 `--dir` vs `--target`/`--scope` 互斥 `exit 2`、JSON 字段形状、首次/无变更/平滑升级、已登记目标团队 `git pull` 同步当前版（`unchanged` 免写盘刷新收据）与 `git checkout` 切回旧版（`upgraded` 无需 `--force`）、跨机 `adopted`/`adopted_and_upgraded`、`--force` 清理多余文件再运行必 `unchanged`、未受管 `uninstall --force` 绝不强删、`uninstall` 对已登记但目录已被 `rm -rf` 删除的目标清理陈旧收据返回 `exit 0`（`removed_files=[]`）而悬空 symlink 仍返回 `exit 11`、`uninstall --force` 仅删受管文件并保留额外文件、损坏收据各命令行为与 `.corrupt-<ts>` 备份、目录/文件 symlink 含 `--force` 一律 `exit 11`、以及同级第三方 skill 目录零触碰。

### S3 · MCP 运行时同源直读、双层净化守卫、`check-release` 扩展与红队审查
- **改动**：
  1. 更新 `guiju/mcp.py`：`initialize` 响应运行期直接从 `contract.md` 标记区间提取并返回 `instructions`（不改动 MCP 工具逻辑，MCP 永不返回 `11`）；
  2. 新增跨 5 处消费方可见输出（`agent_assets/*` 与拼装 `SKILL.md`、`initialize.instructions`、`TOOLS` 描述、`guiju --help`、`hint`）的内容级词法/相对死链守卫测试；
  3. 新增 `set(EXIT_TABLE.keys()) | {EXIT_INTERNAL} == codes_in(README.md) == codes_in(contract.md)` 退出码集合一致测试，更新 `tests/test_hygiene.py` 相关文档与契约卫生校验；
  4. 扩展 `scripts/check-release.py --artifacts` 增加真实 wheel 路径白/黑名单检验（确认含 `guiju/agent_assets/` 且绝不含根 `AGENTS.md`/`.agents/`/`docs/`/`scripts/`/`tests/`）及 `history_hashes.json` 必须包含当前 `__version__` 且哈希等于运行时派生资产哈希校验；并自已发布历史 tag（`>= v1.5.0`）取回对应 `contract.md` 与 `skill_frontmatter.yaml` 重新派生哈希，逐条核对 `history_hashes.json` 已发布历史条目只增不改；
  5. 按 `.agents/skills/red-team-review/SKILL.md` 执行全量红队代码审查与修复复查。

### S4 · `v1.5.0` 冻结、验收、产物构建、本地与 hk 演练及发布
- **改动**：冻结 `1.5.0`、更新 `history_hashes.json` 收录本版资产哈希、同步发布文档、关闭 ADR 0008；
- **门禁**：双 Python 3.10/3.13、`ci.sh`、联网 `scripts/e2e.py --require-currency` 39/39、wheel/sdist 打包与完整 `check-release.py --artifacts`（含 wheel 内容与 `history_hashes.json` 检验）、本地与 hk `1.4.0 ↔ 1.5.0` 升级/回退演练，经所有者确认后发布。

---

## 三、进度跟踪清单

- [x] **S0**：`feat/v1.5.0` 基线、ADR 0008（按 B1–B4 与 R1–R4 定稿并已接受）、`spec-1.5.0.md`、`CHANGELOG.md` `Unreleased` 与索引登记
- [x] **S1**：`guiju/agent_assets/` 唯一真源、指针页、`EXIT_CONFLICT = 11`（+`README.md` 补 `34`/`11`）、`domain/agent_assets.py` 纯状态机与 `store/agent_receipts.py` 持久化/锁
- [x] **S2**：`guiju/application/agent.py`、CLI `guiju agent` 与全场景单测
- [x] **S3**：MCP `initialize.instructions` 运行时直读、5 处内容级净化/SSOT 守卫、`test_hygiene.py`/`check-release.py` 扩展与红队审查（Commit `451f1d9` 关闭全部 3 项可复现缺陷，Mode 2 复查「通过」）
- [x] **S4**：`v1.5.0` 本地冻结、联网 `e2e.py 39/39`、wheel/sdist 产物构建与 `--artifacts --skip-tag` 对账、本地 `1.5.0 ↔ 1.4.0` 回退演练闭环（远端打 tag 推送与 `hk` 部署待所有者按 `AGENTS.md §4` 另行授权）
