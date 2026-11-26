# guiju v1.4.0 迭代规格（Spec）

- 状态：**已批准按默认执行（2026-11-26，分步实施中）**
- 分支：`feat/v1.4.0`
- 基线：`main` @ `61ea2e4`（`v1.3.0` 正式版）
- 目标版本：`1.4.0`
- 关联文档：[ADR 0007](adr/0007-atlas-currency-and-catalog.md) · [ADR 0006](adr/0006-v1-3-0-architecture-and-mcp-purification.md) · [产品架构 v1](ARCHITECTURE.md) · [交接文档](../HANDOFF.md)
- 红线约束：
  - 运行时**零第三方依赖**（`pyproject.toml` `dependencies = []`）；
  - **不新增退出码**，**不升 DB schema v3**（保持 `SCHEMA_VERSION = 2`）；
  - **绝不下载图集正文**：`BodyPolicy` 保持仅允许 `*.gov.cn`；图集仅接入 `MetadataPolicy` + `MetadataClient`（1.2s/host 限速、禁跨源重定向、禁附件响应）；
  - **绝不触碰 NAS**（kb-system/ADR0048）。

---

## 一、目标与范围

本迭代按 [ADR 0007](adr/0007-atlas-currency-and-catalog.md) 为 `guiju` 接入官方 **国标电子书库（`https://ebook.chinabuilding.com.cn`）** 的标准图集时效校验与完整目录索引能力，并提前治理打包元数据弃用警告：

1. **图集有效期与替代关系校验**：支持国标图集、中南标图集、地方标图集（含 28 个汉字省前缀如 `粤25G/T116` 与 `DBJ/T02-...-12D1`）；采用**方案 A**（`/zbooklib/search` + 废止条目追加 1 次 `detail/show` 获取 `代替标准号`），通过 `NativeCurrency` 缓存与既有 `which` / `guiju mcp`（`query` / `verify` / `batch`）统一服务。
2. **完整图集目录索引（CLI + MCP）**：通过 `/zbooklib/conciseCatalogueList?SiteID=1&sublibID=2118&currentSublibInnerCode=0029` 单次请求同步全站 ~4,252 本唯一图集（~5,592 行）至独立快照 `GUIJU_HOME/atlas-catalog.json`；提供 `guiju atlas {sync|search|list}` 与 MCP 只读工具 `atlas_search`，并在 MCP 工具描述中清晰指导 agent 联合检索图集、国标/行标与上海地标。
3. **严格防串号与路由互斥**：隔离 `B` 双语版（如 `B16G101-1`）、`英文版` 后缀及同族兄弟子号（如 `23G101-11` vs `G101-1`），严守上海 `shzjw` 与 `GB/JGJ/DB` 路由优先级，修复 `resolve.ref_is_code` 对汉字省前缀图集号的判定，并对图集 `which`/`pull` 给出准确边界提示。
4. **PEP 639 打包治理**：`pyproject.toml` 切至 `setuptools>=77`、`license = "MIT"` 与 `license-files = ["LICENSE"]`，消除 `SetuptoolsDeprecationWarning`。

---

## 二、架构落位与兼容契约

| 主题 | 涉及模块 | 兼容与隔离契约 |
|---|---|---|
| **网络元数据出口** | `guiju/net/endpoints.py`、`guiju/net/policy.py`、`guiju/net/metadata.py` | 仅新增 `ebook.chinabuilding.com.cn` 的 `/zbooklib/search`（2 MB）、`/zbooklib/book/detail/show`（2 MB）与 `/zbooklib/conciseCatalogueList`（按端点 8 MB 上限）；`BodyPolicy` 零修改 |
| **纯领域解析与匹配** | 新增 `guiju/domain/atlas.py` | 与 `domain/codes.py` / `currency/codes.py` 彻底隔离；承接图集号识别、严格匹配及 `search`/`detail`/`conciseCatalogueList` 解析 |
| **值对象与磁盘缓存** | `guiju/domain/currency.py`、`guiju/store/currency.py` | `Record` 增量新增 `replaced_by: str = ""`；`DiskCurrencyCache` 向下兼容缺失 `replaced_by` 的存量 `native-v1-*.json`；MCP `verify` `code\tstatus` 第一段契约不变 |
| **时效适配器与路由** | 新增 `guiju/currency/chinabuilding.py`、`guiju/currency/registry.py`、`guiju/application/resolve.py` | 单源 `ChinaBuildingCurrency` + `code_filter=is_atlas_code`；`ref_is_code` 识别汉字省前缀图集号；`which` 对图集跳过住建部公告检索，`pull` 诚实返回 `exit 30` |
| **独立目录索引与 CLI/MCP** | 新增 `guiju/store/atlas.py`、`guiju/application/atlas.py`、`guiju/cli/commands/atlas.py`；更新 `guiju/cli/__init__.py`、`guiju/mcp.py` | `GUIJU_HOME/atlas-catalog.json` 原子落盘，不动 `catalog.db`；MCP 新增只读 `atlas_search`（不落盘、不发网络同步请求）并完善工具说明 |
| **打包配置** | `pyproject.toml` | `setuptools>=77` + PEP 639 `license` / `license-files` |

---

## 三、分步实施计划（S0–S4）

### S0 · 迭代基线、ADR 0007、PEP 639 与离线黄金夹具
- **改动**：
  1. 切出 `feat/v1.4.0` 分支，写入 `docs/adr/0007-atlas-currency-and-catalog.md`、`docs/spec-1.4.0.md`、`CHANGELOG.md` 顶部 `## Unreleased` 及文档索引更新；
  2. 更新 `pyproject.toml` 为 PEP 639（消除 `SetuptoolsDeprecationWarning`）；
  3. 采集并脱敏裁剪离线黄金 HTML 夹具至 `tests/fixtures/atlas/`（涵盖 `22G101-1` 现行、`B16G101-1`/`16G101-1` 串号陷阱、`G101-1` 与 `23G101-11`/`英文版` 撞号、`99Z999` 权威空、`61268`/`153493` 详情替代链、以及 `conciseCatalogueList` 多前缀代表性样本）。
- **门禁**：`uv build` 无弃用警告且 wheel METADATA 含 PEP 639 字段；`--docs-only` 通过且 `--artifacts` 在 `Unreleased` 下正确失败；双 Python 全量、ruff 与 `scripts/ci.sh` 全绿。

### S1 · 纯领域层 `guiju/domain/atlas.py` 与离线单测
- **改动**：
  1. 实现图集编号规范化、识别与分级匹配（严格区分国标/中南标/28 省前缀/合订本/`DBJ/T02-...-12D1`，严守与 `shzjw` 互斥，隔离 `B` 双语与 `英文版` 后缀）；
  2. 实现 `parse_search_page`、`parse_detail_page`、`parse_concise_catalogue` 三个纯解析器；
  3. 新增 `tests/test_atlas_domain.py`，基于 `tests/fixtures/atlas/` 锁定全量解析与串号/互斥回归。
- **门禁**：新增离线测试、AST/边界、ruff、双 Python 全量与 `scripts/ci.sh` 全绿。

### S2 · `chinabuilding` 时效适配器、端点策略、方案 A 替代链与缓存兼容
- **改动**：
  1. `guiju/net/{endpoints,policy,metadata}.py`：配置 `ebook.chinabuilding.com.cn` 3 路径与按端点字节上限（8 MB 仅限 `conciseCatalogueList`，不放开其他端点）；
  2. `guiju/domain/currency.py` 与 `guiju/store/currency.py`：`Record` 增 `replaced_by`，保留旧版 `native-v1-*.json` 缓存兼容读取；
  3. `guiju/currency/chinabuilding.py` 与 `registry.py`：实现 `ChinaBuildingCurrency`（列表页 + 精确废止追查 1 次详情页 `代替标准号`）、接入 `DefaultRegistry`、支持 `probe_sink`；
  4. 新增 `tests/test_currency_chinabuilding.py` 并更新缓存/端点契约测试（内存 `HTTPResponse` transport 验证）。
- **门禁**：`tests/test_net_boundaries.py` 边界不变、缓存旧版重放测试通过、双 Python + `ci.sh` 全绿。

### S3 · `atlas-catalog.json`、CLI `guiju atlas`、`which/pull` 边界与 MCP `atlas_search`
- **改动**：
  1. 新增 `guiju/store/atlas.py`（只读写 `GUIJU_HOME/atlas-catalog.json`，原子替换，兼容缺失/损坏降级提示）与 `guiju/application/atlas.py`；
  2. 新增 `guiju/cli/commands/atlas.py`（`sync [--if-stale N]` / `search` / `list`，尊重 `--json` / `--quiet` / `--offline` / `GUIJU_HOME`）；
  3. 更新 `guiju/application/resolve.py`、`guiju/cli/commands/which.py`、`guiju/application/pull.py`：正确识别汉字省前缀图集号，`which` 对图集展示状态/日期/`replaced_by` 且跳过住建部公告检索，`pull` 对图集直接诚实退出 `30`；
  4. 更新 `guiju/mcp.py`（并在 `application/` 只读装配下透出）：新增只读工具 `atlas_search`（只读 `atlas-catalog.json`，绝不写盘或联网）、优化 `TOOLS` 描述以明确国标/行标/上海地标/图集的统一入口，在 `query`/`verify`/`batch` 输出中透传 `replaced_by` 替代信息（不破坏 `verify` 第一段 `code\tstatus`）；
  5. 补齐 `tests/test_atlas_cli.py`、`tests/test_mcp.py` 与 AST 防回流测试。
- **门禁**：双 Python 全量、ruff、CI 全绿。

### S4 · `v1.4.0` 冻结、真链验收、产物构建、回退演练与发布
- **改动**：冻结 `guiju/_version.py = "1.4.0"`、同步全部发布文档（含 `README.md` / `AGENTS.md` / `HANDOFF.md` / `INSTALL.md` / `ARCHITECTURE.md` / `CHANGELOG.md` / `ACCEPTANCE.md` / `release-1.4.0-*.md` / `deploy/README.md`）、关闭 ADR 0007。
- **门禁**：双 Python 3.10/3.13、`ci.sh`、联网 `scripts/e2e.py --require-currency` 39/39 + 图集真链点查、PEP 639 零警告打包、`check-release.py --artifacts`、本地 `1.3.0 ↔ 1.4.0` 与 hk 升级/真实 systemd 回退/再部署（hk 同步执行一次 `guiju atlas sync` 预热只读图集索引），经所有者确认后发布打 tag。

---

## 四、进度跟踪清单

- [x] **S0**：分支 `feat/v1.4.0`、ADR 0007、`spec-1.4.0.md`、`Unreleased` 门禁、`pyproject.toml` PEP 639 与离线黄金夹具
- [x] **S1**：`guiju/domain/atlas.py` 与全量离线解析/匹配单测
- [x] **S2**：`chinabuilding` 原生时效适配器（方案 A）、端点上限与缓存兼容
- [x] **S3**：`guiju atlas` 索引命令、`which/pull` 图集边界与 MCP `atlas_search`
- [ ] **S4**：`v1.4.0` 冻结、真链验收、产物构建、本地与 hk 升级/回退演练及发布
