# guiju v1.5.0 迭代规格（Spec）

- 状态：**准备就绪（2026-11-27，S0 已按 ADR 0008 评审意见修订；待所有者审阅确认后进入 S1）**
- 分支：`feat/v1.5.0`
- 基线：`main` @ `dc13268`（`v1.4.0` 正式版）
- 目标版本：`1.5.0`
- 关联文档：[ADR 0008](adr/0008-agent-skills-and-usage-decoupling.md) · [ADR 0007](adr/0007-atlas-currency-and-catalog.md) · [产品架构 v1](ARCHITECTURE.md) · [使用契约入口](AGENT-USAGE.md) · [交接文档](../HANDOFF.md)
- 红线约束：
  - 运行时**零第三方依赖**（`pyproject.toml` `dependencies = []`）；
  - 退出码契约严格按 ADR 0008 D5 仅在 `1x` 本地文件段新增 `EXIT_CONFLICT = 11`（`AgentConflict`，不污染既有 `10/20/30–34/40/50` 规范采集语义），**不升 DB schema v3**（保持 `SCHEMA_VERSION = 2`）；
  - **双层净化隔离**：wheel 不含根 `AGENTS.md` 与 `.agents/`，且 `guiju/agent_assets/` 与 MCP 导出文本通过内容级词法/相对死链守卫；
  - **目标目录纯净与带外收据**：受管收据只存于 `GUIJU_HOME/agent-receipts.json`，目标 skill 目录绝不写私有清单文件；
  - **零安装期副作用**，绝不接管或整份覆写宿主工作区 `AGENTS.md`，绝不触碰同级第三方 skill、`guiju/net/` 或 NAS（ADR0048）。

---

## 一、目标与范围

本迭代按 [ADR 0008](adr/0008-agent-skills-and-usage-decoupling.md) 落地 **开发态约束/技能** 与 **产品使用态契约/技能** 的双层净化解耦、真正单一真源（SSOT）与跨 harness 通用分发：

1. **包内使用态单一真源（`guiju/agent_assets/`）与文本净化**：建立唯一消费方真源目录（净化的自包含 `contract.md`、开放标准 `skills/guiju-standards/SKILL.md` 及同源 `mcp-instructions.txt`）；清理原 `docs/AGENT-USAGE.md` 中混入的相对死链与源码路径/类名并将其改写为极简指针页。
2. **显式 CLI 分发器（`guiju agent`）与带外收据平滑升级**：提供 `show`、`install`、`status`、`uninstall`（`--json` / `--kind contract|skill` / `--target agents|claude` / `--dir` / `--scope project|user` / `--force`）；实现 `GUIJU_HOME/agent-receipts.json` 带外收据、`EXIT_CONFLICT = 11`、四态判定与旧版受管无感平滑升级。
3. **MCP 运行时直读与自动化守卫**：`guiju/mcp.py` 在 `initialize.instructions` 中运行时直读包内真源；测试覆盖路径级 + 内容级词法/链接纯净守卫、SSOT 防漂移与完整红队审查。

---

## 二、分步实施计划（S0–S4）

### S0 · 分支基线、ADR 0008 与迭代 Spec
- **改动**：从 `main` @ `dc13268` 切出 `feat/v1.5.0`，完成并按 6 项红队评审意见修订 `docs/adr/0008-agent-skills-and-usage-decoupling.md`、`docs/spec-1.5.0.md`，登记 `CHANGELOG.md` `## Unreleased`、`docs/adr/README.md` 与 `docs/README.md`。
- **门禁**：`ruff check .`、`python3 scripts/check-release.py --docs-only`、`GUIJU_CURRENCY=off bash scripts/ci.sh` 全绿（不改 `guiju/_version.py`）。

### S1 · 使用态单一真源 `guiju/agent_assets/`、指针文档与纯领域状态机
- **改动**：
  1. 新增包内资产 `guiju/agent_assets/`（`contract.md`、`skills/guiju-standards/SKILL.md`、`mcp-instructions.txt`），更新 `pyproject.toml` `[tool.setuptools.package-data]`；
  2. 将 `docs/AGENT-USAGE.md` 改为极简指针页（指向 `guiju/agent_assets/contract.md` 与 `guiju agent show --kind contract`），消除仓内多份并行正文的重复维护；
  3. 在 `guiju/errors.py` 新增 `EXIT_CONFLICT = 11`（`AgentConflict`）并写入 `EXIT_TABLE`；
  4. 新增纯领域模块 `guiju/domain/agent_assets.py`：路径规范化解析（`agents`/`claude`/`--dir`、`project`/`user`）、带外收据 `agent-receipts.json` 读写校验，以及四态判定（`installed`/`unchanged`/`upgraded` 平滑升级/`agent_unmanaged_conflict`/`agent_user_modified`/`agent_receipt_corrupt`）；
  5. 补齐纯领域单测（兼容 editable 与 wheel `importlib.resources` 加载、平滑升级 vs 篡改拒绝）。

### S2 · 应用层 `guiju/application/agent.py` 与 CLI `guiju agent`
- **改动**：
  1. 新增 `guiju/application/agent.py`（只读 `show`/`status` 绝不建目录，显式 `install`/`uninstall` 原子写盘与带外收据维护，严格仅管理 `guiju-standards` 子目录）与 `guiju/cli/commands/agent.py`，接入 `guiju/cli/__init__.py`；
  2. 补齐应用层与 CLI 端到端单测（覆盖 `show` / `status` / 首次安装 / 无变更 / 旧版平滑升级无 `--force` / 非受管冲突 `exit 11` / 手工改文件或脏文件 `exit 11` / 收据损坏 `exit 11` / `--force` 覆盖 / 安全卸载 `0` 与非受管卸载 `11` / 同级其他 skill 目录零触碰）。

### S3 · MCP 运行时同源直读、双层净化守卫与红队审查
- **改动**：
  1. 更新 `guiju/mcp.py`：`initialize` 响应按 MCP 规范返回运行时直读自包内 `agent_assets` 的 `instructions`；
  2. 新增路径级 + 内容级双层净化守卫测试（构建临时 wheel 或检查包文件列表与内容：含 `agent_assets/`、绝不含根 `AGENTS.md`/`.agents/`、消费方文本与 MCP 输出零 `../`、零 `guiju/net/`、`guiju/sources/`、`BodyPolicy`、`RedirectGuard`、`red-team-review` 等泄露，且无双写并行副本）；
  3. 同步更新 `README.md` 与 `docs/hygiene` 相关校验测试（确保不回归 `test_hygiene.py`）；
  4. 按 `.agents/skills/red-team-review/SKILL.md` 执行全量红队代码审查与修复复查。

### S4 · `v1.5.0` 冻结、验收、产物构建、本地与 hk 演练及发布
- **改动**：冻结 `1.5.0`、同步发布文档、关闭 ADR 0008；
- **门禁**：双 Python 3.10/3.13、`ci.sh`、联网 `scripts/e2e.py --require-currency` 39/39、wheel/sdist 打包与 `check-release.py --artifacts`、本地与 hk `1.4.0 ↔ 1.5.0` 升级/回退演练，经所有者确认后发布。

---

## 三、进度跟踪清单

- [x] **S0**：`feat/v1.5.0` 基线、ADR 0008（按 6 项评审意见修订）、`spec-1.5.0.md`、`CHANGELOG.md` `Unreleased` 与文档索引登记
- [ ] **S1**：`guiju/agent_assets/` 单一真源、`docs/AGENT-USAGE.md` 指针化、`EXIT_CONFLICT = 11` 与纯领域四态/带外收据逻辑
- [ ] **S2**：`guiju/application/agent.py` 与 CLI `guiju agent` 子命令及全量单测
- [ ] **S3**：MCP `initialize.instructions` 运行时直读、双层净化与 SSOT 守卫及红队审查
- [ ] **S4**：`v1.5.0` 冻结、真链验收、产物构建、回退演练及发布
