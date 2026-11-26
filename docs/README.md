# guiju 文档索引与状态

> 更新时间：2026-11-26
>
> 当前发布：**1.3.0（2026-11-26）**；发版事实见 [docs/audit/release-1.3.0-20261126.md](audit/release-1.3.0-20261126.md)。1.2.0 / 1.2.0a4 记录保留作历史。

目的：把“当前文档”和“历史证据”分开，避免历史迁移记录被误当成当前运行说明。历史文档不重写事实，也不删除；ADR 接受后只增状态索引，不改写决策原文。

## 当前文档

| 文档 | 状态 | 用途 |
|---|---|---|
| [README.md](../README.md) | 当前 | 用户手册：安装、命令、退出码、合规边界 |
| [AGENTS.md](../AGENTS.md) | 当前 | agent 操作契约：命令选择、退出码动作、必知事实 |
| [INSTALL.md](../INSTALL.md) | 当前 | 安装、自部署、回退 |
| [HANDOFF.md](../HANDOFF.md) | 当前 | 交接状态、已验证事实、踩坑记录 |
| [deploy/README.md](../deploy/README.md) | 当前 | hk 生产服务部署与验证 |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | 当前（v1.3.0 已对齐） | 产品架构；第四节按实际结构描述 |
| [docs/sources.md](sources.md) | 当前基线（2026-10-31 实测） | 数据源现状；当前目录 3255 条见页首说明 |
| [docs/ACCEPTANCE.md](ACCEPTANCE.md) | 当前发布记录 + 历史验收 | 验收记录；顶部为最新发布，以下按版本保留 |
| [docs/audit/release-1.3.0-20261126.md](audit/release-1.3.0-20261126.md) | 当前发版记录 | 1.3.0（v1.3.0）发版事实与产物 |
| [docs/audit/release-1.2.0-20261124.md](audit/release-1.2.0-20261124.md) | 历史发版记录 | 1.2.0（v1.2.0）发版事实与产物 |
| [docs/audit/release-1.2.0a4-20261122.md](audit/release-1.2.0a4-20261122.md) | 历史发版记录 | 1.2.0a4（v1.2.0-alpha.4）发版事实与产物 |
| [docs/audit/external-review-2026-11-22.md](audit/external-review-2026-11-22.md) | 当前复核记录 | 外部评审逐条验证、证据与错误反驳 |
| [docs/remediation-2026-11-18.md](remediation-2026-11-18.md) | 历史计划（B2-B5 已完成；1.2.0 发版） | 1.2.0 契约整改的过程记录 |
| [docs/spec-1.3.0.md](spec-1.3.0.md) | 已完成（v1.3.0 已发布，S0–S6 全部闭环） | v1.3.0 迭代 Spec（M4 观测、去冗余、兼容门面退场、MCP 纯净化、测试收敛） |
| [docs/adr/README.md](adr/README.md) | 当前索引 | 本项目 ADR 0001–0006 的唯一状态入口 |
| [CHANGELOG.md](../CHANGELOG.md) | 当前 | 版本变更 |

## 历史文档（只读证据，不再更新）

| 文档 | 性质 |
|---|---|
| [docs/adr/0001-currency-and-scope.md](adr/0001-currency-and-scope.md) | 已关闭的历史 ADR；其中 cmd/mcp 三适配器描述已随 spec-currency 退役 |
| [docs/adr/0002-merge-into-one-product.md](adr/0002-merge-into-one-product.md) | 已关闭的历史 ADR；保留合并过程与验收证据 |
| [docs/audit/](audit/) | 历史审计/对比记录，见 [审计索引](audit/README.md) |
| [docs/audit-2026-10-30.md](audit-2026-10-30.md) | 2026-10-30 一次性审计记录 |
| [docs/建筑规范CLI-蓝图.md](建筑规范CLI-蓝图.md) | v0.2 历史设计蓝图，已被 ARCHITECTURE 取代 |

历史文档中的 NAS、spec-currency、cmd/mcp 内容都是当时事实或实验记录；不是当前运行依赖，也不是当前施工依据。

## 跨项目边界：ADR0048

kb-system/ADR0048 是 kb-system 的知识库 v2 决策，不是 guiju 的 ADR，也不是 guiju 的发布门禁。它在 guiju 文档中只作为外部运维边界出现：NAS 端在缺陷清单清零前不得执行任何步骤，解除须所有者明示。guiju 当前生产在 hk，运行 native 时效路由，不依赖 NAS；任何 NAS 操作都不属于本项目。

## 文档卫生规则

1. 当前文档只写当前运行形态：native 时效、hk guiju-mcp、GUIJU_CURRENCY=native[:off|dir] / off。
2. 提到已退役的 cmd/mcp、NAS spec-currency 时必须标“历史”，并链接 ADR 0002 或审计记录。
3. ADR 0001–0005 原文保留；状态变化只改本索引和 ADR 顶部提示，不重写决策正文。
4. 审计/验收记录保留证据链，不删除、不改写当时读数；新结论写在新的记录里。
5. 需要淘汰整份文档时，先在本索引标记“历史/归档”，再由所有者决定是否物理删除；不静默删除历史材料。

- [B5 稳定版门禁评估](audit/b5-release-gate-20261123.md)

- [1.2.0 发版记录](audit/release-1.2.0-20261124.md)
- [1.3.0 发版记录](audit/release-1.3.0-20261126.md)
