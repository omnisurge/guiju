# guiju ADR 索引

> 更新时间：2026-11-27

本目录只收 guiju 自己的 ADR。kb-system/ADR0048 不是本项目 ADR；它只作为外部 NAS 变更冻结约束被引用。

| 编号 | 标题 | 状态 | 当前阅读方式 |
|---|---|---|---|
| [0001](0001-currency-and-scope.md) | 废止 index，与 spec-currency 分层协作 | **已关闭（历史）** | 保留历史决策；第三节/第五节已被 ADR 0002 取代；cmd/mcp 描述已在 2026-11-13 退役 |
| [0002](0002-merge-into-one-product.md) | 合并为一个产品：guiju 吸收 spec-currency | **已关闭（历史）** | 保留合并过程与验收证据；当前运行形态看 README/deploy/README |
| [0003](0003-catalog-self-refresh.md) | 目录自保鲜：进程内刷新，不依赖设备级定时任务 | **已实现（现行）** | 当前 search/which/pull/cite 自动保鲜行为 |
| [0004](0004-contract-first-hardening.md) | 契约优先的根源性修复（停止按点止血） | **已关闭（1.2.0 已闭环；D1–D6 为现行契约）** | 第十节关闭条件已于 2026-11-24 随 v1.2.0 满足（hk 部署与真实回退演练完成）；兼容 facade 与 MCP batch list/out 过渡面计划 1.3.0 移除，证据见 b4-application-mcp-20261123.md 与 b5-release-gate-20261123.md |
| [0005](0005-external-review-2026-11-22.md) | 2026-11-22 外部评审复核：MCP 并发/完整性修复与 B4 收口 | **已关闭（I1–I4 已于 1.2.0 闭环）** | P1-1 与 B4/B5 已闭环，P2-4 保持安全默认，P2-5 probe_sink 已接线；I3 非阻塞技术债（body_sha256、CachedCurrency 旧分支、test_fix_* 收敛）转入下版本迭代；P2-6 403/429 建议不采纳 |
| [0006](0006-v1-3-0-architecture-and-mcp-purification.md) | v1.3.0 架构收口、M4 观测闭环与 MCP 纯净化 | **已关闭（1.3.0 已于 2026-11-26 正式发布；D1–D6 为现行契约）** | S0–S6 已全部闭环：M4 观测、`guiju sources`、冗余清理、过渡门面退场、MCP 服务面纯净化、测试收敛及 hk 升级/真实回退演练完成 |
| [0007](0007-atlas-currency-and-catalog.md) | 工程建设标准图集时效校验、完整目录索引与打包元数据治理 | **已关闭（1.4.0 已于 2026-11-27 正式发布；D1–D6 为现行契约）** | 对应 `docs/spec-1.4.0.md` S0–S4：接入国标电子书库（`chinabuilding`）只读图集有效期/替代链校验、`guiju atlas` / MCP `atlas_search`、PEP 639 打包修复 |
| [0008](0008-agent-skills-and-usage-decoupling.md) | 开发态与使用态 Agent 约束/技能解耦及跨 Harness 分发 | **已关闭（1.5.0 已于 2026-11-27 收口；D1–D6 为现行契约）** | 对应 `docs/spec-1.5.0.md` S0–S4：双层净化守卫、`contract.md` 单一正文真源、`store/agent_receipts.py` 带外收据/`flock`、跨机认领与平滑升级、`EXIT_CONFLICT = 11`、显式 `guiju agent` 分发及 MCP `initialize.instructions`（第三节不变量持续守护） |

## 规则

- ADR 原文是决策证据，不重写、不删除；状态变更只更新本索引和顶部阅读提示。
- 已关闭 ADR 中的旧实现细节（尤其 cmd/mcp、NAS spec-currency）只作历史背景，不能据此恢复旧服务。
- 新的替代决策必须新增 ADR 编号，并在本索引标注被取代关系；不得用旧 ADR 覆盖新事实。
