# guiju Agent 使用契约入口（指针页）

> 本文件仅作仓库内索引指针。遵循 [ADR 0008](adr/0008-agent-skills-and-usage-decoupling.md) 单一真源约定，不再在此重复维护消费方使用规则、退出码决策表或 JSON 形状。

- **唯一消费方使用契约真源**：[`guiju/agent_assets/contract.md`](../guiju/agent_assets/contract.md)
- **离仓消费 / CLI 打印契约**：
  - 输出完整使用契约原文：`guiju agent show --kind contract`
  - 安装标准开放 Agent Skill 目录（`guiju-standards/SKILL.md`）：`guiju agent install`
  - 启用 MCP 工具服务：`guiju mcp`（服务端 `initialize` 的 `instructions` 直接从同一份契约真源运行时提取）
- **人类用户与本仓维护分流**：
  - 人类用户命令手册：[README.md](../README.md)
  - 参与本仓源码开发与发版审查的 agent：仓库根 [AGENTS.md](../AGENTS.md)
