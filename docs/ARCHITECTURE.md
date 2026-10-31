# 规矩 guiju · 产品架构 v1

> 定稿：2026-10-30。取代 `docs/建筑规范CLI-蓝图.md` 的第三～七节。
> 文件裁定见 `docs/audit-2026-10-30.md`。

---

## 〇、一页结论

| 问题 | 裁定 |
|---|---|
| 产品是什么 | 一个**只做三件事**的命令行工具：把政府已公开的工程建设标准**目录**同步到本地、把**正文**按编号取到本地、把**引用串**交给监理文书。 |
| 实现语言 | **Python 3.10+（CI 基线 3.11），仅标准库**，不引任何第三方运行时依赖。 |
| 产品界面语言 | **中文文案 + 英文机器字段**：命令、帮助、人读输出全中文；`--json` 的 key 全英文。主路径是 JSON。 |
| 架构形态 | 六边形：`cli → application → domain`，`ports` 定义边界，`adapters` 实现；`compliance` 是唯一出站闸门。 |
| 数据主体 | **公告**（现实存在物）与**版本号**（引用主体）分层，M:N + 角色；绝不把编号当主键。 |
| 最核心竞争力 | 不是"能抓"，是**"不抓错"**与**"改版后 24 小时修好"**。 |

---

## 一、产品边界（一句话 + 三条不变量）

**一句话**：以规范为准绳，让依据随手可取。工具只搬运与校验，不生产、不解释、不承诺条文内容。

**三条不变量（比任何功能都重要）**

1. **不抓错优先于抓不到。** 附件歧义、编号歧义、名称冲突 —— 一律显式失败（非零退出 + 结构化 hint），绝不用"取第一个"兜底。抓不到是空，抓错是事故。
2. **不猜编号。** 任何编号必须来自页面原文可引用的证据（`evidence` 字段）。判不出角色就标 `ambiguous`，`cite --strict` 直接非零退出。
3. **单一出站闸门。** 全仓只允许 `guiju.adapters.fetch.Guard.fetch()` 发网络请求；白名单对**每一次跳转**生效，无 override 开关。

---

## 二、实现语言决策

### 结论

```
Python 3.10+（CI 用 3.11），运行时零第三方依赖
分发：uv tool install / pipx（单命令安装，自带 venv，不污染系统 Python）
```

### 对比（按本项目真实权重打分，不按语言热度）

| 维度（权重） | Python | Node/TypeScript | Go | Rust |
|---|---|---|---|---|
| 领域匹配：HTTP + HTML 正则 + SQLite + PDF 魔数（30%） | ★★★★★ 标准库全覆盖 | ★★★☆ 需 cheerio/undici 等 | ★★★★ | ★★★ |
| **零依赖分发**（25%） | ★★★★★ 纯标准库可跑，agent 侧 `python -m` 即用 | ★★☆ node_modules 数十 MB | ★★★★★ 单二进制 | ★★★★★ |
| 适配器迭代速度（20%） | ★★★★★ 改正则即改行为 | ★★★★ | ★★★ 编译往返 | ★★ 借用检查拖慢脏活 |
| 与现有工具链协同（15%） | ★★★★ opencli/uv 都可用 | ★★★★★ 与 DSH 套件同栈 | ★★ | ★ |
| 长跑/并发抓取（10%） | ★★★ GIL，但限速 1.2s 下并发无意义 | ★★★★ | ★★★★★ | ★★★★★ |

**决定性理由是第 2 行与第 3 行**：这个产品的性能瓶颈是**别人服务器的限速**（1.2s/请求），不是本地 CPU；而它的主要成本是**站点改版后的适配器修补频率**。在这两条上 Python 是纯粹的最优解，Go/Rust 的单一二进制优势被"迭代慢 + 生态少"抵消。

**Python 的唯一真实劣势**（MCP 与 TS 生态）用一层薄壳解决：

```
guiju mcp        # stdio JSON-RPC，手写 ~150 行，不需要 MCP SDK
```

对外契约是 **JSON + 退出码**，与实现语言完全解耦。未来若要把 MCP 层换成 TS，业务层一行不用改 —— 这正是分层架构要买的东西。

### 为什么不是 Rust（反方论证，2026-10-30 定稿）

"Rust 天然严谨能减少 bug" 这句话本身成立，但**对错了缺陷类别**。用本仓实测的十条缺陷逐条问"换成 Rust 会不会自动拦住"：

| # | 实测缺陷 | Rust 拦得住？ | 原因 |
|---|---|---|---|
| 1 | 重定向绕过白名单 | ❌ | `reqwest` 默认同样跟随 30x 重定向（`Policy::limited`）。两条语言都得手写跳转校验 |
| 2 | 多附件时兜底取第一个 → 抓错文件 | ❌ | 这是业务启发式错误。`alive[0]` 在 Rust 里是**合法代码** |
| 3 | 新/旧编号角色混淆 | 🟡 仅当建模为 enum | `enum Role{Primary,SupersededRef,...}` + 穷尽 match 能强制；但若写成 `role: String` 则毫无帮助。Python 用 `StrEnum` + 严格 mypy 可得 80% |
| 4 | `--version --json` 报参数错误 | ❌ | clap 同样要求 `global = true`，不声明就报一样的错。那是 API 设计问题 |
| 5 | `EXIT_OK if results else EXIT_OK` 死分支 | ✅ | clippy 默认开 `if_same_then_else`（ruff 同能力需另配规则） |
| 6 | `--out` 默认写当前目录 | ❌ | 设计取舍 |
| 7 | provenance 依赖绝对路径 | ❌ | 设计取舍 |
| 8 | 响应体大小阈值漏检 | ❌ | 逻辑阈值 |
| 9 | 限速不分 host | ❌ | 状态建模，两条语言都得自己写 |
| 10 | 无 schema 迁移机制 | ❌ | 工程纪律 |

**自动拦住的：1 条（#5）；条件性拦住：1 条（#3，且取决于你是否真的建了 enum）；无关：8 条。**
反向看：Rust 的主场（内存安全、数据竞争、alias 检查）**在本项目是空集** —— 单线程、无 `unsafe`、无共享可变状态的 I/O 胶水，Python 从来不会在这些地方出错。

Rust 真正的收益只有一条，而它有牙齿：**"让非法状态无法表示"** —— 用 enum 消灭"新/旧编号不分"这类建模错误，用 `Result` 消灭"忽略错误"。这是 #3 那条事故级缺陷的正解。

但它的代价在本项目上是反向的：

| 代价 | 对本项目的具体影响 |
|---|---|
| 依赖面爆炸 | HTTP+TLS+JSON+SQLite 需要 `hyper/tokio/rustls/rusqlite(含 C)/serde` 数百个 crate 的审计面。一个**合规工具**把"依赖面最小"换成"内存安全"，是拿好不容易守住的干净换了用不上的属性 —— Python 标准库零依赖在这里是**合规资产** |
| 迭代慢 | 主要成本是站点改版后的正则修补。借用检查在"改一行正则"上的收益为零，编译往返与类型体操是纯税。而 M4 指标是"改版后 24 小时修好" |
| 工期 | 1085 行 → 约 2.2–2.8k 行（错误类型、DTO、trait impl），M0–M4 从 4–5 周变成 10–13 周，且这期间**一条数据都没多拿到** |
| 选型判据不满足 | 见下 |

**选择 Rust 的四条判据（本项目全部不满足）**：① 需长期无人值守 + 真并发；② 需分发单二进制给第三方；③ 需解析不可信二进制格式（此时才需要内存安全）；④ 团队已用 Rust。
本产品：①限速下并发无意义；②`uv tool install` 已够；③只认魔数、不解析 PDF 结构；④团队 Python。

**结论**：换语言不是这个项目的 bug 杠杆。真正的杠杆是 ①单一出站闸门（消除 #1 的类别）②禁止"取第一个"兜底（消除 #2）③enum 化角色 + 穷尽处理（消除 #3）④对 3254 条真实页面跑黄金语料回归。这四件事与语言无关，Python 全做得到。

**留的后门**：`ports/adapters` 边界已允许将来把某个热模块（例如 PDF 文本抽取）写成 Rust 侧车，通过 subprocess + JSON 调用，业务层零改动。若日后真要换语言，这个边界就是重组装点，不是重写点。

### 语言层面的硬性约束

- `requires-python = ">=3.10"`（当前声明 3.9 与代码风格不匹配，见 audit #缺陷表）。
- 禁止引入第三方运行时依赖；PDF 页数/魔数解析自己写（`%PDF-` / `D0CF11E0` / `PK\x03\x04` / `<!doctype`）。
- dev 依赖：`pytest`、`ruff`、`mypy`，不进运行时。
- 中文处理：所有读写显式 `encoding="utf-8"`；URL 参数必须 percent-encode（HANDOFF 已踩：`UnicodeEncodeError`）。
- 类型注解：`from __future__ import annotations` 保留；公开函数必须有注解（mypy 基础档）。

---

## 三、产品语言（文案与命名）

**双层语言**：机器要稳，人要懂。

| 层 | 语言 | 规则 |
|---|---|---|
| 命令 / 参数 | 拼音 `guiju`（别名 `gj`）＋英文参数 `--json --db --out` | 参数名英文（agent 生态惯例），命令名拼音（唯一性好，PyPI/npm 双空已实测） |
| `--json` 输出 | key 全英文 snake_case，value 可中文 | 与语言实现解耦，可生成 TS 类型 |
| 人读输出 / 帮助 / 报错 | **全中文**，用监理行业语汇 | 面向监理，不面向程序员："正文"不叫"payload" |
| 退出码 | 数字语义化，稳定不扩 | agent 依据它自主决策 |

### 术语表（必须统一，禁止同义混用）

| 术语 | 定义 | 反例 |
|---|---|---|
| 规范 / 标准 | 同义，正文里优先用"标准"，引用串里用"规范"（符合监理习惯） | 不要写"法规""文件" |
| 编号 | `GB 55037-2022` 这类登记号 | 不要叫"代码""code 号" |
| 版本 | 编号 + 年，引用与下载的最小单位 | 不要叫"条目" |
| 公告 | 政府网页（`announcement`） | 不要叫"页面""文章" |
| 附件 / 正文 | 公告页上的文件 vs 文件里的规范内容 | — |
| 现行 / 废止 / 被代替 | 状态三态 + `unknown` | 不要用"有效""过期" |
| 局部修订 | 只改条文的公告，正文编号可能被改写（`JGJ450` → `JGJ/T450`） | — |
| 条款号 | `5.2.1`，规范化后存储（去"第""条"） | — |

### 输出契约

```
stdout : 只有 JSON（--json）或唯一结果（cite 人读模式）
stderr : 全部日志、进度、警告
退出码 : 见第八节；2（argparse 用法错误）保留但文档标注"非产品语义码"
禁止   : input() / 任何确认提示 / 交互式分页
```

---

## 四、分层架构

```
                 ┌──────────────────────────────────────────┐
   agent ──────► │ cli/            表现层                    │
   人类  ──────► │   parser · 命令实现 · JSON/人读投影 · 退出码 │
                 └───────────────────┬──────────────────────┘
                                     │ 只调用 application
                 ┌───────────────────▼──────────────────────┐
                 │ application/    用例编排（无正则、无 SQL）  │
                 │   sync · index · pull · cite · verify     │
                 │   provenance · resolve · status           │
                 └───────┬───────────────────────┬──────────┘
                         │                       │
        ┌────────────────▼───────┐   ┌───────────▼─────────────┐
        │ domain/   纯逻辑        │   │ ports/  协议（Protocol） │
        │  编号归一化 · 角色判定  │   │  Source  Catalog  Store  │
        │  引用串生成 · 状态机    │   │  Fetcher                 │
        │  文件名策略 · 可得性评分│   └───────────┬─────────────┘
        └────────────────────────┘               │ 实现
        ┌────────────────────────────────────────▼─────────────┐
        │ adapters/                                            │
        │  sources/mohurd.py   …                               │
        │  store/sqlite.py                                     │
        │  fetch/guard.py  ←── 唯一出站；依赖 compliance        │
        └──────────────────────────────────────────────────────┘
                                     │
                          ┌──────────▼──────────┐
                          │ compliance/  白名单  │
                          │ 限速 · 诚实UA · 放弃  │
                          └─────────────────────┘
```

**依赖方向铁律**：`compliance` 不依赖任何业务模块；`domain` 不碰网络与 DB；`sources` 只把 HTML 变成 DTO，绝不写库；`application` 不含正则。违反即评审不通过。

### 目标目录

```
guiju/
├── __init__.py            # 只导出 __version__
├── __main__.py
├── _version.py            # 版本单一来源
├── cli/
│   ├── __init__.py        # build_parser / main
│   ├── render.py          # JSON 与人读两种投影
│   └── commands/          # 每命令一文件：sync.py index.py pull.py cite.py ...
├── application/
│   ├── sync.py  index.py  pull.py  cite.py  verify.py  provenance.py  resolve.py
├── domain/
│   ├── codes.py           # 归一化 / 角色判定 / 新旧关系
│   ├── cite.py            # 引用串（最需测试）
│   ├── naming.py          # 文件名策略
│   └── availability.py    # 可得性判定与评分
├── ports/
│   └── source.py          # Source/Catalog/Store/Fetcher 四个 Protocol
├── adapters/
│   ├── fetch/guard.py     # 唯一出站
│   ├── store/sqlite.py    # schema + 迁移
│   └── sources/mohurd.py  # 站点魔术值全在此
├── compliance/
│   └── __init__.py        # 白名单 + 限速 + 反爬放弃
migrations/0001_init.sql
tests/
  fixtures/catalog-page-1.html      # 列表页解析的离线夹具（唯一的测试固件）
  test_cite.py  test_codes.py  test_guard.py  test_parse_catalog.py
```

> 拆分不是为了好看：`cite` 与 `codes` 是唯一值得写单测的纯逻辑，混在 536 行的 cli 里就永远测不了。

---

## 五、数据模型 v2

**建模错误修正**：现模型以 `announcements` 为主体、`codes` 为附属索引，导致"无编号公告"（实测 39%）无法寻址，而"同编号多版本"无法表达。改为 **公告 ⇄ 版本（M:N + 角色）**，文件独立于编号存在。

```sql
PRAGMA user_version = 1;          -- 迁移与 schema 版本分离

-- 现实存在物：一条政府公告
CREATE TABLE announcement (
  id            INTEGER PRIMARY KEY,
  url           TEXT NOT NULL UNIQUE,
  legacy_url    TEXT,              -- zhengcefilelib 旧链，仅溯源
  published_on  TEXT,              -- YYYY-MM-DD
  title         TEXT NOT NULL,
  doc_name      TEXT,              -- 《》内名称；60 条无书名号
  category      TEXT NOT NULL,     -- national|industry|local|partial_revision|draft_en|other
  body_sha256   TEXT,              -- 详情页指纹：改版检测的免费信号
  fetched_at    TEXT
);

-- 引用主体：一个版本号
CREATE TABLE edition (
  code_key      TEXT PRIMARY KEY,  -- GB55037-2022（归一化）
  code_display  TEXT NOT NULL,     -- GB 55037-2022
  title         TEXT,
  mandatory     INTEGER DEFAULT 0,
  status        TEXT NOT NULL DEFAULT 'unknown',  -- current|superseded|draft|unknown
  replaces      TEXT,              -- 被它代替的 code_key
  replaced_by   TEXT,
  confidence    TEXT NOT NULL DEFAULT 'parsed'    -- parsed|ambiguous|manual
);

-- 公告谈到了哪个版本，以及是什么身份
CREATE TABLE mention (
  ann_id        INTEGER NOT NULL REFERENCES announcement(id),
  code_key      TEXT NOT NULL REFERENCES edition(code_key),
  role          TEXT NOT NULL,     -- primary|superseded_ref|target_of_revision|mentioned
  evidence      TEXT,              -- 命中的那句原文（人可复核）
  PRIMARY KEY (ann_id, code_key)
);

-- 正文文件（独立于编号，允许"无编号但有正文"）
CREATE TABLE artifact (
  id            INTEGER PRIMARY KEY,
  ann_id        INTEGER NOT NULL REFERENCES announcement(id),
  code_key      TEXT,              -- 可空
  source        TEXT NOT NULL,     -- mohurd|sh_zjw|sc_119
  page_url      TEXT NOT NULL,
  file_url      TEXT NOT NULL,
  label         TEXT,
  sha256        TEXT UNIQUE,       -- 内容寻址的主键
  bytes         INTEGER,
  magic         TEXT,              -- pdf|doc|docx|zip|html
  pages         INTEGER,
  verdict       TEXT NOT NULL,     -- ok|ambiguous|wrong_attachment|not_pdf|truncated|blocked
  local_path    TEXT,               -- 只是缓存；溯源靠 sha256
  fetched_at    TEXT
);

-- 每次出站请求的韧性台账（M4 观测的数据源）
CREATE TABLE probe (
  id INTEGER PRIMARY KEY, source TEXT, url TEXT, status INTEGER,
  bytes INTEGER, outcome TEXT, note TEXT, at TEXT
);

CREATE TABLE corpus_meta (k TEXT PRIMARY KEY, v TEXT);
```

### 角色判定规则（`domain/codes.py`）

| 页面证据 | role | 后果 |
|---|---|---|
| 「发布…《X》」书名号内名称对应的编号 | `primary` | 可作为 `cite` 依据 |
| 「代替」「废止」+ 编号 | `superseded_ref` | 不参与 `cite`，仅建立 `replaces` 链 |
| 「标准编号修改为」/「自…实施」+ 编号 | `target_of_revision` | **后者为 primary**（实测 `JGJ450-2018`→`JGJ/T450-2018`） |
| 只有多个编号、无任何上下文 | `mentioned` + `confidence=ambiguous` | `cite --strict` 退出 32 |

---

## 六、命令行面 v2

保留现有 8 命令（设计是对的），新增 2 个，修全局 flag。

```bash
guiju sync [--pages N] [--page-size 100]        # 增量：遇 0 新增即停 + 尾部抽样比对
guiju search <kw>...      [--level --status --limit --offline]
guiju index [--match K] [--limit N] [--all]     # 详情页→编号（断点续跑）
guiju show <ref>
guiju pull <ref> [--out DIR] [--force]          # 幂等；默认 ~/.guiju/library/
guiju cite <ref> [clause] [--strict]            # 核心价值点
guiju verify [--all] [--deep]                   # sha256/魔数/页数
guiju provenance <path>                          # 按 sha256 反查，与路径无关
guiju status [--json]                            # 新增：目录水位/覆盖度/失败分布
guiju sources [--probe]                          # 新增：适配器健康与最近失败

全局（每个子命令都接受）：--json --db PATH --offline --quiet --version
```

`<ref>` 三种形态保持不变（URL / 公告ID / 编号），但解析优先级明确：
**精确编号 → 编号模糊 → 公告ID → URL**，多候选时返回 `candidates[]` 并退出 32，绝不替用户挑。

---

## 七、主流程状态机

```
目录条目 ──sync──► announcement (id, url, date, title, doc_name, category)
                        │
                        ├─ index ──► mention(role,evidence) ──► edition(status)
                        │              └─ 无编号 → 保留 announcement（39% 的常态）
                        └─ pull  ──► artifact(verdict) ──► local file + sha256
                                        │
                                        ├─ verify ──► ok / missing / modified
                                        └─ provenance ──► 反查公告+附件原始 URL
```

**可得性分档**（必须写进 README，不许吹成"全库可下载"）：

| 区间 | 实测有正文比例 | 产品承诺 |
|---|---|---|
| 2014–2026 | 抽样 2/2 每年命中，≈100% | 保证可取（失败即报明确原因） |
| 2000–2013 | 零星（2001/02/04 各 1–2 例） | 只保证目录元数据，正文标 `needs_manual` |
| 全库 | 52%（54 条分层样本） | 如实展示，不夸大 |

---

## 八、失败模式与退出码（稳定契约）

| 码 | 含义 | agent 动作 |
|---|---|---|
| 0 | 成功（含"搜索无结果"） | 继续 |
| 10 | 已存在且校验通过 | 跳过 |
| 20 | 源不可达（网络/5xx/超时） | 稍后重试 |
| 30 | 公告无附件 / 格式不符 | 换备源或报人工 |
| 31 | 编号未索引 | 先 `index` |
| **32** | 编号/公告**多候选或有歧义** | **必须让用户选**（不许猜） |
| **33** | 附件多个且无法判定 | 报人工（附全部候选 label+url） |
| 40 | 命中合规护栏 | **停止，不重试** |
| 50 | 页面结构变更，需人介入改适配器 | 上报 |

`--json` 失败体统一：`{"ok":false,"exit":N,"error":"中文原因","hint":"下一步","candidates":[...]}`。

---

## 九、里程碑与验收（基线均为本次实测）

| 阶段 | 交付 | 验收指标 |
|---|---|---|
| **M0 能站住**（1 周） | git/README/LICENSE；修 audit 缺陷 #1#2#3；schema v2 + 迁移；cli 拆分；`tests/` 三个纯逻辑测试 | 重定向越权单测通过；`pick_attachment` 对"英文版公告"返回 ambiguous 而非错文件；`cite` 矩阵 12 例全绿 |
| **M1 能信目录**（1 周） | `sync` 全量 + 对账（`index` 已按 ADR 0001 撤销） | 目录 **3254/3254**（基线已测，33 页）；编号覆盖 ≥61%（实测基线） |
| **M2 能取正文**（已完成） | `pull`（单条 + `--list` 批量）+ `verify` + 内容寻址 | 2014+ 编号抽检 **0 垃圾文件**；任取一文件可 `provenance` 反查公告 URL；复跑全部命中已有（零请求） |
| **M3 能扩展**（并入 ADR 0002 二期） | 上海 DG/TJ、四川消防两源 + 冲突仲裁（同版本多源：官方发布公告优先） | 同版本多源不重复入库（sha256 去重）；单源失效不影响其它源 |
| **M4 能维护**（持续） | `probe` 台账 → 适配器健康度、页面指纹变更告警 | 站点改版后 24h 内能定位到失效 adapter 与失效正则 |

> M4 不是可选项。这类工具 90% 死于"站点改版没人修"，而 `body_sha256` + `probe` 是几乎零成本的预警。

**实施进度（2026-10-30）**：M0 ✅；M1 ✅（目录对账 3254/3254，33 页 61 秒）；M2 ✅
（单条/批量取正文、幂等复跑、弱网有界重试、编号定位两道闸）；端到端 36 项、单测 79 项全绿。

**M1 的"全量 index"已被 ADR 0001 取代并撤销**：编号 → 名称改由权威时效源提供（`guiju/currency.py`），
名称 → 公告由本地三层召回检索完成，因此不再需要维护本地"公告 ⇄ 编号"映射。

**M3 / `guiju mcp` 的归属已改**：`docs/adr/0002` 决定把 `spec-currency` 并入本产品（二期），
届时以"一个仓、Python、`guiju` + `guiju mcp` 两个入口"落地；本表 M3 的内容并入其二期第 ② 步。

---

## 十、明确不做（写进 README 与 LICENSE）

1. 不做标准文件的公开分发/镜像/网盘 —— 产品的价值是"取回本地自用"，不是"提供下载站"。
2. 不抓团体标准（T/CECS）、不抓标准图集（G101）—— 无官方公开渠道。
3. 不绕过验证码/登录/付费墙；命中即放弃（退出 40）。
4. **不解析条文内容**、不做智能问答 —— 只生成引用串，避免承诺条文准确性。
5. 不提供高频并发抓取；限速是产品的一部分，不是可调参数。
6. 不承诺条文准确；只承诺"本地文件与官方发布件一致（sha256 可复核）"。

---

## 十一、需要所有者拍板的三件事（默认值已给，不改就按默认执行）

| # | 事项 | 我的默认决定 | 为什么需要你 |
|---|---|---|---|
| 1 | 命令名保留拼音 `guiju`/`gj` 还是改英文（`plumb` 等已被占） | **保留拼音**，理由：PyPI/npm 双空、唯一性、中文用户第一眼就懂 | 涉及商标与品牌，只有你能定 |
| 2 | 首发形态是 CLI 还是 MCP Server | **已定：先 CLI**（一期已交付）；`guiju mcp` 定为二期必修，见 ADR 0002 | MCP 优先会让"数据正确性"被拖到后面 |
| 3 | 是否接受"2014 年前无正文"这一现实 | **接受，并在 README 明示分档** | 若你要求全库正文，则需引入第二/第三源，M3 提前，成本翻倍 |
