# ADR 0006 · v1.3.0 架构收口、M4 观测闭环与 MCP 纯净化

- 状态：**已批准（现行实施）**（2026-11-26）
- 决策人：所有者
- 触发：`v1.2.0` 发布后按 `docs/spec-1.3.0.md`（Plan B）集中清偿 ADR 0004 / ADR 0005 I3 延后项与过渡兼容层
- 影响：`guiju/net/`、`guiju/sources/`、`guiju/currency/`、`guiju/application/`、`guiju/cli/`、`guiju/mcp.py`、`guiju/store/sqlite.py`、`deploy/` 及 `tests/`
- 关系：承接 ADR 0004（D1–D6 长期契约）与 ADR 0005 I3 排期债；ADR 0001 / 0002 / 0004 / 0005 已关闭，本文记录 1.3.0（S0–S6）确立的代码终态与不可回流边界。详细规格见 [spec-1.3.0.md](../spec-1.3.0.md)。

> **实施记录（`feat/v1.3.0`，2026-11-26）**：S0–S5 已逐步提交（`0c62bc7`、`0b91e97`、`57d60fd`、`eda66c3`、`937ee30`、`efc6b48`、`8019be7`、`62c85dc`），由本版本冻结门禁统一归档；发版验证与产物事实见 [release-1.3.0-20261126.md](../audit/release-1.3.0-20261126.md) 与 [ACCEPTANCE.md](../ACCEPTANCE.md)。

---

## 一、背景

`1.2.0` 完成 `B4` 分层骨架与 `B5` 稳定版发版后，全仓仍保留了若干为降低 `1.2.0` 改动风险而留置的过渡面：
1. M4 改版观测尚未闭环（`body_sha256` 未写入与暴露、`MetadataClient` 无 `probe_sink`、缺少 `guiju sources` 健康视图）；
2. `CachedCurrency` 默认 `native` 分支后留有不可达的 SQLite/进程缓存路径、`Store.search()` 死代码仍在、`McpServer._announcement` 持 `_store_lock` 跨网络调用、`search`/`show` 人读主输出走 `stderr` 会被 `--quiet` 吞掉；
3. `guiju/compliance.py`、`guiju/currency/httpx.py`、`guiju/cli/{lookup,render}.py` 与 `application/` 隐式构造兜底等兼容门面仍在阻隔纯净分层；
4. MCP 服务面仍留有 `batch` 的 `list`/`out` 文件路径模式、`backend.lookup()` 单线程兼容路径以及黑名单式 `_ReadOnlyStore`；
5. 33 个按历史修复批次命名的 `test_fix_*`/`test_b2_*`/`test_b3_*`/`test_w1_*` 测试文件分散，跨文件重复 helper 增多。

---

## 二、决策

### D1 M4 改版观测闭环（S1）

- `Store.record_body_hash(ann_id, digest)` 将详情页 `body_sha256` 落库并返回指纹变化布尔信号；`pull` 成功 JSON 在 HTML 公告页路径追加只读观测字段 `body_sha256` 与 `body_changed`，且页面改版时经 `stderr` 告警。
- `MetadataClient` 统一接入可选 `probe_sink`，与 `BodyClient` 共用 `ProbeRecord` 观测路径。
- 新增 `guiju sources [--probe] [--json]`：默认零联网只读统计目录水位、探测分布与 `body_sha256` 变更次数；仅显式 `--probe` 时按 `1.2s/host` 限速发低频只读探活（`--offline` 明确拦截）。

### D2 冗余清理、MCP 锁收窄与 CLI 输出流对齐（S2）

- 删除 `CachedCurrency` 不可达的进程内 memo 与 SQLite `currency_cache` 读写路径；不再在新建库按需建 `currency_cache` 表，同时保留存量用户库中旧表为无害孤儿表，不迁移不删数据。
- 删除 0 生产调用的 `Store.search()`，检索统一走 `store.catalog_entries() + domain.search.rank()`。
- `McpServer._announcement` 仅在本地 SQLite 读取阶段持有 `_store_lock`，网络 `detail()` 与纯内存选件移出锁外。
- `guiju search` / `guiju show` 人读主结果统一走 `stdout`，提示/警告留在 `stderr`，`--quiet` 不吞主结果，`--json` 契约不变。

### D3 过渡兼容门面退场与显式装配（S3a / S3b / S3c）

- 删除 `guiju/compliance.py`：`sources/{mohurd,shzjw}` 改直连 `guiju/net/{body,metadata}`，保留全部 76 条 target characterization 用例。
- 删除 `guiju/currency/httpx.py`：抽出无 I/O 纯解码模块 `guiju/currency/jsonutil.py`，将 `MetadataPolicy.check_form_url` 与表单主机白名单下沉至 `guiju/net`，7 个时效模块直连 `MetadataClient` + `jsonutil`。
- 删除 `guiju/cli/lookup.py`、`guiju/cli/render.py` 与 `cli/commands/{pull,cite}.py` 的 `sys.modules` alias，移除 `application/` 内隐式 `Store`/`CachedCurrency`/`registry()` 兜底构造，统一由入口 `AppContext` 显式注入。
- `tests/test_net_boundaries.py` 新增 AST 与文件系统守卫，禁止上述门面、绕过 `guiju/net` 或隐式装配回流。

### D4 MCP 服务面彻底纯净化（S4）

- 前置确认下游（`DSH`、`kb-system`、`jl-docx`）均无 `list`/`out` 文件路径依赖后，从 `TOOLS["batch"]` 移除 `list`/`out` 参数，删除 `_safe_mcp_path`、`_mcp_io_root` 及 `/var/lib/guiju/mcp` 相关逻辑；`deploy/guiju-mcp.service` 同步移除 `ExecStartPre=/bin/mkdir -p /var/lib/guiju/mcp`。
- 删除 `_query_result()` 中 `backend.lookup()` 单线程兼容路径，强制所有 MCP 后端提供并发安全的 `query(code) -> Outcome`。
- `_ReadOnlyStore` 由 `_WRITES` 黑名单改造为显式 `_ALLOWED_READS` 只读白名单代理，任何未列属性与写方法一律阻断。

### D5 测试体系按领域收敛（S5）

- 将 33 个 `test_fix_*`/`test_b2_*`/`test_b3_*`/`test_w1_*` 按批次命名回归文件零删减归并入对应领域单测套件（由 82 个缩减为 62 个领域套件），抽离共用 `ShortRead` 至 `tests/net_harness.py`。
- `tests/test_hygiene.py` 新增 `NoBatchFixTestFilesTest`，永久禁止新增按修复批次命名的临时测试文件。

### D6 不可动摇边界与明确不做（延续 S0–S6）

- 维持零第三方运行时依赖、`SCHEMA_VERSION = 2`（双向 `1.2.0 ↔ 1.3.0` 零迁移）、既有 12 个退出码语义、`*.gov.cn`（含上海 9175 策略）每跳重定向校验与 `1.2s/host` 限速；保留 P2-4（csres 明文回退不落缓存）与 P2-6 否定裁定；绝不访问 NAS（ADR0048）。

---

## 三、关闭条件（S6）

- 双 Python（3.10.21 / 3.13.5）全量 **1224 项单测**、`ruff check .`、`scripts/ci.sh` 与联网 `scripts/e2e.py --require-currency` **39/39** 全绿；
- 构建 `1.3.0` wheel/sdist 与 `SHA256SUMS`，完成本地 `1.2.0 ↔ 1.3.0` wheel/DB 回退演练；
- 在所有者确认窗口下完成 hk 生产升级、`deploy/guiju-mcp.service` 同步与真实 systemd 回退/再部署演练后，打 `v1.3.0` tag 并过完整 `scripts/check-release.py --artifacts`。
