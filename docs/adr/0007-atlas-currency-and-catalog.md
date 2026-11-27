# ADR 0007 · 工程建设标准图集时效校验、完整目录索引与打包元数据治理

- 状态：**已关闭（1.4.0 已于 2026-11-27 正式发布；D1–D6 为现行契约）**（2026-11-26 立项）
> **实施记录（`main` / `v1.4.0`，2026-11-27 最终）**：S0–S4 全部闭环——双 Python（3.10.21 / 3.13.5）各 1279 项单测、`ruff`、本地 CI、完整联网 e2e 39/39 + 图集真链点查、PEP 639 零警告产物构建与校验、本地 `1.4.0 ↔ 1.3.0` 回退演练、hk 生产 `1.4.0` 升级与 `atlas-catalog.json`（4,252 条）预热及 `1.3.0 ↔ 1.4.0` 真实 systemd 回退/再部署均已通过；未访问 NAS。发版验证与产物事实见 [release-1.4.0-20261127.md](../audit/release-1.4.0-20261127.md) 与 [ACCEPTANCE.md](../ACCEPTANCE.md)。
- 决策人：所有者
- 触发：`v1.3.0` 收口后，需要为 `guiju` 增加工程建设标准图集（国标图集/中南标图集/地方标图集）的权威现行/废止/替代校验与 agent 完整目录索引能力，并提前消除 `pyproject.toml` 的 `SetuptoolsDeprecationWarning`（2027-02-18 截止）。
- 影响：`guiju/net/{endpoints,policy}.py`、`guiju/domain/atlas.py`、`guiju/domain/currency.py`、`guiju/currency/{chinabuilding,registry}.py`、`guiju/store/{atlas,currency}.py`、`guiju/application/{atlas,resolve,pull}.py`、`guiju/cli/`、`guiju/mcp.py`、`pyproject.toml`、`tests/`
- 关系：延续 ADR 0002 / ADR 0004（双平面出站隔离与契约优先）、M3 `shzjw` 单源隔离经验与 [ADR 0006](0006-v1-3-0-architecture-and-mcp-purification.md) 架构终态；本文定义 `v1.4.0`（S0–S4）的正式契约与边界。详细规格见 [spec-1.4.0.md](../spec-1.4.0.md)。

---

## 一、现场探查事实（2026-11-26，只读低频实测）

对住房和城乡建设部唯一归口单位中国建筑标准设计研究院官方平台 **国标电子书库（`https://ebook.chinabuilding.com.cn`）** 实测确认：

1. **合规与可访问性**：标准 HTTPS 可用、`robots.txt` 返回 404，诚实 UA（`guiju/1.3.0 ...`）直接返回 `200 UTF-8 HTML`，无需登录、无验证码/WAF；图集正文为付费版权出版物（仅提供试读与公开目录/状态元数据）。
2. **精准实时检索（`/zbooklib/search` + `detail/show`）**：
   - `/zbooklib/search?SiteID=1&contentType=book&SearchKey=<图集号>` 返回服务端渲染卡片与统计 JSON `var data`（精确计数 + 空结果时 `{"fieldType":[],"showStat":"false"}`）；`advSearchResult` 会按数字分词产生噪音（如 `99Z999` 命中 304 条），不作为主入口；
   - 详情页 `/zbooklib/book/detail/show?SiteID=1&bookID=<id>` 在废止图集上明确给出 `废止日期` 与 `代替标准号`（如 `16G101-1` `bookID=61268` → 废止 `2022-05-01`、`代替标准号：22G101-1`），现行图集给出 `被代替标准号` 与主编单位。
3. **全库《简明目录》（`/zbooklib/conciseCatalogueList?SiteID=1&sublibID=2118&currentSublibInnerCode=0029`）**：
   - 单次 HTTPS GET（约 4.55 MB HTML）包含全站标准图集 **5,592 行 / 4,252 本唯一 `bookID`**（现行 `3,712` 行、废止 `1,880` 行），覆盖国标图集、废止国标图集、中南标图集与地方标图集；
   - 表格直接携带图集号、完整名称（`title` 属性）、`bookID`、现行/废止、`td5`（被代替图集号）与 `td6`（代替图集号）；
   - **零命名空间重叠**：实测 5,592 行中与上海地标（`DG/TJ08` / `DGJ08` / `DBJT08` / `DBTJ08` / `DBJ08` / `DB31`）重合 **0 条**，与 `GB` / `JGJ` / `CJJ` / `JTG` / `DB\d` 重合 **0 条**。
4. **实测编号形态与 3 类串号陷阱**：
   - 覆盖国标图集（`22G101-1`、`09MR603`、`06SS127`、`24CS01-1`、`13CD701-4`、`11SJ937-1（1）`、`DX003～004`、`S901～902`）、中南标（`21ZJ111`）、地方标含 28 个汉字省前缀共计 1,762 条（如 `粤25G/T116`、`京05G69`、`冀00J201`）以及带图集后缀的 `DBJ/T02-81-2013-12D1`；
   - 串号陷阱：① `16G101-1`（已废止）检索首条为 `B16G101-1`（中英文双语版，站点标 `现行`）；② `G101-1` 会撞到 `23G101-11`（答疑图解）；③ `11G101-1` 会撞到 `11G101-1 英文版`。**必须严格精确匹配图集号**。

---

## 二、决策

### D1 严守双平面合规红线，正文下载绝不接入图集

- 图集属版权出版物，只接入只读元数据平面（`MetadataPolicy` + `MetadataClient`）；`BodyPolicy`（仅允许 `*.gov.cn` 正文下载）**一字不动**。
- `guiju pull <图集号>` 识别为图集编号后直接诚实拒绝（`exit 30` 或明确 `hint` 说明受版权保护，仅支持 `which`/MCP 查有效性），**零下载请求**。

### D2 独立纯领域模块 `guiju/domain/atlas.py`（零污染标准编号逻辑）

- 新增纯函数模块 `guiju/domain/atlas.py`，绝不混入 `guiju/currency/codes.py`（标准年份在尾，图集年份在头）：
  - 统一识别国标、中南标、汉字省前缀图集及显式图集后缀 `DBJ/T02-...-\d{2}[A-Z]\d+`；严守上海 `shzjw`（`owns_shanghai_namespace`）与国/行标前缀不被抢占；
  - 带年份编号严格精确匹配（拒绝 `B` 双语版与 `英文版` 后缀伪装），无年份基础号按严格家族匹配（`22G101-1` 匹配 `G101-1`，拒绝 `23G101-11`）；
  - 承载 `/zbooklib/search`、`/zbooklib/book/detail/show` 与 `conciseCatalogueList` 三个纯 HTML 解析器，全量离线黄金夹具覆盖。
- 同步修复 `guiju/application/resolve.py` 的 `ref_is_code`，允许以汉字省简称开头的图集编号（如 `粤25G/T116`）识别为编号而非书名。

### D3 方案 A 原生时效适配器 `guiju/currency/chinabuilding.py`

- 在 `MetadataPolicy` 注册 `ebook.chinabuilding.com.cn` 的 3 条精确 HTTPS 路径：
  - `/zbooklib/search`（默认 2 MB 上限）；
  - `/zbooklib/book/detail/show`（默认 2 MB 上限）；
  - `/zbooklib/conciseCatalogueList`（**显式按端点 8 MB 上限**，因为全库简明目录实测约 4.55 MB）。
- 查询采用**方案 A**（低请求 + 精准替代链）：
  - 先请求 `/zbooklib/search` 获取现行/废止状态、发布日期、实施日期、废止日期与 `bookID`；
  - 仅当精确命中「废止」图集时，再追加 1 次 `detail/show` 提取 `代替标准号`：有替代者时 `Record.status = "superseded"` 且写入新字段 `Record.replaced_by`，无替代者时保持 `"withdrawn"`；受 `1.2s/host` 限速与 `NativeCurrency` 24h TTL / 30d stale 缓存保护（不重复打上游）；
  - 仅使用单源权威适配（仿 M3 `shzjw`），不回落 `csres`。
- `Record` 增量增加 `replaced_by: str = ""`（`as_payload()` 仅在非空时附带或由 `which`/MCP 告警呈现，不破坏既有 5 字段与 MCP `verify` `code\tstatus` 格式）；`DiskCurrencyCache` 向下兼容缺少 `replaced_by` 键的存量 `native-v1-*.json` 缓存文件（不失效 hk 存量预热缓存）。

### D4 完整图集索引、CLI 命令与 MCP 服务面扩展

- **独立索引存储（不动 `catalog.db`）**：`conciseCatalogueList` 同步结果以原子替换写为 `GUIJU_HOME/atlas-catalog.json`（含 `synced_at` 与 ~4,252 条去重记录），保持数据库 `SCHEMA_VERSION = 2` 零变更，绝不污染住建部 `announcement` 表与既有 39/39 端到端契约。
- **CLI 命令与既有入口边界**：
  - 新增 `guiju atlas sync [--if-stale N] [--json]`、`guiju atlas search <关键词> [--status current|withdrawn|all] [--all] [--json]`、`guiju atlas list [--status ...] [--prefix ...] [--limit N] [--all] [--json]`；
  - `guiju which <图集号>` 命中图集时效路由后，显式跳过本地住建部公告模糊匹配，清晰返回 `currency[]`（含状态/日期/替代图集）与 `announcement: null` + 中文说明；
  - `atlas-catalog.json` 若存在且已同步，同时可作为未覆盖省前缀规则的离线已知图集编号补充判别信号。
- **MCP 只读纯净化延续与增强**：
  - `query` / `verify` / `batch` 通过 `NativeCurrency` 原生支持全部图集号；
  - 新增只读工具 **`atlas_search`**（基于只读 `atlas-catalog.json` 按关键词/前缀/状态筛选并返回分页条目，无写盘、不触发网络同步、找不到目录时给出明确的中文提示）；
  - 同步优化 MCP `TOOLS` 描述文案，让 agent 一目了然何时用 `query`/`verify`/`batch`（查国标/行标/上海地标/图集有效期）、`announcement`（住建部/上海正文索引）与 `atlas_search`（检索图集索引清单）。

### D5 打包元数据提前治理（PEP 639）

- 修改 `pyproject.toml`：升级 build requirement 为 `setuptools>=77`，改用 `license = "MIT"` 与 `license-files = ["LICENSE"]`，删除弃用的 `License :: OSI Approved :: MIT License` 分类器，消除 `uv build` 的 `SetuptoolsDeprecationWarning`。

### D6 明确不做

- 不下载或缓存任何图集 PDF / 试读页；
- 不新增退出码，不升数据库 `SCHEMA_VERSION = 3`，不引入第三方运行时依赖；
- 不接入非官方/盗版图集站，不访问 NAS（ADR0048）。

---

## 三、关闭条件（v1.4.0 S4）

- `uv build` 零 `SetuptoolsDeprecationWarning`， wheel METADATA 符合 PEP 639；
- 双 Python（3.10 / 3.13）全量单测（含脱敏离线黄金夹具、串号防错回归、`DiskCurrencyCache` 向后兼容、MCP `atlas_search` 与 AST/边界守卫）通过，`ruff` 与 `scripts/ci.sh` 全绿；
- 联网验收 `scripts/e2e.py --require-currency` 39/39 通过 + 图集真链点查（现行 `22G101-1`、废止替代 `16G101-1 -> 22G101-1`、中南标/省标、`conciseCatalogueList` 同步）通过；
- 完成本地与 hk 生产（`1.3.0 ↔ 1.4.0`）升级与真实回退演练并发布。
