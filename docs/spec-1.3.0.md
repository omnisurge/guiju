# guiju v1.3.0 迭代规格（Spec）

- 状态：**已确认按默认执行（2026-11-26，分步实施中）**
- 分支：`feat/v1.3.0`
- 基线：`main` @ `5838b6a`（`v1.2.0` 正式版 `a895734` + 发布后文档与低风险 P3 收口 `5838b6a`）
- 目标版本：`1.3.0`
- 关联文档：[ADR 0004](adr/0004-contract-first-hardening.md) · [ADR 0005](adr/0005-external-review-2026-11-22.md) · [产品架构 v1](ARCHITECTURE.md) · [交接文档](../HANDOFF.md)
- 红线约束：
  - 运行时**零第三方依赖**（`pyproject.toml` `dependencies = []`）；
  - **不新增退出码**（沿用 0 / 1 / 2 / 10 / 20 / 30 / 31 / 32 / 33 / 34 / 40 / 50）；
  - **不升 DB schema v3**（保持 `SCHEMA_VERSION = 2`）；
  - **不触碰 NAS**（遵守 kb-system/ADR0048 外部冻结边界）；
  - **不放松合规护栏**：正文下载仍只允许 `*.gov.cn`（及既有上海 9175 端口策略）且每跳校验重定向；出站仍保持 `1.2s/host` 限速；命中人机校验立即退出 40；**不采纳** P2-6 的“无 marker 403/429 一律 40”建议。

---

## 一、目标与范围

本迭代对应方案 B（直接演进至 `1.3.0`），一次性收口 `ADR 0004` / `ADR 0005 I3` 排期到 `1.2.x/1.3.0` 的非阻塞技术债与过渡兼容面，聚焦五大主题：

1. **开发期门禁解耦（`Unreleased` 态）**：允许在 `feat/v1.3.0` 迭代期间增删/重构测试与文档而不被“1.2.0 精确单测计数 1223”锁死，同时严防未冻结版本误走 `--artifacts` 发布。
2. **M4 改版观测闭环（P2-5 收口）**：落盘详情页 `body_sha256` 并识别页面改版变更；为 `MetadataClient` 补齐 `probe_sink`；落地 `guiju sources [--probe] [--json]` 健康度与探活视图；将页面指纹观测接入 `pull` 结果。
3. **时效 / 存储 / MCP 冗余与锁范围收敛**：清除 `CachedCurrency` 默认路径下已不可达的进程内 memo 与 SQLite `currency_cache` 读写路径；删除生产无调用方的 `Store.search()`；缩窄 `McpServer._announcement` 的 `_store_lock` 持锁区间（不把网络 I/O 放进锁内）；统一 `search` / `show` 人读输出流语义，避免 `--quiet` 吞掉主结果。
4. **1.2.0 过渡兼容门面退场**：让 `sources` 与 `currency` 直接使用 `guiju/net`、`cli` 直接使用 `guiju/application`，删除 `guiju/compliance.py`、`guiju/currency/httpx.py`、`guiju/cli/lookup.py` 兼容门面以及 `application/` 内的隐式 `Store` / `CachedCurrency` 构造兜底。
5. **MCP 服务面彻底纯净化与测试体系收敛**：在确认上游消费者切换完毕后，删除 MCP `batch` 的 `list`/`out` 文件路径面与 `backend.lookup()` 单线程兼容路径；`_ReadOnlyStore` 收敛为白名单只读代理；将 27 个按修复批次命名的 `tests/test_fix_*.py` 按功能域收敛。

---

## 二、代码影响盘点（基线实测）

| 主题 | 涉及生产代码（`guiju/`） | 涉及测试 / 脚本 / 文档（基线） | 备注 |
|---|---|---|---|
| **M4 改版观测**（`body_sha256`、`probe_sink`、`sources`） | `guiju/store/schema.sql`（`announcement.body_sha256`、`probe` 表已存在）、`guiju/store/sqlite.py`、`guiju/sources/mohurd.py`（`detail()`）、`guiju/net/metadata.py`（`MetadataClient` 当前无 `probe_sink`）、`guiju/application/{pull,resolve}.py`；新增 `guiju/application/sources.py`、`guiju/cli/commands/sources.py`、`guiju/cli/__init__.py` | `guiju/ports/store.py`、`guiju/ports/source.py`、`tests/test_net_metadata_transport.py`、`tests/test_store.py`、`docs/ARCHITECTURE.md` | `schema.sql` 第 14 行已预留 `body_sha256`，无需升 schema 版本 |
| **`CachedCurrency` / `currency_cache`** | `guiju/currency/__init__.py`（`native` 分支提前 `return`，后段 `_cache` / `_save` / `_load` 不可达）、`guiju/cli/__init__.py`、`guiju/cli/commands/which.py`、`guiju/adapters/currency/__init__.py`、`guiju/application/{cite,pull,resolve}.py`、`guiju/store/sqlite.py`（`_ensure_additions` / `cache_currency` / `cached_currency`）、`guiju/mcp.py` | 17 个测试文件（`test_currency.py`、`test_currency_cache.py`、`test_currency_boundary.py`、`test_currency_w3_contract.py`、`test_fix_*` 等） | 已存在库的 `currency_cache` 表作为孤儿表留存，不迁移不删用户文件 |
| **`Store.search()` 死代码** | `guiju/store/sqlite.py`（第 287 行定义；0 处生产调用） | 3 个测试文件：`tests/test_fix_w2_store_20261122.py`、`tests/test_b3_w4_store.py`、`tests/test_b3_baseline_20261122.py` | `search` 命令已统一走 `store.catalog_entries() + domain.search.rank()` |
| **`guiju/compliance.py` 退场** | `guiju/sources/mohurd.py`（4 处：`catalog_page`/`detail`/`download`/`download_to`）、`guiju/sources/shzjw.py`（3 处：`_fetch`/`download_to`/`download`） | 20 个测试/夹具 Python 文件（含 `tests/char_baseline_20261121.py`、`tests/char_baseline_harness.py`、`tests/char_case_recorder.py`、`tests/test_net_boundaries.py` 等）及 1 份 JSON 夹具 `tests/fixtures/net/characterization-20261121.json` | 迁移时需显式处理 B2 C0 characterization harness 锚点 |
| **`guiju/currency/httpx.py` 退场** | `guiju/currency/{ccsn,dbba,stdsamr,jtst,csres,shzjw,registry}.py`（7 个模块） | 11 个测试/夹具 Python 文件（`tests/net_harness.py`、`tests/char_baseline_harness.py` 及 9 个 `test_*.py`） | 纯 JSON 解码辅助需抽出独立模块（如 `guiju/currency/jsonutil.py`） |
| **`cli/lookup.py` 等 shim 与隐式兜底** | `guiju/cli/{lookup.py,render.py,commands/show.py,commands/which.py,commands/pull.py,commands/cite.py}`、`guiju/application/{__init__,context,resolve,pull,cite}.py` | 10 个测试文件引用 `cli.lookup` / `cli.render` / `cli.commands.{pull,cite}` | 删除未注入时的隐式 `Store()` / `CachedCurrency()` 兜底 |
| **MCP 纯净化 & 锁收窄 & 输出流** | `guiju/mcp.py`（`TOOLS` 中 `batch` 的 `list`/`out`、`_safe_mcp_path`、`_batch`、`_query_result` 的 `backend.lookup()` 回退、`_ReadOnlyStore` `_WRITES` 黑名单、`_announcement` `_store_lock` 包裹 `source.detail()`）、`guiju/cli/commands/{search,show}.py` | `tests/test_fix_a1_mcp.py`、`tests/test_fix_w5_mcp_20261122.py`、`tests/test_mcp.py`、`README.md`、`AGENTS.md`、`deploy/README.md` | 删除 `list`/`out` 前需先完成上游消费者核验 |
| **测试按批次命名收敛** | 无生产代码改动 | `tests/` 下共 81 个 `test*.py`，其中 27 个为 `tests/test_fix_*.py`（全量 1223 项单测） | 按领域归并，保证有效断言数不降 |

---

## 三、核心不变量与兼容契约

### 1. 保持不变的契约
- **退出码集合封闭**：不新增退出码，所有错误仍映射到现有语义码；
- **JSON 字段只增不改**：已有成功体与失败体字段名、含义、嵌套结构不变，新增观测字段（如 `body_sha256`、`body_changed`）仅作增量追加；
- **数据库兼容**：保持 `SCHEMA_VERSION = 2`；存量库中的 `currency_cache` 表保留为无害孤儿表，不做破坏性 `DROP TABLE`，新库可保留表定义或保持向后可读（读旧库不报错）；
- **安全与限速不降级**：`RedirectGuard`、per-host 1.2s 限速、40 立即停止、shzjw 9175 端口白名单与 HMAC 令牌策略保持原样。

### 2. v1.3.0 显式破坏性变更清单（按 ADR 0004/0005 预先登记）
1. **删除 MCP `batch` 的 `list` / `out` 文件路径参数**（仅支持内联 `codes[]` 数组），同步删除 `_safe_mcp_path` 与 `GUIJU_HOME/mcp` 文件读写路径；
2. **删除内部兼容门面模块**：`guiju/compliance.py`、`guiju/currency/httpx.py`、`guiju/cli/lookup.py`、`guiju/cli/render.py` 兼容 alias；
3. **删除 `Store.search()` 方法**（统一使用 `catalog_entries() + domain.search.rank()`）；
4. **收紧 `McpServer` 后端契约**：删除 `_query_result` 中的 `backend.lookup()` 单线程兼容路径，强制要求 `query(code) -> Outcome`；
5. **收紧 `application/` 入口注入契约**：删除未注入 `store` / `currency` 时的隐式构造兜底（统一由入口 `AppContext` 显式装配）。

### 3. 每阶段交付纪律（延续 ADR 0004 D6）
- 每个子阶段必须包含：**不变量陈述 + 同类全仓扫描 + 跨面契约测试 + 文档同步**；
- 每个阶段结束必须满足：Python 3.10 与 3.13 全量单测通过、`ruff check .` 通过、`scripts/ci.sh` 全绿、零 `ResourceWarning`、`git diff --check` 干净。

---

## 四、分步实施计划（S0–S6）

### S0 · 迭代基线与开发期门禁
- **目标**：解除 `1.2.0` 静态测试计数绑定对开发的阻塞，同时保留对正式发版的严格校验。
- **改动**：
  1. `CHANGELOG.md` 顶部新增 `## Unreleased` 小节，记录本迭代目标摘要（不改下方 `## 1.2.0` 及历史条目）；
  2. `scripts/check-release.py`：当 `CHANGELOG.md` 顶部为 `## Unreleased` 时，`--docs-only` 改为“开发态”检查（仍校验版本格式、README/HANDOFF/INSTALL/ACCEPTANCE 既有发版段完整、POSIX-only、工作流接线及 `actual_count > 0`，放行“当前发现测试数 == 1.2.0 发布文档计数”的强等式）；`--artifacts` 模式**严格禁止** `## Unreleased` 存在，防止未定版误发；
  3. `tests/test_hygiene.py`：同步识别 `## Unreleased` 状态，并补充单测覆盖 `Unreleased` 下 `--docs-only` 放行与 `--artifacts` 拒绝两类行为。
- **验收与门禁**：`ruff`、`check-release.py --docs-only`、`scripts/ci.sh`、3.10/3.13 全量单测通过；验证 `--artifacts` 在 `Unreleased` 下返回非零。

### S1 · M4 改版观测闭环（P2-5 收口）
- **目标**：打通 `body_sha256`、`MetadataClient` 探针台账与 `guiju sources` 健康视图，让站点改版可在 24 小时内通过观测信号定位。
- **前置**：S0。
- **改动**：
  1. **详情页指纹记录（`body_sha256`）**：
     - `guiju/store/sqlite.py` 新增 `record_body_hash(ann_id: int, digest: str) -> bool`（首次写入或与旧值相同返回 `False`，非空旧值发生变化时更新并返回 `True`）；同步暴露进 `guiju/ports/store.py`；
     - `guiju/sources/mohurd.py` 与详情抓取链路在不破坏 `(html, text)` 既有签名的前提下提供字节摘要（或由 `application/resolve.py` / `pull.py` 对 `html.encode("utf-8")` 统一计算稳定指纹并写库）；
     - `guiju/application/pull.py`：在成功 JSON 中追加 `body_sha256`（HTML 公告页为详情页 sha256，API 源无详情页时为 `None` 或省略需按契约固定）与 `body_changed`（`bool`），同时通过 `render.warn` 在 `body_changed=True` 时提示“公告详情页内容指纹发生变化”。
  2. **`MetadataClient` 探针台账**：
     - `guiju/net/metadata.py`：`MetadataClient.__init__` 新增可选 `probe_sink=None`（默认 no-op），在 200 成功与 HTTP/传输/截断失败路径发射 `ProbeRecord`，与 `BodyClient` 保持同构；
     - 在时效查询入口按需接入（不破坏只读语义：MCP 只读 Store 不写主库 `probe` 表，仅在可写 CLI 上下文或内存观测点记录）。
  3. **`guiju sources` 命令**：
     - 新增 `guiju/application/sources.py` 与 `guiju/cli/commands/sources.py`，在 `guiju/cli/__init__.py` 注册子命令 `sources [--probe] [--json]`；
     - 默认（不带 `--probe`）：**零网络请求**，只读汇总各源目录水位（`source_meta` / `catalog_age_days`）、`probe` 成功/失败分布、近期失败条目及 `body_sha256` 变更次数；
     - 带 `--probe`：受 `1.2s/host` 限速约束对注册源执行轻量只读探活（受 `--offline` 拦截返回提示/退出 2）。
- **验收与门禁**：`body_sha256` 变更检测单测、`MetadataClient` `probe_sink` 成功/失败回归、`guiju sources --json`（离线/带探活）契约测试、CLI 与 `StorePort` 契约测试、3.10/3.13 全量 + `scripts/ci.sh` 全绿。

### S2 · 时效 / 存储 / MCP 冗余清理与锁收窄
- **目标**：消除默认不可达缓存路径、无用查询方法、MCP 锁内网络 I/O 与 `--quiet` 吞主输出问题。
- **前置**：S0（与 S1 无强文件冲突，建议 S1 后顺行）。
- **改动**：
  1. **`CachedCurrency` 死路径清理**：
     - `guiju/currency/__init__.py`：删除 `CachedCurrency` 中跳过 `native` 后已不可达的 `_cache` 进程 memo 与 `_save` / `_load` SQLite `currency_cache` 读写路径（原生时效由 `NativeCurrency` + `DiskCurrencyCache` 独家管理 TTL 与 30 天降级）；评估并决定是把 `CachedCurrency` 精简为薄适配还是统一由 `default_currency()` 返回 `NativeCurrency`/`NullCurrency`；
     - `guiju/store/sqlite.py`：停止在 `_ensure_additions` 中为新库按需补建 `currency_cache`，标记 `cache_currency` / `cached_currency` 退场；存量库里的旧 `currency_cache` 表保留为无害孤儿表，不删用户数据；
     - 清理或迁移 17 个引用 `CachedCurrency`/`currency_cache` 的测试文件（旧 SQLite `currency_cache` 专属测试替换为 `DiskCurrencyCache` 或显式清理）。
  2. **`Store.search()` 清理**：
     - 删除 `guiju/store/sqlite.py:287` 的 `Store.search()`；
     - 将 `tests/test_fix_w2_store_20261122.py`、`tests/test_b3_w4_store.py`、`tests/test_b3_baseline_20261122.py` 中的 3 处直接调用迁移到 `store.catalog_entries() + domain.search.rank()`（或等价表查询断言 LIKE 转义与按公告置信度）。
  3. **`McpServer._announcement` 缩窄 `_store_lock`**：
     - `guiju/mcp.py`：仅在访问只读 SQLite `self._ann_store()`（`resolve(..., allow_network=False)`）期间持有 `self._store_lock`，释放锁后再执行 `source.detail(ann["url"])` 网络请求与纯内存 `attachments`/`pick`，避免单次慢网络抓取阻塞其他并发 `announcement` 请求。
  4. **`search` / `show` 人读输出流统一**：
     - `guiju/cli/commands/search.py`、`guiju/cli/commands/show.py`：人读模式主结果改走 `stdout`（与 `cite` 一致），仅诊断提示/警告走 `stderr`（`render.note`/`warn`）；`--quiet` 只静音提示而不吞掉检索/详情主结果；`--json` 输出保持不变。
- **验收与门禁**：`McpServer` 并发 `announcement`（模拟慢 `detail()` 不阻塞另一 `announcement`）测试、`search`/`show` `--quiet` 仍输出主结果测试、`CachedCurrency` 与 `Store.search()` 清理后全量 3.10/3.13 + `scripts/ci.sh` 全绿。

### S3 · 兼容门面与隐式构造兜底退场（拆 S3a / S3b / S3c）
- **目标**：彻底移除 1.2.0 过渡期兼容层，使架构图 `cli / mcp → application → domain / ports → adapters → net` 成为代码唯一事实。
- **前置**：S0–S2 完成。
- **S3a：`guiju/compliance.py` 退场**：
  1. `guiju/sources/mohurd.py`、`guiju/sources/shzjw.py` 改用注入或构造 `guiju/net/body.BodyClient` 与 `guiju/net/metadata.MetadataClient`（允许通过测试注入 client/transport）；
  2. 处理 B2 C0 baseline harness（`tests/char_baseline_20261121.py`、`tests/char_baseline_harness.py`、`tests/char_case_recorder.py`）对 `compliance` 的依赖：将 `test_char_target_20261121.py` 锚定到 `BodyClient`，评估并归档/迁移旧 `compliance` baseline harness；将 20 个依赖 `compliance` 的测试文件切到 `guiju.net`（`BodyClient`/`BodyPolicy`/`Throttle`）；
  3. 删除 `guiju/compliance.py`，在 `tests/test_net_boundaries.py` 中增加“全仓不得存在 `compliance.py` 或旧门面回流”的 AST 守卫。
- **S3b：`guiju/currency/httpx.py` 退场**：
  1. 新增纯 JSON 工具模块 `guiju/currency/jsonutil.py`（承载 `decode_json`、`json_rows`、`json_string`、`json_int`、`json_flag`，无任何网络 I/O）；
  2. `guiju/currency/{ccsn,dbba,stdsamr,jtst,csres,shzjw,registry}.py` 改直接依赖 `guiju/net/metadata.MetadataClient` 与 `jsonutil`；
  3. 迁移 11 个依赖 `guiju.currency.httpx` 的测试/harness 文件到 `MetadataClient` / `jsonutil`；
  4. 删除 `guiju/currency/httpx.py`，更新 `tests/test_net_boundaries.py` 与 `tests/test_currency_httpx.py`（重命名为 `test_net_metadata_client.py` 或并入现有 net 测试）。
- **S3c：`cli/lookup.py` 等 shim 与 `application/` 隐式构造兜底删除**：
  1. `guiju/cli/commands/show.py`、`which.py` 直接 import `guiju.application.resolve` 与 `guiju.report`；删除 `guiju/cli/lookup.py` 与 `guiju/cli/render.py`；
  2. 清理 `guiju/application/{__init__,resolve,cite,pull}.py` 中未传 `store` / `currency` / `sources` 时的隐式 `Store()` / `CachedCurrency()` / `registry()` 兜底构造，强制经 `AppContext` 或显式参数传入；
  3. 迁移 10 个直接引用 `cli.lookup` / `cli.render` / `cli.commands.{pull,cite}` 内部符号的测试文件到 `application.*` 或完整 CLI `main()` 入口。
- **验收与门禁**：`grep` 全仓零 `compliance` / `currency.httpx` / `cli.lookup` 残留；`test_net_boundaries.py` 加强通过；双 Python 全量单测 + `scripts/ci.sh` 全绿。

### S4 · MCP 服务面彻底纯净化（破坏性变更门）
- **目标**：兑现 ADR 0004 D5 与 ADR 0005 D1 的 1.3.0 终态承诺，彻底剥离 MCP 服务面的文件读写能力与旧单线程后端兜底。
- **前置**：S3 完成，且**必须完成上游消费者（`kb-system`、`jl-docx`、`DSH`）核验**，确认均已切到 `codes[]` 内联参数且不再传递 `list` / `out`。
- **改动**：
  1. `guiju/mcp.py`：
     - 从 `TOOLS["batch"]["inputSchema"]` 移除 `list` 与 `out` 属性，仅保留 `codes`（`required: ["codes"]`）；
     - 删除 `_safe_mcp_path`、`_batch` 中的文件读写逻辑及 `GUIJU_HOME/mcp` 目录创建/扫描；若调用方仍传 `list`/`out`，明确报错提示“1.3.0 已移除文件路径模式，请改用内联 codes[]”；
     - 删除 `_query_result` 中的 `backend.lookup()` 单线程兼容路径（MCP 后端强制实现 `query(code) -> Outcome`）；
     - `_ReadOnlyStore` 从 `_WRITES` 黑名单改写为显式只读白名单代理（仅允许转发已列名的只读查询方法及 `close`，任何未列属性/写方法一律拒绝）。
  2. 更新 `tests/test_fix_a1_mcp.py`、`tests/test_fix_w5_mcp_20261122.py`、`tests/test_mcp.py`（测试替身升级为提供 `query()` -> `Outcome`；路径拒绝测试改为断言 `list`/`out` 已废弃拒收；新增 `_ReadOnlyStore` 白名单阻断写方法/直访 `.db` 的契约测试）。
  3. 同步更新 `README.md`、`AGENTS.md`、`deploy/README.md` 中的 MCP `batch` 说明，删除 `1.1.1-1.2.0` 过渡期描述。
- **验收与门禁**：MCP 并发/HTTP 安全/白名单只读/内联 batch 矩阵测试、双 Python 全量单测 + `scripts/ci.sh` 全绿。

### S5 · 测试体系收敛
- **目标**：将 27 个按修复批次命名的 `tests/test_fix_*.py` 按功能域整理进主题测试模块，消除跨文件重复 helper 与脆弱测试耦合。
- **前置**：S1–S4 功能与接口全部定型。
- **建议归并映射（分批迁移，每批核对断言数不降）**：
  - 网络 / 出站 / 下载续传：`test_fix_a1_download.py`、`test_fix_a1_currency_throttle.py`、`test_fix_20261112.py`（网络部分）、`test_b2_blockfix_20261121.py`、`test_w1_net_20261122.py` → 归入 `tests/test_net_body.py` / `tests/test_net_metadata.py` / `tests/test_resume.py`；
  - 存储 / 同步 / 自保鲜：`test_fix_a1_autosync.py`、`test_fix_a1_store_error.py`、`test_fix_b1_storage.py`、`test_fix_b1_sync_status.py`、`test_fix_w2_store_20261122.py`、`test_fix_w2_sync_20261122.py`、`test_b3_baseline_20261122.py`、`test_b3_w2_sync_status.py`、`test_b3_w4_store.py` → 归入 `tests/test_store.py` / `tests/test_autosync.py` / `tests/test_sync_status.py`；
  - 时效 / 适配器：`test_fix_low_currency.py`、`test_fix_med_currency.py`、`test_fix_w3_currency_20261122.py` → 归入 `tests/test_currency*.py`；
  - 领域 / 解析 / pull / cite：`test_fix_20261112_lookup_contract_errors.py`、`test_fix_a1_batch.py`、`test_fix_b1_cite.py`、`test_fix_b1_codes.py`、`test_fix_b1_multi_name.py`、`test_fix_p3_cite_contract.py`、`test_fix_w3_lookup_20261122.py`、`test_fix_w4_cli_20261122.py`、`test_fix_low_20261112.py`、`test_fix_low_core.py`、`test_fix_med_20261112.py`、`test_fix_med_core.py`、`test_b3_w3_lookup.py` → 归入 `tests/test_codes.py`、`tests/test_cite.py`、`tests/test_lookup.py`、`tests/test_pull.py`、`tests/test_batch.py`；
  - MCP：`test_fix_20261112_mcp_crash.py`、`test_fix_a1_mcp.py`、`test_fix_w5_mcp_20261122.py` → 归入 `tests/test_mcp.py`（或拆 `test_mcp_protocol.py` + `test_mcp_http.py`）。
- **门禁**：迁移前后 `unittest` 覆盖的场景与总断言只增不减；零 `ResourceWarning`；双 Python 全量 + `scripts/ci.sh` 全绿。

### S6 · v1.3.0 冻结、发版与回退演练
- **目标**：完成 `v1.3.0` 正式发版门禁、hk 生产升级及回退演练。
- **前置**：S0–S5 全部完成，并完成独立 review。
- **步骤**：
  1. `guiju/_version.py` 升为 `"1.3.0"`；`CHANGELOG.md` 将 `## Unreleased` 替换为 `## 1.3.0 — <日期>`（写明破坏性变更与升级指引）；
  2. 同步 `README.md`、`HANDOFF.md`、`INSTALL.md`、`docs/ACCEPTANCE.md`、`docs/ARCHITECTURE.md`、`docs/releases/v1.3.0.md`、`docs/audit/release-1.3.0-<yyyymmdd>.md` 与新 `ADR 0006`；
  3. 跑全量双版本单测（3.10 / 3.13）、`ruff check .`、`scripts/ci.sh`、`python3 scripts/e2e.py --require-currency`（联网验收）；
  4. 构建 `dist/`（wheel/sdist + `SHA256SUMS`），执行 `python3 scripts/check-release.py --artifacts --skip-tag`，打 `v1.3.0` tag 后再过完整 `--artifacts`；
  5. 执行本地 wheel/DB 回退演练（`1.2.0 ↔ 1.3.0`）与所有者窗口下的 hk `1.2.0 ↔ 1.3.0` 升级/健康检查/真实回退再部署；
  6. 收尾清理历史分支（经所有者确认后删除已合并旧分支）。

---

## 五、需要确认的 4 个设计默认项

以下 4 个设计点已给出推荐默认，请在确认后按默认推进；如有异议请指出具体项：

| # | 议题 | 推荐默认 | 备选方案 |
|---|---|---|---|
| 1 | **MCP `batch` `list`/`out` 删除时机（S4）** | S0–S3 先行；**S4 开工前核验** `kb-system` / `jl-docx` / `DSH` 是否已全切 `codes[]`，确认无消费者后再删除 `list`/`out` | 立即在 S4 强制删除（若下游未切将直接报错） |
| 2 | **`pull` 成功 JSON 新增 `body_sha256` / `body_changed`（S1）** | **新增**（仅追加字段：HTML 公告页附带 `body_sha256` 与 `body_changed`，不改原有字段与退出码） | 仅写入数据库并在 `guiju sources` 暴露，不加进 `pull` JSON |
| 3 | **`guiju sources` 命令形态（S1）** | **`guiju sources [--probe] [--json]`**（默认只读本地 DB 统计，零联网；显式 `--probe` 才发低频只读探活，对应 [ARCHITECTURE.md](ARCHITECTURE.md#L319)） | 仅并入 `guiju status --probe`，不新增 `sources` 子命令 |
| 4 | **`search` / `show` 人读输出流（S2）** | **人读主结果走 `stdout`**，提示/警告走 `stderr`，`--quiet` 只静音提示不吞主结果（与 `cite` 一致；`--json` 不受影响） | 保持人读结果走 `stderr`（`--quiet` 继续静音） |

---

## 六、风险与回退策略

| 风险 | 影响阶段 | 缓解与回退策略 |
|---|---|---|
| **删除 `compliance.py` 破坏 B2 C0 baseline 门禁** | S3a | 在 S3a 先把 `test_char_target_20261121.py` 等特征测试改锚到 `BodyClient`，确认 `scripts/ci.sh` 在 `CI_C0_BASELINE` 兼容或不留悬空引用后，再删 `compliance.py`；单独 commit 便于一键 `git revert` |
| **MCP 消费者仍依赖 `batch` 的 `list`/`out`** | S4 | S4 严格放在 S3 之后，开工前逐一检查消费者配置/代码仓库或日志；若仍有遗留，先协助上游切 `codes[]` 或单独推迟 S4 |
| **`CachedCurrency` 清理破坏既有测试替身/patch 面** | S2 / S3c | S2 先只移除不可达的 SQLite/进程缓存与写路径，保留测试里仍需要的轻量注入协议；到 S3c 再统一切换为 `AppContext` 注入，避免同一 commit 同时改两层 |
| **测试归并（27 → 主题文件）意外漏掉断言** | S5 | 按域分批合并，每批执行前后对比各领域测试方法与子断言覆盖清单，并保留 `tests/test_hygiene.py` 与 `test_net_boundaries.py` 等所有不变量门禁不变 |
| **阶段间中途回退** | S0–S6 | 每一子阶段（S0, S1, S2, S3a, S3b, S3c, S4, S5, S6）保持独立小步提交，每步全量门禁全绿后再进下一步；不修改 `v1.2.0` 已有发布产物和 tag |

---

## 七、明确不做

- 不升数据库 `SCHEMA_VERSION` 到 v3；不删除用户存量库里已有的 `currency_cache` 表或历史数据。
- 不推翻 **P2-4**（`csres` 明文回退保持 `complete=False`、不落磁盘缓存的安全默认）。
- 不采纳 **P2-6** 前半段（无 marker 的 403/429 仍维持 20 `SourceUnreachable`）。
- 不引入任何第三方运行时依赖。
- 不实现跨进程文件锁限速（沿用 ADR 0004 决策 #8）。
- 不操作 NAS（遵守 kb-system/ADR0048）。

---

## 八、进度跟踪清单

- [x] **S0**：迭代基线与开发期门禁（`Unreleased`、`check-release.py`、`test_hygiene.py`）
- [x] **S1**：M4 改版观测闭环（`body_sha256`、`MetadataClient.probe_sink`、`guiju sources`）
- [ ] **S2**：冗余清理与锁收窄（`CachedCurrency` 死路径、`Store.search()`、`_announcement` 锁、`search`/`show` 输出流）
- [ ] **S3a**：`guiju/compliance.py` 退场（`sources` 直连 `guiju/net`）
- [ ] **S3b**：`guiju/currency/httpx.py` 退场（`currency` 直连 `guiju/net/metadata.py` + `jsonutil`）
- [ ] **S3c**：`cli/lookup.py` 等 shim 与 `application/` 隐式构造兜底退场
- [ ] **S4**：MCP 服务面纯净化（上游核验后删 `batch` `list`/`out`、删 `lookup()` 兼容路径、`_ReadOnlyStore` 白名单化）
- [ ] **S5**：测试体系收敛（27 个 `test_fix_*.py` 按领域归并）
- [ ] **S6**：`v1.3.0` 冻结、联网 e2e、产物构建、本地与 hk 回退演练及 ADR 0006 归档
