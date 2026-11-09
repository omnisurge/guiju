# 变更记录

## 未发布 — 2026-11-09（verify --repair：挪动文件的指纹对位）

- 对账 audit-2026-10-30 第 7/10 条：provenance 主键 sha256 与 user_version
  迁移机制在 M0 重构（0d96403）已落地，audit 表按旧实现描述，勾销。
- 新增 `guiju verify --repair`：扫描 library（`--dir` 可改），把挪了位置的
  归档文件按 sha256 重新对位（结果状态 `repaired`），并列出库内未登记文件
  `orphans`；`local_path` 本就是缓存字段，身份仍是内容指纹。
  只修 missing，modified 仍上报 50。新增 `Store.repoint_artifact`。
- 测试：`test_verify_repair.py` 4 例（missing 上报 / 重指 local_path /
  orphan 与 ok 不动 / .partial 跳过）。

## 未发布 — 2026-11-08（高危 bug 修复：编号角色链路 + 退出码契约）

- `codes._role_for` 修复「X 代替 Y」方向反转：句尾「由/被 + 代替」的被动句里
  施动者是新版（PRIMARY），不再被判成旧版；`_demote_older` 与
  `sqlite.persist_codes` 双双加守卫，ambiguous 兜底 primary 不再把同族旧版
  降级/伪造成 `superseded_ref`；`upsert_edition` 的 confidence 已 `parsed`
  后永不被覆盖降级。
- `autosync`/`status`：目录 age 为 None 时不再 `TypeError`；`search._sorted`
  对 `published_on='待定'` 等非数字年份归 0，不再 `ValueError`。
- `which`：名称输入不再误把 `NoCurrencySource(31)` 当"没收录"；
  `except Exception` 吞 `ComplianceDenied(40)` 已收窄。
- `lookup`/`pull`：空 ref、未命中 URL/ID、空清单等用户输入错误分流到
  `BadUsage(2)`/`NotIndexed(31)`，不再落到裸 `GuijuError(1)`。
- `McpCurrency`：JSON-RPC `{"error":...}` 不再静默变 `[]`（伪 31），改抛
  `SourceUnreachable(20)`；握手失败同理。
- `McpCurrency` 新增 Bearer 认证：`GUIJU_CURRENCY_TOKEN`，请求时惰性读取，
  `McpCurrency(url, token=...)` 可显式注入；`DEFAULT_MCP_URL` 指向
  `https://guiju.守夜人.中国/mcp`（NAS `:18083` 已于 2026-11-01 退役）。
- `lookup._pick_current_version`：`persist_codes` 后补 `store.commit()`
  （此前解析结果随连接关闭回滚）；时效服务 `SourceUnreachable` 不再被
  压成 `AmbiguousTarget(32)`，向上冒泡为 `20` 让用户重试。
- 测试：`test_codes` 新增 `DirectionTest` 6 例；新增 `test_regressions.py`
  10 例（_year_of / ref_is_code / resolve 退出码 / persist_codes 双向 /
  McpCurrency error 与 Bearer）。共 16 个新断言，461 单测全绿。

## 1.0.0 — 2026-11-01（产品化：guiju mcp + native 默认 + hk deployment）

- 新增 `guiju mcp`：标准库 stdio JSON-RPC 与 HTTP `POST /mcp`，提供 `query / verify / batch`，
  复用 `NativeCurrency`，默认缓存目录为 `GUIJU_HOME/currency-cache`。
- `GUIJU_CURRENCY` 默认改为原生 `native`；`cmd / mcp / off` 仍可显式覆盖，旧 SQLite 缓存不再包裹原生。
- 新增 hk systemd 部署单元与运行说明：`/opt/guiju`、`/var/lib/guiju`、`18083`，健康检查 `GET /health`。
- hk 已验证 health 与 37 个 M4 编号查询（35 有结果、2 个 DB31/T 为空、0 错误），并预热 28 个 GB/JGJ/CJJ 官方页面缓存。
- 2026-11-01 所有者授权后完成消费者端点切换，NAS spec-currency 已停止并移除 cron 自启动；文件保留可回退。

## 未发布 — 2026-11-01（ADR 0002 期 1 · M4 预检）

- 新增只读 M4 工具 `scripts/m4_currency_diff.py` 与纯比较模块 `guiju/currency/m4diff.py`，
  37 个编号覆盖 GB/JGJ/CJJ/JTG/DB/DG/TJ；对比 `status / name / implement_date`。
- 通过 hk/ctyun 出口取同一官方 csres 原始页面，与 NAS MCP 缓存逐字段核对：
  GB/JGJ/CJJ **28/28** 一致；DG/TJ **2/2** 一致；DB31/T 双方空结果 **2/2**。
- 修复 csres 返回 `/error/noright.html` 时被误判为合规 40 的边界，改为 `SourceUnreachable`
  以允许备源；正文/跨源重定向仍保持 40。
- **M4 未通过**：旧 NAS MCP 对 JTG 返回无命中、对 DBJT/DBTJ/DGJ 无能力；本机 csres 被 IP 限流。
  默认仍为 cmd / mcp / off，未切 native；未部署、未修改 NAS、未构建旧 Go 仓。

## 未发布 — 2026-10-31（ADR 0002 期 1 · M3）

- 所有者裁定：上海地标以上海住建委为唯一真相；撤销 shzjw + csres 双源复合/交叉校验，
  不移植旧 Go composite，也不新增第二份地标解析器。
- 将原上海 _record/四栏检索/排序提取为唯一纯核心 guiju/domain/shzjw.py；内容源保留
  原合规 POST/下载与全部旧导出，原生 guiju/currency/shzjw.py 只注入独立 JSON 查询 transport。
- 上海已确认编号先于通用 DB 路由；统一空白/大小写/破折号身份但保留官方原编号。
  DG/TJ08、DGJ08、DBJT08、DBTJ08、DBJ08、DB31/T 是查询归属边界，不扩到其他省市。
- 基础号完整读取四栏并返回所有严格匹配版本；显式版年只取该完整编号，不自动用新版替换旧版。
- 官方 standardType 接受实际字符串 "0"/"1"；废止栏缺行标记仍为 withdrawn，未来实施日期
  不把源站 current 改成 upcoming；分页不完整/显式失败统一 exit 50，不缓存部分结果。
- 缓存增加上海源特有精确身份校验与官方 v2 namespace：不重放错标准、外源/备源结果和旧裁剪数据；
  无记录/源故障/40/取消均不查 csres/其他源，只允许已验证同源有界 TTL 明示降级。
- 新增 84 项测试，全量 349 → 433；三版本各 433 通过，ruff 与离线冒烟 4/4 全绿。
  官方查询端点仅元数据点查 5/5（四现行、一废止），未取 PDF、未访问 NAS、未切默认/部署。

## 未发布 — 2026-10-31（ADR 0002 期 1 · M2）

- 前缀注册表按最长匹配与 ASCII 词边界选源；GB/JGJ/CJJ → csres→ccsn→std_samr，
  JTG → csres→jtst，DB* → csres→dbba；上海地标专门路由留给 M3（后经所有者裁定仅接 shzjw）。
- 移植四个备源与基础号重查；主源成功空结果不换源，备源只救源故障，
  结果显式保留 `fallback`，不得把备源结论伪装成主源。
- `NativeCurrency` 提供显式查询编排：24h TTL、上游失败最多用 30 天内旧缓存，
  fresh/stale/来源风险分别可见；40 与取消不能由备源或缓存恢复。
- 具体磁盘缓存位于 `guiju/store/currency.py`，显式目录、namespace+query hash、
  schema/时间/全字段校验、原子替换；原生元数据树仍无文件或数据库依赖。
- 标准库 HTTP 扩展 JSON/查询表单：dbba 不发 JSON Accept，307/308 保留 POST 参数，
  禁止跨源重定向；JSON Unicode/HTML 实体验证码同样即时 40，深层 JSON 异常归 50。
- 八份原始 Go golden 全部按字节锁定并逐字段回归；全量单测 189 → 349。
  本轮不切默认、不做 M3/M4、不访问 NAS，不修改或构建旧 Go 仓；未发版/提交/推送。

## 未发布 — 2026-10-31（ADR 0002 期 1 · M1）

- 将时效单文件迁为 `guiju/currency/` 包，保留全部旧导出、缓存和默认 cmd / mcp / off 选择；
  原生 csres 只可显式构造，尚未切换默认查询链路。
- 新增纯标准库元数据 HTTP 客户端（非第三方 httpx）：25s 超时、最多 3 次、0.5/1.5s 退避、
  诚实 ASCII UA、按 host 1.2s 间隔；固定 csres 搜索出口，逐跳校验，拒绝附件/二进制与人机校验。
  有界读取并识别 Content-Length 截断；中间跳转只采样 8192 字节，不让 urllib 无界吞体。
- 移植 csres GBK 表格解析与号族过滤，保留发布日期/raw 证据，
  未知/缺失状态不兜底现行；既有 CLI/TSV 五字段投影不变。
- 原始 GBK golden 按字节迁入并锁 SHA-256 / Git 换行属性，六字段逐项对照 Go 金标准；
  新增 54 项离线测试（含真实 HTTPResponse/opener 内存回归与禁正文/禁落盘哨兵）。
- M1 不含 M2 路由/备源/TTL、不更改旧 Go 仓或 NAS；验收见 `docs/ACCEPTANCE.md`。

## 0.14.5 — 2026-10-31（交接文档重写）

- `HANDOFF.md` 按 v0.14.4 现状重写：文档地图、跑法、**这一轮 9 个版本各做了什么**、
  21 条"已验证的事实"（住建部 / 上海地标 / 其他源 / 时效与配置四组）、
  ADR 0002 期 1 的 M1–M4 计划与门禁、红线、环境表。
- 明确两件"不要再提"的事：特种作业证书查询产品（三站全反爬 + 敏感个人信息，已放弃）；
  以及开工前不要动 `spec-currency` 仓、不要再写第二份地标解析器。

## 0.14.4 — 2026-10-31（部署形态定案：合并后落香港服务器）

- ADR 0002 补「部署形态」：两仓合并完成后**一次性部署到香港服务器**（`hk`），用于外网使用；
  不再部署到 NAS。本机不单独安装 `spec-currency`；NAS 上的 `:18083` 维持到合并完成再被取代。
- 澄清一处措辞：NAS 处于 ADR0048 的**变更冻结**（不得对其执行部署步骤），**不是 NAS 故障** ——
  `mcp` 适配器一直在正常调用它的 `:18083`，这本身就是在证明它健康。
- 风险表补一行：冻结与部署的冲突已通过"只落香港服务器"消解。

## 0.14.3 — 2026-10-31（cmd 适配器实测收尾：ADR 0001 的三条路径全部验证）

- 不需要 NAS：`spec-currency` 源码在本机（`~/Development/spec-currency`，Go），
  `go build -o /tmp/spec-currency ./cmd/spec-currency` 后即可验证 `cmd` 适配器。
- 实测结果：`currency_source: cmd`、`currency_endpoint` 指向该二进制、
  `GB 55037-2022 → current · 建筑防火通用规范`（与 mcp 完全一致）；
  地标 `DG/TJ08-2007 → current · 地质灾害危险性评估技术标准 · source=shzjw`，
  与 guiju 自建上海源结论一致（同一上游，两套实现互证）。
- 顺带确认默认优先级：PATH 上有 `spec-currency` 时不设环境变量也会优先用 `cmd`（符合 ADR 0001）。
- 文档：ADR 0001「后续变化」把"cmd 未实测"改为已实测并附命令；验收记录同步。

## 0.14.2 — 2026-10-31（GUIJU_CURRENCY_URL 曾被静默丢弃）

- **修掉第二处静默不生效**：`GUIJU_CURRENCY=mcp` 时，`GUIJU_CURRENCY_URL` 会被**直接忽略**，
  实际连的仍是默认 NAS —— 用户设了 URL 却以为生效了。现在 `arg > GUIJU_CURRENCY_URL > 默认值`。
- **新增 `currency_endpoint` 字段**（`status` / `which`）：显示实际使用的端点
  （MCP URL / 二进制路径 / "不可用：原因"）。配置是否生效不再需要靠猜。
- 单测 131 → 136 项（含 mcp 覆盖优先级、端点可读性）。

## 0.14.1 — 2026-10-31（时效适配器的显式配置：照办，或者报错）

- **修掉一处静默替换**：`GUIJU_CURRENCY=cmd` 而机器上没有 `spec-currency` 时，
  原先会悄悄退到 mcp（`currency_source` 显示 mcp，用户以为在用本地二进制）。
  现在显式取值要么照办、要么如实报错：
  - `cmd` 缺二进制 → `currency_source: cmd(缺失)`，查询 `exit 20` 并说明原因；
  - `GUIJU_CURRENCY=xyz` → `xyz(未知)` + 20；
  - 有本地缓存时仍可用缓存回答，并标 `currency_from_cache`。
- ADR 0001「后续变化」补三行：`pull` 在名称定位时额外抓详情页（有意放宽）、
  显式配置不再静默替换、以及 `CommandCurrency` 在本机**未实测**（没装 spec-currency）。
- 单测 127 → 131 项。

## 0.14.0 — 2026-10-31（同名多版本按时效自动定版：版本迭代不是歧义）

- **按名称定位时自动定版**：同一个名字命中多条公告，绝大多数是**同一标准的版本迭代**
  （实测《绿色建筑评价标准》：2024-07-11 局部修订 / 2019-05-30 发布 / 2014 / 2006）。
  现在先抓详情页解析每条的正文编号，再问时效服务哪一个是现行版，
  最后在同一现行编号的多条公告里取**最新且有附件**的那条 —— 这正是 spec-currency 存在的意义。
  实测 `guiju pull 绿色建筑评价标准` → 选中 2024-07-11，4,069,066 B，`status: current`。
- 结果里新增 `versions`：列出所有版本（编号 / 发布日期 / 状态 / 是否有附件 / 是否被选中），
  stderr 也会提示"同名 N 版，按时效选中 X"。
- **定不出唯一现行版时仍然 32**（全部被替代、离线无法核实、或同名但不同体系）。
- 代价：名称定位最多多抓 6 个详情页（`_VERIFY_MAX`），解析结果落库，第二次起 0 额外请求。
- 单测 124 → 127 项；联网验收新增 1 项（38 项地域断言之外）。

## 0.13.1 — 2026-10-31（多源仲裁先探查：实测无跨源同编号冲突）

- **结论写进 `docs/sources.md` §六**：两源覆盖的标准体系互斥（上海 942 条全是地方标准前缀，
  住建部 3254 条公告里没有任何 备案/地方标准/DG-TJ 字样），**同编号跨源重复目前不会发生**，
  因此"多源仲裁"没有战场；真正存在的是"同名不同编号"（19 条完全同名），已由 `exit 32` 处理。
- 候选补 `source`（`mohurd`/`shzjw` + 中文标签）与 `url`：同名多候选时人能看出是哪个源发的。
- 单测 123 → 124 项。

## 0.13.0 — 2026-10-31（删除 index 命令：ADR 0001 收尾）

- `guiju index` **删除**。它是"在本地维护公告⇄编号映射"的入口，ADR 0001 已判定这条路
  不可持续（上游不提供、必须一直抄）；替代路径（时效服务 + 名称召回 + `pull` 顺带解析）早已就位。
- 一并删除只服务于它的 `Store.announcements_without_mention`；`mention`/`edition`/`scan` 表保留
  （`persist_codes` 仍在写，删表要升 schema 版本，为一个空表不值得）。
- 守卫测试升级：`index` 不得出现在子命令里、`index.py` 不得存在、任何文案不得推荐它。
- 单测 122 → 123 项。

## 0.12.1 — 2026-10-31（数据源边界成文 + 两处静默降级修掉）

- **新增 `docs/sources.md`**：四个源的现状与实测证据 —— 住建部（主源）、上海（地标）、
  SAMR（只能按名称查，实测按编号返回 0 条）、openstd（普通 GET 一律 0 条结果）。
  附"接入新源的五条检查清单"与不变红线。
- **修掉一处静默降级**：`cite --strict --offline` 原本会返回 exit 0 且 `warnings: []`，
  相当于把 `--strict` 悄悄关掉。现在按用法冲突退出 2 并说明原因。
- **清掉三处仍在推荐 `guiju index` 的提示**（`errors.py` ×2、`show.py`），
  `index` 在 README 里改标"已废弃（待移除）"；新增守卫测试禁止文案回潮。
- `docs/ACCEPTANCE.md` 更新到 v0.12.0（41 项联网 / 122 单测），并订正"未覆盖清单"
  （断点续传与归档策略已于 0.9.0 完成）。
- ADR 0001 增加「后续变化」段，记录地标源与时效缓存对本 ADR 结论的影响，
  并把 `index` 命令的去留列为**待所有者决定**的唯一遗留项。

## 0.12.0 — 2026-10-31（时效查询本地落库：服务断了，查过的编号仍答得出）

- **两级缓存**：进程内（一次命令不问两次）+ 落库（`currency_cache` 表）。
  时效服务不可达时用本地缓存回答，并在结果里明确标出
  `currency_from_cache=true` / `currency_cache_age_days`，stderr 也提示"现行性可能已变化"。
  缓存只存元数据，不存正文。
- **不升 schema 版本**：缓存表是纯派生数据，用 `CREATE TABLE IF NOT EXISTS` 做"加法"，
  已有库自动补齐；把它算进版本迁移只会多出一类"版本对了但缺表"的故障。
- **SAMR / 国家标准全文公开系统的实测结论**（阶段 B 的调研）：
  `std.samr.gov.cn` 的检索接口只支持**名称关键词**（`searchText`），
  传编号返回 0 条；`openstd.samr.gov.cn` 的 `std_list` 对普通 GET 一律返回 0 条结果。
  也就是说"国标编号 → 名称/状态"这条路，官方并没有开放给程序用 —— 所以阶段 B 改为上面的本地缓存，
  而不是接一个答不出问题的源。
- 单测 114 → 119 项（落库、不可达回落、无缓存仍抛 20、空结果不落库）。

## 0.11.0 — 2026-10-31（地标时效自己判：现行/废止照抄源站，不依赖外部服务）

- **上海源的两个维度打通**：站点按「现行/废止 × 标准/设计标准」分四栏
  （实测：现行标准 548 / 废止标准 334 / 现行设计标准 37 / 废止设计标准 23）。
  适配器按四栏检索，**同一编号两栏都有时保留现行那条**。
- **状态照抄，不猜**：`abolishTime` / `isAbolish` → `withdrawn`，否则 `current`；
  `standardType=1` 标为设计标准（图集）。
- **`which` / `cite` 兜底**：外部时效服务没记录（或不可达）时，地方标准的现行性
  直接读源站（`localstatus.records`）—— 地标这块不再依赖 NAS 上的服务。
- `pull` 下载到已废止版本会明确警告，并把 `status` 写进结果。
- 单测 107 → 114 项（状态两维、现行优先、废止兜底、which 回落）。

## 0.10.0 — 2026-10-31（多源第一步：上海地方标准可下载）

- **新增上海源 `shzjw`**（bzjg.zjw.sh.gov.cn）：`guiju pull --source shzjw DG/TJ08-2007`
  直接拿到地标正文（实测 2,084,644 字节 PDF，sha256 归档，复跑 exit 10）。
  上海没有可抓的公告页（SPA + JSON API），因此适配器以"API 模式"接入：
  检索 `external/localStandard/list` → 下载 `api/files/webDownload`（HMAC 令牌按需现算）。
- **`compliance.post_json`**：POST 也走同一道闸门（白名单 / 逐跳校验 / 限速 / 诚实 UA）。
  没有为第二个源开私路。
- 编号口径：直接采用站点给的含年版编号（`DG/TJ08-2007-2026`），
  不套用通用解析器 —— 它会把 `-2026` 误当成年份并判成歧义。
- 单测 101 → 107 项（API 模式的分支：命中/未命中 31/多命中 32/幂等 10）。

## 0.9.0 — 2026-10-31（断点续传：大文件不再从头再来）

- **HTTP Range 续传**：`pull` 先把附件流式写入 `library/.partial/<公告+附件键>.part`，
  中断后重跑同一命令从断点继续（服务器回 206 追加；回 200 说明不支持 Range，则重下）。
- **流式落盘**：不再是 `resp.read()` 整包进内存再写盘 —— 5 MB 的 PDF 不再占 5 MB RAM，
  也避免了"内存里拼好再落盘"时被中断的浪费。
- 失败可见：续传时会打印"从 N 字节处继续（共 M 字节）"；非 PDF 附件（.doc/压缩包/HTML 错误页）
  会删掉 `.part` 再报错，不让错误页成为下次续传的底子。
- 单测 97 → 101 项（本地起 HTTP 服务验证 206 追加 / 200 重下 / HTML 拒绝落盘）。

## 0.8.1 — 2026-10-31（刷新不许静默：catalog 状态进 JSON）

- **每份 `--json` 结果都带 `catalog` 字段**：`state`（fresh / refreshed / refreshing /
  refresh_failed / stale）、`age_days`、`announcements`、`new`、`retry_after_seconds`、`message`。
  由 `render.emit()` 统一注入，任何命令都不会漏。
- **锁被占用时明确要求重试**：`state=refreshing` + `retry_after_seconds=30`，
  而不是悄悄用旧目录给出否定结论。
- 刷新失败进入冷却时同样如实上报剩余秒数。
- 修：目录非空但从未记录 `synced_at` 时的格式化崩溃（`None` 格式化）。

## 0.8.0 — 2026-10-31（目录不再需要任何人照料：进程内自保鲜）

- **目录自保鲜**：`search` / `which` / `pull` / `cite` 执行前若目录为空或 >7 天，
  自动增量刷新（实测空库首拉 3254 条后照常返回结果；新鲜时 0.08s / 0 请求）。
  不依赖 cron、launchd 或常驻进程 —— 换机器、进容器都成立（ADR 0003）。
- 刷新失败**不阻断命令**：记录时间戳，1 小时内不再尝试，命令按现有目录继续。
- 并发安全：非阻塞 `flock`，别的进程正在同步时直接跳过。
- 逃生开关：`GUIJU_NO_AUTO_SYNC=1`（CI/离线测试用），命令级 `--offline` 同样不触发。
- 重构：抓取逻辑下沉到 `guiju/syncengine.py`，`sync` 命令与自保鲜共用一份实现。

## 0.7.2 — 2026-10-31（把"目录要维护"变成"目录会自己保鲜"）

- **`sync --if-stale DAYS`**：目录还新就 0 请求秒退（实测 `--if-stale 7` 用时 0.10s），
  定时任务一行即可：`guiju sync --if-stale 7`。
- **`status` 报新鲜度**：新增 `catalog_age_days` / `catalog_stale`（>7 天），
  过期时在 stderr 提示"增量约 3 秒 / 2 个请求"。
- 澄清一件事：目录**不是索引**。索引要逐条详情页抄编号（持续维护不可避免）；
  目录只是上游列表的镜像 —— 全量 33 页≈60s，之后增量 2 个请求≈3s，零人工整理。

## 0.7.1 — 2026-10-30（建仓后 CI 抓到的第一个兼容缺陷）

- **修 Python 3.10 无法导入**：`domain` 三个模块直接用 `enum.StrEnum`（3.11+），
  而 `pyproject` 声明 `requires-python >= 3.10`。新增 `guiju/_compat.py` 提供最小等价物。
  本地用 `uv` 装 3.10 实测：79 项单测全绿、CLI 可启动。
- 仓库落地 GitHub（私有 `omnisurge/guiju`）；CI：3.10 / 3.12 / 3.13 单测 + 离线冒烟 + 入口自检 + ruff。

## 0.7.0 — 2026-10-30（修复"拿错书"：编号定位只看发布角色）

### 修复（严重）

- **`GB 50016-2014` 曾被定位到《建筑防火通用规范》**（`JGJ 130-2011` → 《施工脚手架通用规范》同理）。
  原因：旧编号以"代替 GB 50016-2014"的形式出现在新规范公告里，被当作"发布该编号的公告"。
  现在只有 `primary` / `target_of_revision` 才算这个编号的家；`mentioned` / `superseded_ref` 只作参考
- 单一候选也要核实：权威名称与公告名不一致时，抓详情页比对正文编号；对不上则 `31`，
  绝不把相近的另一本标准递出去

### 验收

- 新增 3 条针对性单测（被代替引用 / 核实命中 / 核实失败拒绝）
- 端到端新增 2 条精准性断言（GB 50016-2014、JGJ 130-2011）

## 0.6.1 — 2026-10-30（清理：不加仓库负担）

- 移除 `pull --list` 的 `--limit`：清单本身就是范围，静默截断会让"跑完了"变成假象
- 不把任何业务清单/台账数据入仓（结论写进 `docs/ACCEPTANCE.md`，数据留在使用者本机）

## 0.6.0 — 2026-10-30（批量取正文 + 弱网重试）

### 新增

- `pull --list <文件|->`：按清单批量取正文。清单每行一个编号/名称/公告ID/URL，
  支持 `#` 注释与**行尾注释**（`GB 55037-2022  # 建筑防火通用规范`），`- ` 读 stdin
- 批量汇总：`counts`（按退出码）、`bytes_total`、`results[]`（每条含 ref/exit/outcome/path）

### 优化

- **幂等快路径**：已知编号且归档完好时，连详情页都不再抓（批量复跑从 2 请求/条 降到 0：
  实测 22 本复跑 **0.67 秒**）
- **有界核实**：按名称命中 2–6 条公告时，逐条抓详情页比对正文编号，命中唯一即锁定；
  >6 条退回人工（32）。修掉了首轮 22 本里 2 本卡在 32 的问题（GB 55006-2021、GB 55031-2022）
- **归档编号归一化**：归档表存归一化号、查询对空格大小写不敏感，修掉"复跑仍走网络"的隐性 bug
- **弱网有界重试**：仅对可重试失败（超时/连接错误/5xx）重试，默认 2 次、退避 1s；
  合规拒绝（40）与 4xx 一律不重试——重试它们只会变成攻击

### 验收

- 批量机制实测：单批全量成功、复跑全部命中已有（0.67 秒，零网络请求）
- 端到端断言从 31 项扩到 **34 项**（新增批量 3 项），全绿
- **批量全量下载不是主场景**：正文 kb-system 已有；批量用于"已知缺哪几本"的定向补齐

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
- `data/标准公告全目录.csv` → `tests/fixtures/catalog-2026-10-30.csv`（黄金快照；0.7.2 已移除：没有任何测试读它，对账改由 e2e 断言）
- 版本号单一来源：`guiju/_version.py` + `pyproject` 动态读取