# B4 应用层 / MCP/IPC 收口（已通过独立双审）

> 日期：2026-11-23
> 基线：`refactor/root-cause` @ `cc30d1d` + 未提交工作树
> 状态：**B4 COMPLETE（reviewer-domain-mcp APPROVE + reviewer-cli APPROVE，2026-11-23 rev2）；B5/1.2.0 稳定版与回退演练仍待执行。**

## 目标

按 ADR 0004 D3/D5 和 ADR 0005 I2，把 CLI/MCP 共用的用例编排收进 `application/`，
用 `ports/` Protocol 划边界，把实现放回 `adapters/`；同时关闭 MCP 的任意文件路径、
非回环裸奔和 systemd 以 root 运行的三个服务面风险。

## 本次已落地

| 项 | 改动 | 验证 |
|---|---|---|
| 应用层 | `guiju/application/{__init__,context,resolve,status,sync,cite,pull}.py` 承接 resolve/status/sync/pull/cite 编排；`ports/{store,currency,source}.py`；`adapters/{store,currency,sources}/`。CLI 只保留 parser/alias/render；rev2 已修 pull/cite 的 AppContext 贯通与 `_finalize_body` 回归。 | 全量 1223×2、ruff、CI、docs-only、离线 e2e、在线 e2e 39/39 通过；双审 APPROVE |
| 解析迁移 | `cli/lookup.py` 的 695 行解析编排移到 `application.resolve`；CLI 仅保留兼容 shim | MCP/which/lookup 聚焦 118 项通过 |
| 状态投影 | `cli/commands/status.py` 只做渲染，payload 由 `application.status.build_status` 构造 | status 聚焦测试通过 |
| 同步用例 | `application.sync.run_sync` 承接 `--if-stale`、锁、crawl 注入；CLI 注入 `crawl`/`safe_sync` 以保持既有 patch 面 | sync/status 聚焦 50 项通过 |
| 时效校验 | 新增 `application.cite.check_currency`，集中 `CurrencyCheck` 的类型化结论；CLI `_check_currency` 改为兼容委托 | cite 聚焦 72 项通过 |
| AppContext | CLI 入口构造 `args.app_context`；`from_args()` 统一 store/currency/out_dir/offline/home | 入口/聚焦测试通过 |
| MCP batch | 新增内联 `codes[]`；`list/out` 标记 deprecated 并保留受限兼容；每项缓存/完整性 state 汇总为第二条 JSON 告警 | MCP 聚焦 66 项通过 |
| MCP HTTP | 非回环绑定必须 `--allow-remote` + `GUIJU_MCP_BEARER_TOKEN`；Handler 校验 `Authorization: Bearer` | 非回环门禁 + 真实 401/200 集成测试通过 |
| systemd | `deploy/guiju-mcp.service` 增加 `User/Group=guiju`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/guiju`、`ProtectHome`、`RestrictAddressFamilies`、空 `CapabilityBoundingSet` | 单元文件静态检查；未部署 hk |

## rev2 已关闭 / 剩余边界

1. `pull` 完整编排已迁入 `guiju/application/pull.py`；`cli/commands/pull.py` 仅保留 parser + alias；rev2 修掉 `_finalize_body` 命名/未定义回归。
2. `cite` 完整编排已迁入 `guiju/application/cite.py`；`cli/commands/cite.py` 仅保留 parser + alias。
3. `AppContext` 已贯通 pull/cite/which/show/status/mcp；application 仍保留旧直接调用（测试/嵌入）的局部兼容桥，B5/1.3 再删。
4. MCP batch 的 `list/out` 只是 deprecated，尚未删除；ADR D5 允许 1.1.1-1.2.0 过渡，1.3.0 才移除。
5. MCP HTTP 本机与非回环 401/200 已测；仍未在 hk 真实 Caddy 链路上验证。
6. `deploy/README.md` 已同步用户/组创建、目录属主、restart 与 `systemctl show` 核验步骤；未在 hk 执行。
7. 全量矩阵 3.13/3.10 各 1223/1223、离线 e2e 4/4、`scripts/ci.sh` 全绿；两名 reviewer 已 APPROVE，B4 关闭。

## 验证记录（本轮已执行）

```
python3 -m unittest tests.test_fix_low_core tests.test_fix_w5_mcp_20261122 tests.test_mcp \
  tests.test_fix_w2_sync_20261122 tests.test_fix_b1_sync_status tests.test_fix_w3_lookup_20261122 \
  tests.test_b3_w3_lookup
# Ran 118 tests ... OK

python3 -m unittest tests.test_fix_low_core tests.test_fix_w5_mcp_20261122 tests.test_mcp
# Ran 66 tests ... OK

uv tool run --offline ruff check guiju/application guiju/ports guiju/adapters guiju/mcp.py \
  guiju/sources/__init__.py guiju/cli/lookup.py guiju/cli/commands/status.py \
  guiju/cli/commands/sync.py guiju/cli/commands/mcp.py tests/test_fix_low_core.py \
  tests/test_mcp.py tests/test_fix_b1_multi_name.py
# All checks passed!

python3 -m unittest discover -s tests -t .        # 3.13：Ran 1223 ... OK
/Users/panhaowei/.local/bin/python3.10 -m unittest discover -s tests -t .  # 3.10：Ran 1223 ... OK
python3 scripts/e2e.py --offline                   # 4/4
scripts/ci.sh                                      # 本地 CI 全绿
python3 scripts/check-release.py --docs-only      # PASS（44 项检查）
```

## 2026-11-23 独立复核结论（rev1 历史，已处理）

- reviewer-domain-mcp：**REJECT（按 B4 关闭口径）**。必须修复 `application` 对 concrete store/currency/source 的依赖、`pull`/`cite` 主流程未迁、`cli.lookup` patch 兼容和部署 restart 步骤；本轮已修 status/sync/resolve 的端口依赖、lookup 真 alias、部署 restart/组创建，并补 batch/Bearer 测试，但 `pull`/`cite` 主流程仍在 CLI，B4 仍不能放行。
- reviewer-cli：**REJECT（按 B4/ADR 0004 D4 口径）**。指出 ports 未接线、AppContext eager currency、MCP 测试缺口；本轮已将 AppContext 改为惰性、补 codes+40/out/非法/Outcome 告警与非回环/CLI 测试，并扩 StorePort，但完整 `pull`/`cite` 编排、adapter 注入链仍待完成。
- 结论：**当前工作树只能作为 I2 中间骨架；不得标 B4 完成、不得据此发 1.2.0 稳定版。**

## 2026-11-23 rev2 实施记录（已通过双审）

- `pull` 完整编排已迁入 `guiju/application/pull.py`（664 行）；`cli/commands/pull.py` 改为真 alias，旧 patch 点仍作用于同一模块对象。
- `cite` 完整编排已迁入 `guiju/application/cite.py`；`cli/commands/cite.py` 改为真 alias；`check_currency` 保留类型化合同。
- `AppContext` 新增 `sources` 注入；CLI 入口为 pull/cite/which 构造一次 `CachedCurrency` 并注入，status/show/mcp 读取 `ctx.sources`/`ctx.currency`；application 仅保留旧直接调用（测试/嵌入）的局部兼容桥。
- `guiju/domain/currency.py` 承载 `Record`/`QueryCancelled`；`ports.currency.CurrencyPort` 不再依赖 `currency.model` 具体模块；CLI 入口 Store 从 `adapters.store.sqlite` 取。
- `guiju/mcp.py` 的 `McpServer` 改依赖 `CurrencyPort`/domain `Record`，不再顶层 import `NativeCurrency`；`announcement` 继续只读 Store，batch `list/out` 仍是 ADR D5 允许的 1.1.1-1.2.0 过渡面。
- `deploy/guiju-mcp.service` 与 `deploy/README.md` 保持 User/Group/ProtectSystem/ReadWritePaths/RestrictAddressFamilies 与 restart/show 核验。
- P2-4/P2-5 按 ADR 0005 D5 写了决策记录 `docs/audit/p2-4-p2-5-decision-20261123.md`，未伪称已修复。
- 验证（本机 2026-11-23）：`python3 -m unittest discover -s tests -t .` → **1223 tests OK**（3.13）；`/Users/panhaowei/.local/bin/python3.10 -m unittest discover -s tests -t .` → **1223 tests OK**；`ruff check .` → All checks passed；`scripts/ci.sh` → 全绿；`scripts/check-release.py --docs-only` → PASS；离线 e2e 4/4。
- **复审状态（2026-11-23 rev2）**：reviewer-domain-mcp **APPROVE**（实测 pull/cite 的 AppContext 贯通、ports 方法、systemd 静态检查；render 代理与时序兼容 import 明确放行，列作 B5 清理）；reviewer-cli **APPROVE**（全量 1223×2、聚焦 119、旧 patch 面仍生效、无假迁移）。两名 reviewer 均已 APPROVE，B4 关闭。

## 回退

本轮没有 commit、没有部署 hk、没有改 NAS。回退只需丢弃工作树改动；
已存在的 `1.2.0a4` 发布物和 hk 线上服务不受影响。

## 下一步（B4 复审 APPROVE 后）

- 把本轮工作树提交到 `refactor/root-cause`，按 B5 执行文档/CI/发布与回退演练。
- P2-4/P2-5 按 `docs/audit/p2-4-p2-5-decision-20261123.md` 进入后续专项，不阻塞 B4。
- 1.2.0 稳定版另走所有者闸门；hk 生产部署不在本工作树自动执行。
