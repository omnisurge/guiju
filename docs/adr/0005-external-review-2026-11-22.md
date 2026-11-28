# ADR 0005 · 2026-11-22 外部评审复核：MCP 并发/完整性修复与 B4 收口

- 状态：**已关闭（I1–I4 已于 1.2.0 闭环；原 2026-11-22 批准）**
- 决策人：所有者
- 触发：外部评审全文（2026-11-22）＋ Lead 逐条源码复核（HEAD `cc30d1d` / `v1.2.0-alpha.4`）
- 影响：MCP HTTP 并发、MCP 查询结果完整性、B4 application/ports/adapters、MCP 服务面、csres 缓存、M4 观察、测试与 1.2.0 稳定版
- 关系：补充 ADR 0004，不取代；ADR 0004 的 D1-D6 仍是长期契约。本文记录 2026-11-22 复核后的增量结论、优先级和明确反驳。详细证据见 [external-review-2026-11-22.md](../audit/external-review-2026-11-22.md)。

> **实施进度（2026-11-24 最终）**：I1（D1/D2 的 MCP Outcome 修复）与 I2（B4 application/ports/adapters、MCP 服务面、systemd 强化）已完成，B4 于 2026-11-23 通过独立双 APPROVE。
> P2-4 由所有者 2026-11-24 确认保持安全默认（csres 明文结果不权威、不落缓存）；P2-5 probe_sink 已接线，`body_sha256` 仍未进入 pull 成功 JSON。
> **1.2.0 已于 2026-11-24 正式发布（tag `v1.2.0`）**：全量 1223×2、CI、docs-only 44、artifacts 58、离线 e2e 4/4、完整联网 e2e 39/39；wheel/DB/systemd 回退演练完成；hk 已部署 1.2.0 与强化 unit，并完成真实 1.2.0a4↔1.2.0 回退/再部署。
> I3 中 P2-4/P2-5 的处置已按本 ADR 关闭；`CachedCurrency` 旧分支与测试命名收敛仍作为非阻塞后续技术债，不影响 1.2.0 发布。
> 细节见 [b4-application-mcp-20261123.md](../audit/b4-application-mcp-20261123.md)、[p2-4-p2-5-decision-20261123.md](../audit/p2-4-p2-5-decision-20261123.md)、[b5-release-gate-20261123.md](../audit/b5-release-gate-20261123.md)、[release-1.2.0-20261124.md](../audit/release-1.2.0-20261124.md)。

> 编号说明：本文 P1/P2/P3 采用 2026-11-22 外部评审编号，不是 ADR 0004 的旧问题编号。

---

## 一、背景与当前状态

2026-11-22 外部评审对当前 HEAD `cc30d1d`（`v1.2.0-alpha.4`）给出“B3→B4 半山腰”的判断。Lead 逐条核对源码后确认：

1. 评审关于 **B4 未完成**的核心判断成立：当前树没有 `guiju/application/`、`ports/`、`adapters/`；[docs/ARCHITECTURE.md:184](../ARCHITECTURE.md#L184) 明确标注 B4 尚未实施；[docs/remediation-2026-11-18.md:473-476](../remediation-2026-11-18.md#L473-L476) 的 W5/W6/W7 与 1.2.0 回退演练仍未勾选。
2. 评审新增的 **P1-1（MCP HTTP 共享 `NativeCurrency.last_outcome`）**是真 bug：HTTP 多线程下一次查询的 stale/from_cache 状态可能被另一请求覆盖。该问题不属于“B4 尚未开始”的抽象债，必须优先修。
3. 评审的 **P1-2/P1-3** 大部分是 ADR 0004 已定义的 B4 收口项，不是 a4 新引入的回归；但 systemd、远端绑定门禁、MCP batch 文件面仍需在 B4 完成。
4. 评审的 **P2-4/P2-5** 是真实产品/运维债；**P2-6 前半段的 403/429 判断错误**，不得按它改代码；**P3** 的测试债成立但部分措辞过度。

## 二、决策

### D1 MCP 查询以 `Outcome` 为唯一事实源

- `McpServer._query`、`_verify` 对 native 后端不得在调用 `backend.lookup` 之后再从 backend 的实例属性读取 `stale/from_cache/cache_age_days`；必须使用 `NativeCurrency.query(code) -> Outcome`，从返回的不可变 `Outcome` 读取全部状态。
- 旧适配器若只有 `lookup`，兼容路径仍视为单线程；不得据此声称 MCP HTTP 已并发安全。B4 迁移后移除该兼容路径。
- 增加 HTTP 并发契约测试：两个请求交错时，每个响应只能携带自己的 `stale/from_cache` 与 `complete` 状态；同时覆盖 `GUIJU_CURRENCY_CACHE=off` 的 memo 路径。
- 该修复完成后才能声称 MCP HTTP 可并发使用；在此之前 hk 的 MCP HTTP 仍视为有已知并发缺陷。

### D2 MCP 结果完整性/备源告警必须透传

- `McpServer._query` / `_verify` 必须把 `Outcome.complete`、`Outcome.incomplete_reason`、`Outcome.fallback_source` 转成机器可读告警，不得只返回行文本。
- `McpServer._batch` 用 `Outcome` 完成查询；批量结果的逐条告警/汇总在 B4 批量契约中补齐，当前先保证并发状态不串。
- `verify` 的 `content[0]` 仍保持严格的 `code\tstatus` 契约；完整性/备源告警放入后续 content 或结构化字段。
- 该要求与 P1-1 同批实施、同批测试，避免只修并发却继续丢失“结果不完整”的语义。

### D3 保留 403/429 的现有语义，不采纳 P2-6 前半段

- 403/429 **命中人机校验 marker**：`ComplianceDenied`，exit 40，立即停止。
- 403/429 **无 marker**：`SourceUnreachable`，exit 20；metadata 的 429 按既有验收做有界重试。
- 该语义由 [tests/test_net_p2_14.py:31-47](../../tests/test_net_p2_14.py#L31-L47) 与 [tests/test_net_metadata_transport.py:28-29](../../tests/test_net_metadata_transport.py#L28-L29) 保护；不得为了迎合“全部 40”的评审建议破坏测试与可用性。
- `CachedCurrency` 的旧 SQLite `currency_cache` 分支可在 B4 后清理，但不得在本次 P1-1 修复中顺手删除包装类或改变 CLI 构造路径。

### D4 B4 仍按 ADR 0004 执行，a4 不算完成

- `1.2.0a4` 是预发布，不代表 B4/B5 完成；发布说明必须继续标注 B4 未实施。
- B4 的最小关闭项：
  1. `guiju/application/` 用例层与 `ports/` Protocol、`adapters/` 落地；
  2. CLI/MCP 共用 application，切断 `mcp -> cli.lookup`；
  3. MCP batch 去文件路径，改内联 `codes[]`，保留兼容窗口；
  4. systemd 的 `User=guiju`、`Group=guiju`、`ProtectSystem=strict`、`ReadWritePaths`、`RestrictAddressFamilies`；
  5. MCP HTTP 远端绑定必须显式 `--allow-remote` + Bearer，不允许只打 warning。
- B5 文档/CI/发布与 1.2.0 回退演练仍按 ADR 0004 关闭条件执行。

### D5 产品债的排序

- P2-4（csres 默认明文导致缓存失效）与 P2-5（M4 `body_sha256`/probe 空转）登记为 B4 后续产品修复；在决定改动前分别补契约测试和可观测性验证。
- `CachedCurrency` 旧分支、测试按批次命名、`test_fix_*` 收敛放在 B4 完成后统一处理；不在当前 P1-1 提交中扩大范围。
- `sources` 对 `compliance` 的直接 import 按 B4 的 ports/adapters 迁移处理，不单独作为紧急修复。

### D6 稳定版门禁

- 当前继续以 `1.2.0a4` 预发布运行；不因本次评审而直接改版本号或发布稳定版。
- 任何稳定 `1.2.0` 必须满足：B4/B5 关闭、全量单测/真实 e2e 通过、构建产物可复核、完成一次 wheel/DB/systemd 回退演练。
- 若只希望把当前 a4 作为公开预发布，无需等待 B4；但对外描述不得称为“架构重构完成版”。

## 三、实施顺序

| 阶段 | 内容 | 验收 |
|---|---|---|
| I1（立即） | P1-1：MCP `Outcome` 改造；MCP 完整性/备源告警；HTTP 并发测试 | 并发交错响应各自状态正确；现有 MCP 契约测试不回归 |
| I2（B4） | application/ports/adapters、CLI/MCP 共用编排、MCP 去文件路径、systemd 加固 | CLI/MCP 同 ref 矩阵；MCP 无任意路径；systemd 单元检查 |
| I3（B4 后） | P2-4 csres 缓存策略、P2-5 M4 probe、`CachedCurrency` 旧分支与测试收敛 | 专项契约测试 + 全量 CI；不破坏 403/429 现有语义 |
| I4（B5） | 文档、CI、发布、1.2.0 回退演练 | ADR 0004 第十节全部关闭条件 |

## 四、关闭条件

- P1-1 的并发复现转为回归测试并通过；`_query/_verify` 不再读取共享 `last_outcome`。
- MCP `query/verify` 能看到 `complete/incomplete_reason/fallback_source` 的机器可读状态；`batch` 的逐条告警在 B4 批量契约关闭。
- B4 的 application/ports/adapters 与 MCP 服务面关闭项全部满足。
- P2-4/P2-5 的处置有明确测试或决策记录。
- ADR 0004 第十节全部关闭，且 1.2.0 回退演练完成。

## 五、明确不做

- 不把 403/429 全部映射为 40；不删除 `CachedCurrency` 包装类；不把“a4 已发版”当成 B4/B5 完成。
- 不在本次 P1-1 修复中顺手重写 application 层；先修正确的并发与结果语义，再按 B4 计划迁移。
- 不操作 NAS；不改变 hk 生产部署，除非另有所有者窗口。
