# 变更记录

## 0.5.0 — 2026-10-30（把产品跑顺：全命令面验收 + 6 个真缺陷）

### 新增

- `scripts/e2e.py`：**31 项端到端验收**（目录全量对账 / 检索 / 编号→权威状态→公告 / 下载 / 幂等 / 溯源 / 引用 / 退出码 / stdout 纯净性），可重复执行
- `docs/ACCEPTANCE.md`：验收记录与已知边界

### 修复（都是端到端跑出来的，单测抓不到）

1. **`cite` 按名称/公告ID 直接 31**：现在会现场解析一次详情页（1 个请求）补编号，三种入口都能出引用串
2. **`version --json` 缺 `ok` 字段**：输出形状与其它命令不一致
3. **非法条款号落到 exit 1**：新增 `BadUsage` → `exit 2`，并把校验提前到解析之前（否则错误信息被"解析失败"抢先）
4. **"上游有名称但本地无公告"落到 exit 1**：改为 `31`（可修复：先 sync），不是程序缺陷
5. **上游名称装饰词导致召回 0**：`老年人照料设施建筑设计标准（2026年局部修订）` 在住建部公告里并不存在 → 检索前剥离括号补充；英文版记录不参与定位
6. **同名公告（原版 + 局部修订）无法区分**：改用**版本生效日窗口**这一不猜的原则——某版本的正文公告必然发布在其实施日之前。实测 `JGJ/T450-2018` 正确落到 2018 原版公告，而非 2026 修订公告

## 0.4.0 — 2026-10-30（废止 index：接入 spec-currency 权威时效）

**核心变化：`index` 不再是任何命令的前置条件**（见 `docs/adr/0001-currency-and-scope.md`）。

### 新增

- `guiju/currency.py`：时效端口 + 三适配器（`CommandCurrency` 本地二进制 / `McpCurrency` NAS HTTP / `NullCurrency` 静默降级），`GUIJU_CURRENCY`、`GUIJU_CURRENCY_URL` 可覆盖
- `guiju which <编号|名称>`：一条命令说清「权威状态 + 本地公告 + 有无正文」——index 的替代入口
- `cite` 增加权威状态校验：`superseded`/`withdrawn` 进 warnings，`--strict` 直接失败；`--offline` 跳过
- `pull` 在已获得状态时标注 `status`（不额外发请求）
- 解析链新增「编号 → 权威名称 → 本地公告」，本地无索引也能直达

### 变更

- `resolve()` 支持**规范名**入参（含书名号），多候选抛 `AmbiguousTarget`（exit 32）
- `index` 降级为可选的离线/批量预热缓存，从里程碑中移除
- exit 31 的语义与提示改写（不再提示"先跑 index"）

### 实测（本地 mentions 仅 2 行 / 3254 公告，等于无索引）

```
guiju which GB55037-2022   → current / 建筑防火通用规范 / 公告 #158
guiju cite GB55037-2022 5.2.1 → 《建筑防火通用规范》GB 55037-2022 第5.2.1条（status=current）
guiju which "JGJ 147-2004" → superseded
```

### 合规分层

`compliance.py` 仍只管**内容抓取**（`*.gov.cn` 逐跳校验）；元数据查询走 `currency` 端口，
不下载正文、不落文件。两者刻意不合并（详见 ADR）。

## 0.3.0 — 2026-10-30（检索：从"子串匹配"升级为"三层召回 + 相关性排序"）

### 修复（都是实测到的用户可见问题）

- **带书名号零命中**：`search 《防火》` 原返回 0 条（`LIKE '%《防火》%'`）。现自动剥离 `《》〈〉「」【】` 等包裹符 → 43 条
- **中文插字丢召回**：`防火规范` 原只命中 21 条，漏掉《建筑防火通用规范》。现引入二元组模糊召回 → 26 条
- **有严格结果就丢掉模糊结果**：半吊子的两段式导致召回被截断；改为严格/模糊合并、严格优先排序
- **结果无序**：原按日期倒序，"最相关"被埋在后面的页。现按相关性（名称精确 > 前缀 > 包含 > 编号 > 模糊）排序，同分按名称短者优先
- **看不到全貌**：新增 `--all`，并在 JSON 里给出 `total` / `count` / `truncated` / `mode` / `match` / `score`
- **`--status` 伪过滤**：该字段几乎恒为 `unknown`，移除以免误导
- 检索改为"把 3k 条目录读进内存打分"（3274 行 × 十来个字段，毫秒级），不再需要给 SQLite 建全文索引

### 检索语义（`guiju/domain/search.py`，纯函数，13 项单测）

| 层 | 触发条件 | 例 |
|---|---|---|
| `and` | 各关键词子串全部命中 | `防火` → 43 |
| `fuzzy` | 二元组覆盖"允许差 1 个" | `防火规范` → 26 |
| `chars` | 召回 <3 条时放宽为字符全覆盖（只看名称+编号） | `给排水` → 27 |

## 0.2.0 — 2026-10-30（M0：能站住）

按 `docs/audit-2026-10-30.md` 的裁定执行重构，行为契约与数据模型同时升级。

### 修复（每条都有实测证据）

- **合规红线**：白名单现在对**每一次重定向**生效（原实现只在首请求校验，`urllib` 自动跟随 30x 可绕过）。`compliance._GuardedRedirect`
- **抓错文件**：附件裁定删除"兜底取第一个"；实测 2022-01-18 英文版公告曾选中无关文件。现在多候选即 `33`，英文版公告直接 `not_applicable`
- **引用错版本**：编号引入 `role`（`primary`/`target_of_revision`/`superseded_ref`/`mentioned`）与原文 `evidence`；同号旧版本自动降级，`cite` 不再可能引用被代替版本
- **`guiju --version --json` 退出 2**：全局开关改为 `default=SUPPRESS` + 父 parser，任意位置可用
- **未预期异常裸奔**：统一兜底为 `exit=1` + 结构化 JSON，traceback 只进 stderr
- **`provenance` 依赖绝对路径**：改为按 sha256 反查，文件移动后仍可溯源
- **`--out` 默认写当前目录**：默认改为 `~/.guiju/library`
- **限速不分 host**：改为按 host 计间隔
- **反爬检测阈值**：只对 text/html·json 且 <2MB 的响应抽头部 8KB 扫描

### 数据模型 v2（旧库自动备份重建）

- `announcement` / `edition` / `mention`(role+evidence) / `scan` / `artifact`(sha256 内容寻址) / `probe` / `corpus_meta`
- `PRAGMA user_version` 管版本；检测到旧库即备份为 `catalog.db.legacy-*` 后重建
- 允许"无编号但有正文"（39% 的公告确实没有编号）

### 新增

- `guiju status`：库状态、覆盖度、可得性口径
- `guiju cite --strict`：有未核实项即 `32`
- 退出码 `32`（编号歧义）/ `33`（附件歧义）
- 可得性分档进代码：`domain/availability.py`，`FULL_TEXT_SINCE_YEAR = 2014`；所有失败体都带 `availability` 与中文 `hint`
- `tests/` 33 项零依赖单测（编号角色 / 附件裁定 / 引用串 / 合规闸门 / 目录解析 / 可得性）

### 结构

- 拆分为 `cli/ commands/ domain/ store/ sources/` + `errors.py`（错误→退出码单一映射）
- 新增 `README.md`、`AGENTS.md`、`LICENSE`、`.gitignore`
- `data/标准公告全目录.csv` → `tests/fixtures/catalog-2026-10-30.csv`（黄金快照，线上零差异）
- 版本号单一来源：`guiju/_version.py` + `pyproject` 动态读取
