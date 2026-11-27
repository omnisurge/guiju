# guiju v1.5.0 迭代规格（Spec）

- 状态：**准备就绪（2026-11-27，S0 完成；待所有者审阅确认 ADR 0008 后进入 S1）**
- 分支：`feat/v1.5.0`
- 基线：`main` @ `dc13268`（`v1.4.0` 正式版）
- 目标版本：`1.5.0`
- 关联文档：[ADR 0008](adr/0008-agent-skills-and-usage-decoupling.md) · [ADR 0007](adr/0007-atlas-currency-and-catalog.md) · [产品架构 v1](ARCHITECTURE.md) · [使用契约](AGENT-USAGE.md) · [交接文档](../HANDOFF.md)
- 红线约束：
  - 运行时**零第三方依赖**（`pyproject.toml` `dependencies = []`）；
  - **不新增退出码**，**不升 DB schema v3**（保持 `SCHEMA_VERSION = 2`）；
  - **零安装期副作用**：严禁 `pip`/`uv` 安装期静默写宿主 `.agents/` / `.claude/`；所有安装/导出仅通过显式 `guiju agent` CLI 触发；
  - **开发态资产绝不入包**：仓库根 `AGENTS.md` 与 `.agents/skills/red-team-review/SKILL.md` 不得打入 wheel；
  - `guiju/net/` 出站红线一字不动，绝不触碰 NAS（ADR0048）。

---

## 一、目标与范围

本迭代按 [ADR 0008](adr/0008-agent-skills-and-usage-decoupling.md) 实现 **开发态约束/技能** 与 **产品使用态契约/技能** 的正式解耦与跨 harness 通用分发：

1. **包内使用态资产单一真源（`guiju/agent_assets/`）**：内置通用产品态使用契约片段与符合开放 Agent Skills 规范（`SKILL.md`）的 `guiju-standards` 技能，通过 `pyproject.toml` 随 wheel / sdist 分发。
2. **显式 CLI 分发器（`guiju agent`）**：支持 `show`、`install`、`status`、`uninstall`（含 `--json` / `--stdout` / `--target` / `--dir` / `--scope project|user` / `--force`），可一键导出到 `.agents/skills/`、`.claude/skills/` 或任意自定义目录，具备受管清单校验与防误覆盖保护。
3. **MCP `instructions` 与使用文档同源对齐**：`guiju/mcp.py`、`docs/AGENT-USAGE.md` 与 `guiju/agent_assets/` 共享同一套产品态核心契约口径，并用 AST / 打包 / 单测守卫锁住边界。

---

## 二、分步实施计划（S0–S4）

### S0 · 分支基线、ADR 0008 与迭代 Spec
- **改动**：从 `main` @ `dc13268` 切出 `feat/v1.5.0`，新增 `docs/adr/0008-agent-skills-and-usage-decoupling.md`、`docs/spec-1.5.0.md`，在 `CHANGELOG.md` 添加 `## Unreleased` 并更新 `docs/adr/README.md` 与 `docs/README.md`。
- **门禁**：`ruff check .`、`python3 scripts/check-release.py --docs-only`、`GUIJU_CURRENCY=off bash scripts/ci.sh` 全绿（不改 `guiju/_version.py`）。

### S1 · 使用态资产 `guiju/agent_assets/`、打包配置与纯领域契约逻辑
- **改动**：
  1. 在 `guiju/agent_assets/` 下落位产品态使用契约模板与 `guiju-standards/SKILL.md`；更新 `pyproject.toml` 确保包数据正确打入 wheel/sdist；
  2. 新增纯领域模块（如 `guiju/domain/agent_assets.py`）：统一目标路径解析（`agents`/`claude`/自定义 `--dir`、`project`/`user`）、受管清单序列化与 SHA-256 冲突判定（区分受管未改、受管已手工修改、未受管同名目录/文件）；
  3. 补齐纯领域单测与资源读取兼容（兼容 `importlib.resources` 可编辑安装及 wheel 安装）。

### S2 · 应用层 `guiju/application/agent.py` 与 CLI `guiju agent`
- **改动**：
  1. 新增 `guiju/application/agent.py`（只读查询与显式安装/卸载用例，尊重 `--json`/`--quiet`，绝不隐式写盘）与 `guiju/cli/commands/agent.py`，接入 `guiju/cli/__init__.py` 与 `CommandWiringTest`；
  2. 支持 `guiju agent show [--kind skill|usage]`、`guiju agent install`、`guiju agent status`、`guiju agent uninstall`；
  3. 补齐应用层及 CLI 端到端单测（覆盖多 harness 目录、自定义 `--dir`、非受管冲突拒绝、手工修改保护、`--force`、安全卸载与只读 `show`/`status` 零建目录）。

### S3 · MCP 同源对齐、打包边界守卫与红队审查
- **改动**：
  1. 对齐 `guiju/mcp.py` 服务端引导口径与 `docs/AGENT-USAGE.md` / `guiju/agent_assets/`；
  2. 在 `tests/` 补充打包边界守卫（验证内置 wheel 构建物包含 `agent_assets/` 且绝不含根目录 `AGENTS.md` 与 `.agents/skills/red-team-review/`）及关键契约防漂移测试；
  3. 按 `.agents/skills/red-team-review/SKILL.md` 执行全量红队代码审查与修复复查。

### S4 · `v1.5.0` 冻结、验收、产物构建、本地与 hk 演练及发布
- **改动**：冻结 `1.5.0`、同步发布文档、关闭 ADR 0008；
- **门禁**：双 Python 3.10/3.13、`ci.sh`、联网 `scripts/e2e.py --require-currency` 39/39、wheel/sdist 打包与 `check-release.py --artifacts`、本地与 hk `1.4.0 ↔ 1.5.0` 回退演练，经所有者确认后发布。

---

## 三、进度跟踪清单

- [x] **S0**：`feat/v1.5.0` 基线、ADR 0008、`spec-1.5.0.md`、`CHANGELOG.md` `Unreleased` 与文档索引登记
- [ ] **S1**：`guiju/agent_assets/` 资产、打包配置与纯领域清单/冲突判定
- [ ] **S2**：`guiju/application/agent.py` 与 CLI `guiju agent` 子命令及单测
- [ ] **S3**：MCP/文档同源守卫、wheel 开发/使用隔离守卫与红队审查
- [ ] **S4**：`v1.5.0` 冻结、真链验收、产物构建、回退演练及发布
