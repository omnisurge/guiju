# 验收记录

> 最新离线验收：**v1.0.0（2026-11-01，发布）** —— **446 项单测** / 4 项离线冒烟 / 本地 CI 全绿；hk M4 37 个编号冒烟通过。
> 最近完整联网验收仍为 **v0.14.3 的 42/42** 历史记录；v1.0.0 未重跑完整联网验收、未构建或更改旧 Go 仓。
> 默认时效已切为 **native**；hk 服务与消费者切换已完成，NAS spec-currency 已停用（文件保留可回退）。后续各版本记录保留作历史对照。

## v1.0.0（2026-11-01，发布）

| 项 | 本次实测 |
|---|---|
| 代码基线 | v1.0.0 发布工作区；包含 M1–M4、guiju mcp、hk 部署 |
| 完整单测矩阵 | Python 3.10.21、3.12.14、3.13.5 **各 446/446 通过** |
| 本地 CI | ruff、446 单测、离线 e2e **4/4**、version/status/help 入口自检全绿 |
| MCP | stdio + HTTP `POST /mcp`，query/verify/batch；hk 18083 健康 |
| M4 | 可比基线 32/32；hk 37 个编号：35 有结果、2 个 DB31/T 为空、0 错误 |
| 发布 | wheel/sdist 已构建；消费者 profile 已切到 hk；NAS spec-currency 已停用 |
| 数据隔离 | 临时 GUIJU_HOME、GUIJU_CURRENCY=off、UV_OFFLINE=1；未修改旧 Go 仓 |

## ADR 0002 期 1 · M3（2026-10-31，未发布）

| 项 | 本次实测 |
|---|---|
| 代码基线 | v0.14.5 + 未发布 M1/M2/M3 工作区变更；未提交/推送 |
| 完整单测矩阵 | Python 3.10.21、3.12.14、3.13.5 **各 433/433 通过** |
| 本地 CI | ruff、433 单测、离线 e2e **4/4**、version/status/help 入口自检全绿 |
| M3 新增测试 | **84 项**：上海核心/内容兼容、原生适配、HTTP/路由/缓存/边界回归 |
| 官方元数据点查 | 固定官方 list 端点 **5/5**：DG/TJ08-2007-2026、DBJT08-144-2026、DGJ08-2139-2021、DBTJ08-102-2016、DG/TJ08-2021-2007；四现行、一废止，未取 PDF |
| 默认行为 | cmd / mcp / off、旧内容下载与五字段投影不变；原生只可显式构造 |
| 数据隔离 | 临时 GUIJU_HOME / 缓存目录、GUIJU_CURRENCY=off、UV_OFFLINE=1；未访问 NAS/旧 Go，未写真实规范库 |
| 独立复核 | 基础号裁剪问题修复并复验关闭；冻结版 3.10 重点 171 项、12 种内存 HTTP、16 种磁盘场景通过，无剩余 M3 项 |

### 核心语义与完整链路

- **上海地标只认上海住建委 shzjw**：不查 csres 或其他元数据源交叉校验、补判或兜底；
  无记录、源故障、结构异常、40 与取消都停止在本源，不转其他源。
- 内容源与原生元数据共用唯一 guiju/domain/shzjw.py：原 _record、四栏检索、排序和
  find 只有这一份实现；内容下载令牌/流式下载/文件校验函数保持原样，原生不导入这些出口。
- 上海已确认编号 DG/TJ08、DGJ08、DBJT08、DBTJ08、DBJ08、DB31/T 先于通用 DB；
  统一空白/大小写/已支持破折号，但保留官方原编号，不扩到其他省市。未确认的上海 DB31
  写法、嵌入零宽/控制字符等保守 NoSource，绝不落通用 DB 外源。
- 基础号读取完整四栏并返回全部严格匹配版本；显式完整版年只精确匹配该编号，
  不会自动换成新年份，也不会因裸基础号现行记录提前停止其他栏。
- 官方实际 standardType 为字符串 "0"/"1"，已兼容；废止请求栏即使行缺
  isAbolish/abolishTime 也映射 withdrawn；未来实施日期仍沿源站 current，不推 upcoming。
- 分页按 total 顺序读完整；total 漂移、短页、重复页、超页数上限、显式 success=false
  或结构异常统一 exit 50，不把截断/失败当无命中或部分成功。
- 缓存 namespace 升至上海官方 v2，并按源特有 accepts_record 校验精确编号/来源/状态/
  fallback：DG/TJ08-9999 不能重放给 DG/TJ08-2007，csres/dbba 或备源记录不能伪装上海官方，
  旧外源 namespace 不自动改名重放。
- 内容源 13 项旧回归保持不变；旧 PullApiModeTest 的模拟下载现在显式设置临时
  GUIJU_HOME，避免测试残留写入真实规范库（仅测试隔离，产品下载实现未改）。

### 复跑

~~~bash
python3 -m unittest tests.test_shzjw_core tests.test_shzjw tests.test_currency_shzjw
python3 -m unittest tests.test_currency_httpx tests.test_currency_boundary tests.test_currency_registry tests.test_currency_native_integration
# 整体门禁；执行时另指定全新临时 GUIJU_HOME
env -u GUIJU_NO_AUTO_SYNC GUIJU_CURRENCY=off UV_OFFLINE=1 bash scripts/ci.sh
~~~

官方字段投影见 [fixture 说明](../tests/fixtures/currency/README.md)。
**未做**：M4 线上逐字段 diff、默认切换、部署、发版、旧 Go 归档；NAS 相关步骤仍受 ADR0048 冻结约束。
shzjw 官方点查只访问固定 list 端点，未请求任何 PDF 下载链接。

## ADR 0002 期 1 · M2（2026-10-31，未发布）

| 项 | 本次实测 |
|---|---|
| 代码基线 | v0.14.5 + 未发布 M1/M2 工作区变更；未提交/推送 |
| 完整单测矩阵 | Python 3.10.21、3.12.14、3.13.5 **各 349/349 通过** |
| 本地 CI | ruff、349 单测、离线 e2e **4/4**、version/status/help 入口自检全绿 |
| M2 新增测试 | **160 项**：四备源 81、磁盘缓存 26、路由/fallback/Native 36、HTTP 11、整链路 4、五源边界 1、golden 清单 1 |
| golden | 全部 **8 份**与旧 Go 原件字节一致；长度/SHA-256 锁定，语义期望人工照录而非新解析器生成 |
| 默认行为 | cmd / mcp / off、旧缓存和五字段投影不变；原生只可显式构造 |
| 数据隔离 | 临时 GUIJU_HOME / 缓存目录、GUIJU_CURRENCY=off、UV_OFFLINE=1；未访问上游/NAS或真实规范库 |
| 独立复核 | 深层 JSON 错误收口项已修复并复验关闭；冻结版无剩余 M2 项，另有 111 项最低版本选定用例及内存/临时目录场景复核通过 |

### 核心语义与完整链路

- 最长前缀 + ASCII 词边界，禁用/未实现路由不参与；GBX123 不能误中 GB。
- GB/JGJ/CJJ → csres→ccsn→std_samr；JTG → csres→jtst；DB → csres→dbba。
  主源成功空结果不访问备源，备源错误/空结果才继续，首个非空结果标记实际备源。
- fresh 为 **age < 24h**，stale 为 **age <= 30d**，精确等号用固定时钟验证。
  主源新空成功替换旧非空数据，负缓存重放仍区分 fresh 与 stale。
- 当前 admit/取消/路由先于缓存读取；任意源返回 40，停止后续源、不读 stale、
  不刷新/覆盖旧条目；QueryCancelled/KeyboardInterrupt 与编程错误也不泛化恢复。
- schema/namespace/query/全字段/有限正时间校验；未来/零/坏时间、错状态或 JSON 结构、
  重复键等都为 miss，合法 [] 才是负缓存；唯一临时文件+fsync+原子替换，失败保留旧完整条目。
- 真实 HttpClient + 旧 golden + 临时 DiskCache 覆盖：主源503后CCSN兜底、重启fresh命中、
  全链失败stale重放及备源风险保留、超过30天拒绝、相同旧缓存遇验证码仍40。
- POST form 的编码/Accept、301/302/303转GET、307/308保留参数、跨已批准源也拒绝；
  JSON Unicode转义/HTML实体验证码同样40，错误JSON含有界诊断片段且不重试。
- 五个适配器均在正文出口/文件/DB/真实socket哨兵下通过；Native 不装配缓存时无持久化。
  具体磁盘后端在 store 层显式注入，不放宽元数据适配器禁落盘约束。

### 复跑

```bash
# 八份原始 golden 及全部适配器语义
python3 -m unittest tests.test_currency_golden tests.test_currency_csres tests.test_currency_ccsn tests.test_currency_stdsamr tests.test_currency_jtst tests.test_currency_dbba
# 整体门禁；本次实际执行另指定了全新临时 GUIJU_HOME
env -u GUIJU_NO_AUTO_SYNC GUIJU_CURRENCY=off UV_OFFLINE=1 bash scripts/ci.sh
```

完整输入清单见 [golden manifest](../tests/fixtures/currency/manifest.json)。
**本检查点未做**：M4 线上逐字段 diff、默认切换、部署、发版、旧 Go 归档。
M3 已在后续检查点完成（见上），此处保留历史对照。

## ADR 0002 期 1 · M1（2026-10-31，历史检查点，未发布）

| 项 | 本次实测 |
|---|---|
| 代码基线 | v0.14.5 + M1 工作区变更；未发版、未提交/推送 |
| Python | 3.13.5 本地 CI；3.10.21 / 3.12.14 各跑完整单测 |
| 单测 | **189/189**（原有 135 + 新增 54） |
| 本地 CI | ruff、单测、离线 e2e **4/4**、version/status/help 入口自检全绿 |
| 数据隔离 | 临时 `GUIJU_HOME`；`GUIJU_CURRENCY=off`，`UV_OFFLINE=1`；不读取/改写用户规范库 |
| 包兼容迁移 | 对照 Git 基线，10 个旧类/函数除必要相对导入层级外 AST 一致；旧导出和五字段投影不变 |
| golden | 原始 GBK 10,039 字节，SHA-256 锁定；两个有序结果逐项比较六字段 |
| 边界 | 原生树 AST 禁正文/存储依赖；运行时哨兵禁止正文出口、文件写入、数据库与真实出站连接 |
| 独立复核 | 两个已复现阻断均修复并复验关闭，无剩余 M1 项 |

新增回归的关键断言：

- `GB 50011-2001` → superseded，发布日期 2001-07-20，实施日期 2002-01-01；
  `GB/T 50011-2010` → current，发布日期 2010-05-31，实施日期 2010-12-01。
  名称与 source 同时逐项对照 Go golden；缺状态只能 unknown，不推断现行。
- HTTP：25s 超时、最多 3 次、0.5/1.5s 退避；403 不重试、429/5xx/传输故障有界重试；
  解析、读体失败与人机校验不重发请求。
- 标准库真实 `HTTPResponse` 的 Content-Length / chunked 截断报 20，不能伪装成空结果。
- 标准库真实 opener + 内存 transport：中间附件在读体前拒绝；HTML 中间体只读 8192 字节；
  GBK/UTF-8 验证码与伪装二进制立即 40 且不跟随。301/302/303/307/308、循环、
  缺 Location、非法 scheme 均有分类回归；Python 3.10 的 308 兼容分支已覆盖。

快速复跑（只有内存响应，不请求上游，也不依赖旧仓）：

```bash
python3 -m unittest tests.test_currency_csres tests.test_currency_httpx tests.test_currency_boundary
# 全量 CI：不要全局设置 GUIJU_NO_AUTO_SYNC，它会干扰保鲜测试的受控 mock。
env -u GUIJU_NO_AUTO_SYNC GUIJU_CURRENCY=off UV_OFFLINE=1 bash scripts/ci.sh
```

夹具来源与字节校验见 [golden 说明](../tests/fixtures/currency/README.md)。
**本历史检查点未做**：M2 的前缀路由/备源/TTL、M3、M4 的线上逐字段 diff、默认切换、部署或发版。
当时规划的 M3 复合源已被后续所有者“上海官方唯一真相”的单源决定取代。

## v0.13.0（2026-10-31）

- **`guiju index` 命令已删除**（ADR 0001 的最后一项遗留）。子命令列表不再含它，
  执行会得到 argparse 的明确报错；`mention`/`edition` 表保留（`pull`/`which` 解析时仍在写），
  仅删除 `scan` 的查询方法与命令文件。
- 守卫测试改为"index 不可作为子命令 + index.py 不存在 + 无文案推荐它"。
- 单测 **123 项**全绿。

## v0.12.0（2026-10-31）

| 项 | 值 |
|---|---|
| 版本 | guiju 0.12.0（schema v2，`currency_cache` 为加法表） |
| 单测 | **122 项全绿**（`python3 -m unittest discover -s tests -t .`） |
| 联网验收 | **41/41 通过**（`python3 scripts/e2e.py`，约 3 分钟） |
| 离线验收 | 4/4 入口自检 + 离线 e2e 全绿（`scripts/ci.sh`） |

相对 v0.7.0（36 项）新增的验收面：

| # | 断言 | 实测值 |
|---|---|---|
| 37 | 断点续传：中断后从 `.partial` 续下（Range 206） | 见 `tests/test_resume.py` 4 项 |
| 38 | 上海地标可按编号下载（`--source shzjw`） | 2,084,644 B，`%PDF-1.7` |
| 39 | 地标状态来自源站（现行/废止分栏） | `current` / `withdrawn` |
| 40 | 上海地标幂等返回 10 | `already: true` |
| 41 | 子命令都绑定了 `func`（结构性守卫） | `tests/test_hygiene.py` |

> 第 41 项是补上的教训：v0.12.0 一次联网验收发现 `guiju sync` 因漏写 `set_defaults(func=run)`
> 直接崩溃 —— **离线 CI 抓不到这类只在联网路径出现的问题**，所以既修了 bug 也加了守卫。

## v0.7.0（2026-10-30）

> 目的：把「CLI 是否跑顺」变成可重复执行的证据，而不是"我试过了"。
> 复跑：`python3 scripts/e2e.py`（联网，约 2–3 分钟）；`--offline` 只跑不联网冒烟。

## 环境

| 项 | 值 |
|---|---|
| 版本 | guiju 0.7.0（schema v2） |
| Python | 3.13.12（声明 `>=3.10`，运行时零第三方依赖） |
| 数据目录 | 临时 `GUIJU_HOME`（每次全新，避免"上次的缓存作弊"） |
| 时效服务 | `mcp` 适配器 → NAS `http://100.66.1.x:18083/mcp`（spec-currency 2.3.2） |
| 单测 | `python3 -m unittest discover -s tests -t .` → **79 项全绿** |

## 结果：36/36 通过（v0.7.0）

| # | 断言 | 实测值 |
|---|---|---|
| 1 | status 空库可用（schema v2） | ✅ |
| 2 | status 报出时效适配器 | `mcp` |
| 3 | version 带 `ok` 字段 | ✅ |
| 4 | sync 落库 3254 条 | 3254 |
| 5 | sync 与上游对账 | db=3254 / 上游=3254 |
| 6 | sync 二次幂等（0 新增） | new=0 |
| 7 | search 书名号可用（`《防火》`） | 43 条 |
| 8 | search 模糊补召回 | `and+fuzzy` |
| 9 | search 无命中不报错 | total=0 |
| 10 | which 权威状态 | `current` |
| 11 | which 定位本地公告 | #158 |
| 12 | which 标注可得性 | `in_window` |
| 13 | which 旧版状态 | `superseded` |
| 14 | pull 按编号（本地零索引） | 4,039,934 B |
| 15 | pull 内容寻址命名 | `…__9837c08cebd4.pdf` |
| 16 | pull 实施日窗口选中原版公告 | `art/2018/art_17339_236618` |
| 17 | pull 幂等 | exit **10** |
| 18 | 同名多公告 → 32 + candidates | 2 个候选 |
| 19 | verify 通过 | total=1 |
| 20 | provenance 反查来源 | mohurd 公告 URL |
| 21 | provenance 与路径无关 | 改名后仍命中（按 sha256） |
| 22 | cite 按编号 | `《老年人照料设施建筑设计标准》JGJ/T 450-2018 第5.2.1条` |
| 23 | cite 带权威状态 | `current` |
| 24 | cite 按编号（另一本） | `《建筑防火通用规范》GB 55037-2022 第5.2.1条` |
| 25 | cite --strict 拒绝被替代版本 | exit **32** |
| 26 | cite 同名多公告 → 32 | `ambiguous_target` |
| 27 | 未知编号 → 31 | `not_indexed` |
| 28 | 缺文件 → 30 | `no_attachment` |
| 29 | 非法条款号 → 2 | `bad_usage` |
| 30 | `--json` 时 stdout 只有 JSON | ✅ |
| 31 | `--json` 时人读输出不混入 stdout | ✅ |
| 32 | 批量按退出码汇总（已有项 + 未知编号） | `{'10': 1, '31': 1}` |
| 33 | 批量给出产出目录 | `…/library` |
| 34 | 批量 `ok` 只排除严重失败（31 不算） | ✅ |
| 35 | 被代替引用不误导定位（GB 50016-2014） | 建筑设计防火规范 |
| 36 | 被代替引用不误导定位（JGJ 130-2011） | 建筑施工扣件式钢管脚手架安全技术规范 |

## 批量机制验收（v0.6.0，一次性测量，清单不入仓）

| 轮次 | 结果 | 耗时 | 体积 |
|---|---|---|---|
| 第 1 轮 | 20 成功 + 2 条 exit 32（同名多公告） | ~6 min | 164 MB |
| 加"有界核实"后 | **全部成功** | 63 s | 171 MB |
| 复跑（断点/续跑） | **全部 exit 10，零网络请求** | **0.67 s** | — |
| `verify` | 全部文件异常 0 | 秒级 | — |

**"有界核实"**：按名称命中 2–6 条公告时，逐条抓详情页比对正文编号，命中唯一即锁定；
超过 6 条退回人工（32）。这是**查证**不是猜测，代价最多 6 次请求。

批量只是"同一个动作跑 N 次"，不改变单条语义：每条仍然要么拿到可核验的正文，要么给出
明确的退出码与原因。

**关键结论**：全程 `mentions` 表为 **0 行**（从未跑过 `index`），所有编号入口仍然可用 —— index 已不是前置条件。

## 本轮修掉的 6 个真缺陷

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | `cite <名称/公告ID>` 直接 31 | cite 只读库，未解析页面 | 现场解析 1 次详情页补编号（`--offline` 跳过） |
| 2 | `version --json` 无 `ok` | 输出形状不一致 | 补齐 |
| 3 | 非法条款号 → exit 1 | 校验晚于解析；错误类型错 | 新增 `BadUsage(2)`，校验前置 |
| 4 | 上游有名称但本地无公告 → exit 1 | 用了通用 `GuijuError` | 改为 `31`（可修复状态） |
| 5 | `JGJ/T450-2018` 召回 0 | 上游名称带「（2026年局部修订）」，公告名没有 | 检索前剥离括号补充；英文版记录不参与定位 |
| 6 | 同名公告（原版 + 修订）无法区分 | 缺消歧原则 | **版本生效日窗口**：正文公告必然发布在实施日之前 |
| 7 | 批量复跑没有变快（仍走网络） | 归档表里存的是展示号，查询用的是归一化号 | 归档查询对空格/大小写不敏感；新写入一律归一化 → 复跑 0.67 秒 |
| 9 | 时效服务挂掉时返回 `31 not_indexed`，误导致 agent 去 sync | 适配器把连接失败吞成空数组，与"没有该编号"混为一谈 | 传输失败 → `20 unreachable`（可重试）；`cite` 只降级为警告，引用串照常出；`--offline` 改为完全不走网络 |
| 10 | 按公告ID重复 `pull` 会重复下载（39% 公告无编号） | 幂等判定只查编号 | 补 `artifact_by_announcement` 路径 |
| 8 | **会拿错书**：`GB 50016-2014` 定位到《建筑防火通用规范》，`JGJ 130-2011` 定位到《施工脚手架通用规范》 | 旧编号只以"代替 GB 50016-2014"的形式出现在通用规范公告里，被当成"发布该编号的公告" | 只有 `primary`/`target_of_revision` 才算该编号的家；`mentioned`/`superseded_ref` 一律不作为目标 |

## 已知边界（不是缺陷，是现实）

1. **网络依赖**：`sync` / `pull` / `which` 需联网；`search` / `show` / `verify` / `provenance` 纯本地。
2. **时效服务单点**（v0.12.0 起部分缓解）：NAS 不可达时，**查过的编号**可用本地缓存回答
   （`currency_from_cache=true`）；**没查过的**仍然拿不到名称。地标不受影响（源站直接给状态）。
   原先"这是 index 仍保留的唯一理由"已不成立 —— index 去留见 ADR 0001「后续变化」。
3. **csres 首次查询 1–8 秒**（重复 <0.1s，spec-currency 侧有 24h 缓存）；`cite` 每条会多这一次开销，可用 `--offline` 跳过。
4. **2014 年前多数无正文**：`pull` 返回 30 并说明"这不是故障"。
5. **同名多公告仍需人选**：`32` + candidates 是刻意设计，不自动挑。
6. **MCP 未做**：按所有者决定，先跑顺 CLI，之后再评估。
7. **托管 runner 无法执行联网验收**：GitHub runner 实测 `Temporary failure in name resolution`
   （跨境 DNS），首次成功后连续失败。因此 `e2e-live.yml` 加了站点预检，只作便利入口；
   **权威执行在本机**（`scripts/ci.sh` + `scripts/e2e.py`）。

## 尚未覆盖的验收面（v0.12.0 更新）

- ✅ **断点续传**（v0.9.0 已做）：`Range: bytes=<size>-`，206 续写 / 200 重下 / 416 视为完成，
  4 项单测（`tests/test_resume.py`）+ 实测续下 717,954 B 的大文件。
- ✅ **归档目录策略**：`--out` / `--readable` 已可用（内容寻址 / 人类可读两种命名）。
- ⏳ **数百项量级批量的定位率统计**（含 `DG/TJ`、`GB/T` 等非工程建设前缀）—— 仍未做。
- ⏳ **多源仲裁**：目前每个源各自独立（`--source` 显式选择），没有"同一个编号多源比对"的逻辑。
- ⏳ **`guiju mcp` 服务面**：按所有者决定推迟（见 ADR 0002）。
