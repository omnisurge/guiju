# guiju ADR 索引

> 更新时间：2026-11-20

本目录只收 guiju 自己的 ADR。kb-system/ADR0048 不是本项目 ADR；它只作为外部 NAS 变更冻结约束被引用。

| 编号 | 标题 | 状态 | 当前阅读方式 |
|---|---|---|---|
| [0001](0001-currency-and-scope.md) | 废止 index，与 spec-currency 分层协作 | **已关闭（历史）** | 保留历史决策；第三节/第五节已被 ADR 0002 取代；cmd/mcp 描述已在 2026-11-13 退役 |
| [0002](0002-merge-into-one-product.md) | 合并为一个产品：guiju 吸收 spec-currency | **已关闭（历史）** | 保留合并过程与验收证据；当前运行形态看 README/deploy/README |
| [0003](0003-catalog-self-refresh.md) | 目录自保鲜：进程内刷新，不依赖设备级定时任务 | **已实现（现行）** | 当前 search/which/pull/cite 自动保鲜行为 |
| [0004](0004-contract-first-hardening.md) | 契约优先的根源性修复（停止按点止血） | **已批准（现行实施）** | 当前架构收口与 1.2.0 整改依据 |

## 规则

- ADR 原文是决策证据，不重写、不删除；状态变更只更新本索引和顶部阅读提示。
- 已关闭 ADR 中的旧实现细节（尤其 cmd/mcp、NAS spec-currency）只作历史背景，不能据此恢复旧服务。
- 新的替代决策必须新增 ADR 编号，并在本索引标注被取代关系；不得用旧 ADR 覆盖新事实。
