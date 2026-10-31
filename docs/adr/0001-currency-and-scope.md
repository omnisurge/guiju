# ADR 0001 · 废止 index，与 spec-currency 分层协作

- 状态：**已关闭 / closeout**（决策 2026-10-30 · 实施 2026-10-30 · 关闭 2026-10-31）
- 关闭依据：工具已装入 PATH 并完成全量初始化，四条 agent 主路径实测可用（见文末「关闭记录」）
- 决策人：所有者（明确要求"index 必须要放弃，持续维护不可行"）
- 影响：`guiju` 的检索路径、`cite`/`pull` 的前置条件、与 `spec-currency` 的产品边界
- 被取代：第五节「若将来必须统一语言…」与第三节「合产品，不合代码仓」的结论
  → 见 [ADR 0002](0002-merge-into-one-product.md)（二期，未实施）

---

## 背景：index 为什么会成为负担

`index` 原本要做的事是：**在本地维护一份「公告 ⇄ 标准编号」的映射**。

它成为负担的根因不在实现，而在**数据只能自己产**：

| 事实（实测） | 后果 |
|---|---|
| 住建部列表接口无关键词/编号入参，只有分页 | 上游不能按编号检索 |
| 编号只出现在详情页正文里 | 必须逐页抓才能知道编号 |
| `/so/`、`/search/`、`jaas-search-server` 全部 404 | 没有站内检索可借 |
| 全量 3254 条 × 1.2s ≈ 65 分钟 | 每次重建都是小时级 |
| 新公告每天都在增加 | 需要**持续**增量维护 |

结论：这是一份**上游不提供、必须自己抄**的映射表 —— 抄一次不够，要一直抄。

## 决策：用权威源的「编号 → 名称」替代自建映射

`spec-currency`（同目录另一仓，Go，已部署 NAS，被 kb-system / jl-docx 使用）已经用权威源回答同一问题：

```
$ spec-currency query "GB 55037-2022"
GB 55037-2022   current   建筑防火通用规范   2023-06-01   csres
```

于是「编号 → 公告」退化成两步**都不需要预建索引**的操作：

```
编号 ──currency.lookup──► 名称 + 现行状态
                          └── 本地名称检索（domain/search 三层召回）──► 公告 ──► 正文
```

### 实测（本地库 mentions 仅 2 行 / 3254 公告，等于没索引）

| 命令 | 结果 |
|---|---|
| `guiju which GB55037-2022` | currency 返回 `current / 建筑防火通用规范` → 定位公告 #158（2023-01-19） |
| `guiju cite GB55037-2022 5.2.1` | `《建筑防火通用规范》GB 55037-2022 第5.2.1条`，`status: current`，warnings 为空 |
| `guiju which "JGJ 147-2004"` | `superseded`（旧版如实标出） |

**因此：`index` 不再是任何命令的前置条件。**

## 决策细则

1. **`index` 保留但降级**：仅作为「离线 / 批量引用」的可选预热缓存（`scan` 表继续防重复抓取）。它不在主路径上，也不再是里程碑。
2. **时效查询端口化**：`guiju/currency.py` 定义 `Currency` 协议 + 三个适配器
   - `CommandCurrency`：本地 `spec-currency` 二进制（快、可离线）
   - `McpCurrency`：`http://100.66.1.x:18083/mcp`（NAS 常驻，已验证）
   - `NullCurrency`：都没有时静默降级（不猜、不报错、不阻断）
   - 选择顺序由 `GUIJU_CURRENCY` / `GUIJU_CURRENCY_URL` 覆盖
3. **`cite` 增加权威状态校验**：`superseded` / `withdrawn` 进 `warnings`；`--strict` 直接非零退出。`--offline` 跳过。
4. **`pull` 不额外查询**（保持延迟可预期），只在已拿到状态时标注。

## 合规分层（关键，勿混用）

| 层 | 约束 | 谁负责 |
|---|---|---|
| **内容抓取**（正文 PDF） | 只允许 `*.gov.cn`，逐跳校验重定向，无 override | `guiju/compliance.py` |
| **元数据查询**（编号/名称/状态） | 走已审批的 `spec-currency` 服务，不下载正文、不落文件 | `guiju/currency.py` |

**为什么必须分开**：`currency` 的主源 `csres.com` 不是 `*.gov.cn`。若把它直接接进 `compliance` 的白名单里开例外，这个例外迟早会被用来抓正文。分层之后，红线仍然只对着"文件"生效。

## 与 spec-currency 是否合仓？—— 合产品，不合代码仓

### 结论

一个产品面（用户只装 `guiju`，命令面统一），两个仓（`guiju` = 内容，`spec-currency` = 时效），通过端口/服务协作。

### 三条理由

1. **部署生命周期不同**：`spec-currency` 是 NAS 常驻服务，被 kb-system（定时盘点 621 个规范）与 jl-docx（code_refs 核验）依赖；`guiju` 是本地按需 CLI。合成一个二进制，会让 kb 侧被迫跟着 guiju 发版，反之亦然。
2. **合规面必须分离**（见上表）：合库等于把"第三方源"带进"只抓 gov.cn"的仓里，红线会被稀释。
3. **已验证资产不该重写**：两侧的核心都是逆向契约 + 脏活适配（GBK HTML / 附件网关 / 魔数 / 备用源 / 降级标注），重写 = 重新验证一遍，换来的只有"语言统一"——而语言统一在本项目的收益已论证为接近零。**尤其不该重写 spec-currency**：它有线上下游。

### 若将来必须统一语言，方向是明确的

把 **guiju（本地 CLI）移植到 Go**，而不是把 spec-currency 移植到 Python。判据：本地 CLI 重写没有下游风险，服务重写会打断两个消费者。

### 关闭记录（closeout，2026-10-31）

**目标**：agent（当前 DSH 等）能在任意目录直接下载规范并拿到引用串，不需要预建索引。

| 检查项 | 证据 |
|---|---|
| 命令在 PATH | `uv tool install --editable .` → `guiju` / `gj`（`~/.local/bin`），在 `/tmp` 下可直接调用 |
| 目录已初始化 | `guiju sync` → 3254/3254（33 页，≈60s）；`~/.guiju/catalog.db` |
| 编号 → 名称/状态 | `guiju which GB55037-2022 --json` → `current / 建筑防火通用规范 / 公告158 / in_window` |
| 按名称取正文 | `guiju pull 城市给水工程项目建设标准 --json` → 717,954 B PDF，sha256 `043c214e…` |
| 出引用串 | `guiju cite GB55037-2022 5.2.1 --json` → `《建筑防火通用规范》GB 55037-2022 第5.2.1条`，warnings 空 |
| 完整性校验 | `guiju verify --json` → total=1，anomalies=0 |
| 回归 | 85 项单测 OK；`scripts/e2e.py` 38 项全通过（有时效服务）/ 23 项 + 1 skip（无） |

**未纳入本 ADR（留给二期，见 ADR 0002）**：`guiju mcp` 服务面、上海 DG/TJ 等地标源、多源仲裁。

**遗留风险**：编号路径依赖时效服务（NAS/Tailscale）；服务不可达时返回 20 而非 31，agent 应改走名称路径（已写入 AGENTS.md）。

---

## 后续变化（2026-10-31 之后，不改写上面的历史记录）

| 变化 | 对本 ADR 的影响 |
|---|---|
| 上海地标源落地（v0.10.0 / v0.11.0） | 关闭记录里"未纳入本 ADR：上海 DG/TJ 等地标源"已过时 —— 地标现在可查可下，且**状态由源站自己给** |
| 时效查询本地落库（v0.12.0） | 「遗留风险：编号路径依赖时效服务」被削弱：查过的编号在服务中断时仍答得出（结果标 `currency_from_cache`） |
| `cite --strict --offline` 冲突（v0.12.1） | 原本会静默降级成"没做校验的成功引用"，现在按用法错误退出 2 |
| 提示文案清理（v0.12.1） | 代码里不再有任何文案推荐 `guiju index`；新增守卫测试防回潮 |
| **`index` 命令已删除（v0.13.0）** | 本 ADR 原决策是"降级保留为可选预热"。v0.12.0 之后它已无独特用途（时效缓存 + 地标源 + `pull` 顺带解析各自覆盖），所有者于 2026-10-31 决定**删除**：命令文件、帮助与所有推荐文案一并移除，`scan` 表保留（`persist_codes` 仍在写"已解析"标记，删表要升 schema 版本，代价不值） |

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| NAS 单点不可达 | `NullCurrency` 静默降级 → 退回「本地索引/名称检索」路径；`which --offline` 明确表态 |
| TSV 契约漂移 | `parse_tsv` 容错列缺失；契约在两侧文档互链；`tests/test_currency.py` 钉住 |
| 名称匹配错标准 | 名称检索多候选一律 `exit 32`（`AmbiguousTarget`），禁止自动选第一个 |
| 时效服务的 `unknown` 被当成 `current` | 状态只用于**告警**与 `--strict`，不用于"批准引用"；`unknown` 不产生"现行"结论 |

## 实现验收（2026-10-30）

逐条对照，全部落地：

| 决策 | 证据 |
|---|---|
| `index` 降级为可选 | `mentions` 表 0 行的库上，`which`/`cite`/`pull` 按编号全部可用 |
| 时效端口化 + 三适配器 + 环境变量 | `guiju/currency.py`：`Currency`(Protocol) / `CommandCurrency` / `McpCurrency` / `NullCurrency` / `CachedCurrency`；`GUIJU_CURRENCY`、`GUIJU_CURRENCY_URL` 生效 |
| `cite` 权威状态校验 | `cite JGJ 147-2004 --strict` → **exit 32**；`--offline` 跳过 |
| `pull` 不额外查询 | 仅在解析阶段取一次状态，下载过程再无 currency 调用 |
| 合规分层 | 正文仅 `compliance.py`（`*.gov.cn` + 逐跳校验）；`currency.py` 不落文件、不取正文 |
| 不合仓 | 两仓独立，仅通过端口/服务协作（本条已被 ADR 0002 取代，见下） |

风险缓解项均已验证：NAS 不可达 → `NullCurrency` 降级；TSV 契约由 `tests/test_currency.py` 钉住；
名称多候选 → `exit 32`（v0.7.0 起再用"发布角色 + 正文编号核实"加一道闸）；`unknown ≠ current`。

## 遗留（已清）

- ✅ `AGENTS.md` 的 31 含义与提示已改（"先 sync，或确认该标准无公开正文"）。
- ✅ `batch` 盘点的边界已澄清：**状态盘点**仍归 `spec-currency`；guiju 的 `pull --list`
  是**批量取正文**（同一动作跑 N 次），不是重复实现盘点。
