# ADR 0008 · 开发态与使用态 Agent 约束/技能解耦及跨 Harness 分发

- 状态：**提议中（待所有者审阅确认后启动 S1）**（2026-11-27）
- 决策人：所有者
- 触发：`v1.4.0` 收口时已将仓库根 `AGENTS.md`（开发者/发版约束）与 [docs/AGENT-USAGE.md](../AGENT-USAGE.md)（消费方使用契约）拆分，并在本仓 `.agents/skills/red-team-review/SKILL.md` 沉淀了开发审查技能；但下游消费者安装 `guiju` 后，仍缺少随安装包分发、且通用于任意 AI agent / harness 的产品态使用契约与可移植 Agent Skills。
- 影响：`pyproject.toml`、新增 `guiju/agent_assets/`、新增 `guiju/domain/agent_assets.py`（或同类纯领域模块）、新增 `guiju/application/agent.py`、新增 `guiju/cli/commands/agent.py`、`guiju/cli/__init__.py`、`guiju/mcp.py`、`docs/AGENT-USAGE.md`、`tests/`
- 关系：延续 [ADR 0004](0004-contract-first-hardening.md)（契约优先）、[ADR 0006](0006-v1-3-0-architecture-and-mcp-purification.md)（架构分层与 MCP 纯净化）及 [ADR 0007](0007-atlas-currency-and-catalog.md)（PEP 639 打包治理）；具体分期见 [spec-1.5.0.md](../spec-1.5.0.md)。

---

## 一、背景与问题

1. **职责混杂风险**：开发约束关心的是源码分层、`guiju/net/` AST 边界、`scripts/ci.sh` 与发版审查；消费方 agent 关心的是命令行 `--json` 协议、退出码 `0/10/20/30/31/32/33/34/40/50/2/1` 动作决策、`guiju which/pull/cite/atlas` 选用与 MCP 工具边界。把开发态规则发给消费方会污染上下文甚至误导只读调用。
2. **多 harness 生态现实**：下游既可能是 Antigravity，也可能是 Claude Code、Codex、Cursor 或任意自研 agent harness；且消费者多通过 `uv tool install` / `pip install` 安装产物使用，而非克隆本源码仓。
3. **打包与写盘安全边界**：Python wheel 构建不应混入仓库开发文档/审查技能；同时标准 Python 打包生态不支持、也不应借助安装期 hook 在用户未授权的情况下静默向 `~/.agents`、`~/.claude` 或当前工作目录写入文件。

---

## 二、决策

### D1 严格隔离开发态资产与使用态产品资产

- **开发态资产（仅留仓库，绝不入包）**：根目录 `AGENTS.md`、`.agents/skills/red-team-review/SKILL.md`、`CONTRIBUTING.md`、`docs/` 属源码开发与发版治理资产，**严禁打入 wheel**。
- **使用态资产（随产品包内单一真源分发）**：新增包内数据目录 `guiju/agent_assets/` 存放产品态消费方使用契约片段与开放标准 Agent Skill 模板，通过 `pyproject.toml` 声明随 wheel/sdist 一同分发（离线可用、不联网下载模板）。

### D2 通用跨 Agent / Harness-Agnostic 资产形态（方法 2）

- 采用开放通用的纯文本 Markdown 与开放 Agent Skills（`SKILL.md` + YAML frontmatter）结构，**零第三方依赖**，不绑定任何单一 IDE 私有扩展格式或插件市场。
- 随包内置以下最小高价值消费方资产：
  1. **产品态使用契约片段**（覆盖 `--json` 主路径、无交互、退出码决策表、`warnings`/strict、`availability` 口径、图集版权边界与不做的事）；
  2. **可移植消费方 Skill**：`guiju-standards`（`SKILL.md`），为任意 agent 提供“查规范 → 核时效（含国标/行标/上海地标/图集）→ 取正文 → 出引用串 → 查图集目录”的标准工作流与禁区护栏。

### D3 显式 CLI 安装与导出命令（零静默副作用）

- 新增一级子命令 **`guiju agent`**（计划子命令：`show` / `install` / `status` / `uninstall`，并支持 `--json`）：
  - **绝不在 `pip install` / `uv tool install` 期间自动改写宿主或工作区文件**，所有磁盘写入必须由用户显式执行 `guiju agent install` 触发；
  - **多目标与自定义目录支持**：
    - `--target` 预设覆盖主流开放约定（如 `agents` → `.agents/skills/`、`claude` → `.claude/skills/`）及 `--dir <PATH>` 任意自定义目标目录（支持 `--scope project|user`）；
    - 支持 `guiju agent show`（及 `--stdout`）直接打印产品态使用契约片段或 `SKILL.md` 原文，供任何自研 harness、CI 模板或非标准路径直接接入；
  - **受管标记与防误覆盖护栏**：
    - 写入的受管目录/文件记录明确受管清单或标记与内容 SHA-256 校验；
    - 遇到未受管的同名已有 Skill，或检测到用户已手工编辑过目标文件时，默认拒绝覆盖（显式报错误码与提示，除非用户传 `--force`）；
    - `guiju agent uninstall` **仅移除受管安装文件**，绝不删除用户自建 skill 或改动无关 `AGENTS.md`。

### D4 MCP `instructions` 与文档单一真源

- `guiju/mcp.py` 对外暴露的 MCP `initialize` 说明 / `TOOLS` 引导文案与 `docs/AGENT-USAGE.md`、`guiju/agent_assets/` 产品态使用契约保持口径同源；新增单元测试对关键契约（退出码、图集拒载、不绕过限速、warnings 口径）与文档/资产一致性做静态守卫，防止各处漂移。

### D5 保持核心架构与合规红线不变

- 运行时继续保持**零第三方依赖**（Python 3.10+ 标准库，POSIX-only）；
- 数据库保持 `SCHEMA_VERSION = 2` 不变；
- **不新增退出码**，沿用 `guiju/errors.py` 既有契约（成功 `0` / 已存在等按现有 `EXIT_TABLE`、输入参数错误 `2`、冲突/未找到目标按现有错误码体系与 CLI 契约映射）；
- 严禁修改 `guiju/net/` 出站政策与限速边界；不触碰 NAS（ADR0048）。

### D6 明确不做

- 不提供 `pip` / `uv` 安装期静默写盘 hook 或后台守护进程自动写入 `.agents/` / `.claude/`；
- 不绑定单一厂商私有插件格式或发布仅某一家工具可读的专有包；
- 不把本仓 `AGENTS.md` 开发红线、发版检查、`red-team-review` 技能打入消费方安装包或 MCP 说明中。

---

## 三、关闭条件（规划 `v1.5.0` S4）

1. 完成 S1–S3 实现、`guiju agent` 全套离线单测（覆盖 `show`/`install`/`status`/`uninstall`、多 target、`--dir`、`--stdout`、非受管冲突拒绝、`--force`、用户篡改保护、`uninstall` 边界）；
2. Wheel / sdist 构建打包守卫通过：包内包含 `guiju/agent_assets/`，且明确不包含仓库根 `AGENTS.md` 与 `.agents/skills/red-team-review/SKILL.md`；
3. 双 Python（3.10 / 3.13）全量单测、`ruff`、`scripts/ci.sh` 与联网 `scripts/e2e.py --require-currency` 39/39 通过；
4. 完成红队审查/复查并完成 `v1.5.0` 本地与 hk 升级/回退演练后关闭本 ADR。
