# guiju 使用契约（CLI / MCP 调用指南）

> 你是被指派通过 `guiju` CLI 或 MCP 服务去「查规范 / 验现行 / 取公开正文 / 出引用串 / 检索标准图集目录」的 AI agent。按本契约调用工具，不要猜测未声明的命令行为。

---

## 一、硬规则与服务边界

<!-- guiju:mcp-summary:begin -->
1. **主路径是 `--json`（CLI）与只读工具（MCP）**：
   - CLI 在 `--json` 下 `stdout` 只输出 JSON，日志与告警一律在 `stderr`；直接解析 `stdout`，不要正则人类输出。
   - MCP 服务仅提供只读工具：`query`（查编号现行状态与 `replaced_by`）、`verify`（返回 `code<TAB>status`）、`batch`（仅接受内联 `codes[]`）、`announcement`（仅返回公告与附件元数据，不下载不落盘）、`atlas_search`（只读检索本地图集索引）；MCP 永不提供安装/写盘能力，也**永远不会返回退出码 `11`**。
2. **全程无交互、多候选交人选**：
   - 工具不存在交互确认提示；当遇到 `32`（`ambiguous_code`）或 `33`（`ambiguous_attachment`）时，必须停下并把 `candidates` 原样交由人工选择，禁止按年份或字母序自行定版或兜底取第一个。
3. **版权与合规边界（不可绕过）**：
   - 仅从 `*.gov.cn` 公开政府页面采集强制/公开标准正文并按单 host `1.2s/请求` 限速；不得尝试绕过域名白名单或限速。
   - 标准图集（如 `G101`、`22G101-1`、`粤25G/T116`）属版权出版物，仅支持只读元数据时效核验与本地图集目录检索（`guiju which`、`guiju atlas` 或 MCP `query`/`verify`/`atlas_search`）；`pull`、`cite` 与 MCP `announcement` 对图集显式拒绝且零下载。
4. **退出码就是动作决策**：
   - `0` 继续；`10` 本地文件已存在直接使用 `path`；`11` 仅 `guiju agent` 本地技能安装/卸载冲突；`20` 源不可达退避重试一次（先区分时效服务还是政府站点，不要盲目 `sync`）；`30` 无正文附件（`availability=pre_window` 属 2014 年前常态，不要重试）；`31` 未收录（先核实目录状态）；`32/33` 交人选；`34` 时效路由未覆盖该前缀（勿 `sync`，改按名称检索）；`40` 命中反爬/合规护栏**立即停止**，严禁重试或绕过；`50` 站点改版或需人工介入，附完整错误输出上报维护者；`2` 用法错误按 `hint` 修正；`1` 程序缺陷附 stderr `traceback` 上报。
<!-- guiju:mcp-summary:end -->

---

## 二、该用哪个命令

```text
要先确认环境与目录状态吗？        → guiju status --json
只想按名称关键词找条目（不联网）？ → guiju search <关键词> --json [--all]
   · 书名号可带可不带（《防火》与 防火 等价）
   · 返回 mode=and/fuzzy/chars，total 是总数、count 是本次返回数
   · --limit 默认 20，取全量用 --all
想知道某编号是什么/是否现行？      → guiju which <编号|名称> --json   （无需预先建索引；支持国标/行标/上海地标与标准图集）
   · 返回 currency[{code,status,name,replaced_by?}] + announcement{id,...}（图集固定返回 announcement=null 与版权提示）
   · status: current / superseded / upcoming / withdrawn / unknown；unknown 不等于现行
   · currency_from_cache=true 表示时效服务不可达、退回本地缓存：结论可用但可能已过时
   · currency_endpoint 显示实际生效端点，可用于核对是否禁用了时效服务
要检索工程建设标准图集目录吗？     → guiju atlas {sync|search|list} --json
   · guiju atlas sync [--if-stale N] --json 同步国标电子书库简明目录（仅写本地图集目录索引，不下载图集 PDF）
   · guiju atlas search <关键词> [--status current|withdrawn|all] [--all] --json / list [--prefix 粤] ... 纯本地检索
要标准正文 PDF 文件吗？            → guiju pull <编号|规范名|公告ID|URL> --json
   · 按名称命中多条时，同名多版本按时效自动定版；versions 字段给出全部版本与选中项，定不出唯一现行版才返回 32 交人选
   · 显式带 4 位版年的编号（如 JGJ 147-2004）若未被收录，拒绝以同族新版顶替（诚实返回 exit 31）
   · 断点续传：中断后重跑同一条命令会从 .partial 续下，不必重头再来
   · 幂等按文件名 sha256 指纹判定；exit 10 表示文件已在本地，直接用返回 path
要整批下载吗？                     → guiju pull --list <文件|-> --json
   · 清单每行一个 ref，支持 # 注释与行尾注释
   · 汇总里 counts 按退出码统计，ok=false 表示存在 1/20/40/50 等严重失败
要上海地标（DG/TJ、DB31/T）正文？  → guiju pull --source shzjw <编号|名称> --json
   · 按站点原样编号（含年版，如 DG/TJ08-2007-2026）
   · 命中多条 → 32；无命中 → 31；已有文件 → 10；废止版仍带 status=withdrawn 告警
只要标准引用串吗？                 → guiju cite <编号|公告ID|URL> [条款号] --json
   · --strict 与 --offline 互斥（离线无法校验现行性 → exit 2），不要混传
要启动/接入 MCP 服务吗？           → guiju mcp [--http --host HOST --port PORT]
   · 默认 stdio；--http 提供 POST /mcp 与 GET /health
   · 工具：query / verify / batch / announcement / atlas_search
要查看或安装使用态 Skill 资产吗？  → guiju agent {show|status|install|uninstall} --json
   · guiju agent show --kind contract 仅打印契约正文；install 默认按项目/用户开放 Skill 目录落盘并用带外收据校验完整性
目录为空或过期？                   → search/which/pull/cite 前若目录为空或 >7 天会自动增量刷新
   · 刷新失败不阻断查询且 1 小时内冷却；禁用自动刷新设 GUIJU_NO_AUTO_SYNC=1
   · 手动刷新：guiju sync [--if-stale 7] --json；status.catalog_age_days 反映目录新旧
```

---

## 三、退出码 → 你的动作

| 码 | reason | 你做什么 |
|---|---|---|
| 0 | — | 继续 |
| 10 | — | 已完成，直接用 `path`，**不要再 pull 一次** |
| 11 | `agent_unmanaged_conflict` / `agent_user_modified` / `agent_not_managed` / `agent_receipt_corrupt` / `agent_symlink_rejected` | 仅 `guiju agent` 返回：交由人工核对目标技能目录归属、本地修改或损坏收据后再决定是否显式加 `--force`（符号链接与非受管卸载始终拒绝） |
| 20 | `unreachable` | 退避后重试一次；仍失败就报告「源不可达」 |
| 30 | `no_attachment` / `bad_attachment` | 读 `hint` 与 `availability`；若是 `pre_window`，这是**正常事实**，不要重试，直接向用户说明 |
| 31 | `not_indexed` | 名称检索无命中，或时效服务给不出名称 → 先确认 `catalog` 状态并按需 `guiju sync`；仍无则说明该标准没有公开正文公告（**不要重试**，可先 `guiju which` 确认） |
| 32 | `ambiguous_code` | **停下来**，把 `candidates` 原样交给人选。禁止按年份/字母序自行判断哪个是现行版 |
| 33 | `ambiguous_attachment` | **停下来**，把 `candidates`（label + url）交给人选。禁止兜底取第一个 |
| 34 | `no_currency_record` | 时效路由未覆盖该编号体系（原生侧无此前缀）。**不要** `sync`——这不是本地目录缺口；换名称路径 `search`/`pull` |
| 40 | `compliance` | **立即停止**。不重试、不换 UA、不换代理、不找镜像绕过 |
| 50 | `needs_human` | 带完整错误输出上报维护者（多半是站点页面结构或接口发生变更） |
| 2 | `bad_usage` | 输入或参数组合有误（如条款号无法解析、`--strict` 与 `--offline` 同传、`--dir` 与 `--target`/`--scope` 互斥）：按 `hint` 改正，不要重复相同输入 |
| 1 | `internal` | 程序缺陷：把 stderr 的 traceback 带上去上报维护者 |

### `20` 有两种来源，不要把「时效服务不可达」当成「本地没这本书」

| 码 | 含义 | 你做什么 |
|---|---|---|
| `20` + 错误里带「时效服务」 | **时效服务**不可达（本地目录本身正常） | **不要** `sync`。改走名称路径：`guiju search <名称>` → `guiju pull <名称>`；若设了 `GUIJU_CURRENCY`，请核实仅取 `native`（可带 `:off` 或缓存路径）或 `off` |
| `20` + 错误里带 URL / HTTP | 政府站点不可达 | 退避后重试一次 |
| `31` | 服务可达，但未收录该编号 / 名称检索无命中 | 先确认目录并 `sync`；仍无则说明没有公开正文公告，不要反复重试 |
| `34` | 编号前缀不在原生时效路由覆盖内 | 不 `sync`；改走名称路径 `search`/`pull` |

### 目录自刷新状态（`catalog` 字段）

任何命令的 `--json` 结果都会携带一个 `catalog` 字段：

```jsonc
"catalog": {"state":"fresh","age_days":0.01,"synced_at":"2026-10-31 10:10:27",
            "announcements":3255,"new":null,"retry_after_seconds":null,"message":""}
```

| state | 含义 | 你做什么 |
|---|---|---|
| `fresh` | 目录在有效期内，本次未触发同步 | 无需动作 |
| `refreshed` | 刚刚完成自动增量刷新（`new` 为新增条数） | 结果基于新目录，可直接用 |
| `refreshing` | 另一个进程正在同步目录 | 结果基于旧目录；等待 `retry_after_seconds`（约 30 秒）后重试 |
| `refresh_failed` | 刷新失败或处于 1 小时冷却期 | 结果基于旧目录；到期后自动重连，也可手动执行 `guiju sync` |
| `stale` | 目录过期且未自动刷新（离线 / 已禁用 / 该命令不触发） | 下「未收录/无正文」否定结论前，先补一次 `guiju sync` |

### `cite` 的请求特征与可信度

- 按**标准编号**引用：查询时效服务 1 次，不抓取详情页。
- 按**名称 / 公告 ID** 引用：自动抓 1 次详情页解析编号（`--offline` 可跳过，但此时只接受按编号引用）。
- `confidence`：`parsed` = 编号来自公告正文解析；`unknown` = 编号来自时效服务的名称映射（同样可用，`--strict` 不因此报错）。**`warnings` 非空才是必须人工复核的信号。**

---

## 四、必须知道的事实

### 1. 正文可得性按年份分档

- **2014 年及以后**：通常可取得官方正文 PDF；拿不到值得按错误信息排查。
- **2014 年之前**：政府公告往往**仅公布名称与编号，不附带正文文件**。此时 `pull` 退出 `30` 是**预期内的事实**，不是工具故障。

判定字段：JSON 中的 `availability` 取值为 `in_window` / `pre_window` / `unknown`。所有失败体都会返回 `availability`（适用时）与中文 `hint`；向用户汇报结论时请同时说明该事实。

### 2. 编号可能为空，也可能一则公告同时提及新旧编号

约 39% 的政府公告正文不包含标准编号（如工作计划、产品目录通知等），此时：
- `code` 为 `null`（`pull` 仍可按公告名称落盘正文）；
- `cite` 会返回 `31` —— 这是正确拒绝，禁止自行编造编号。

**现行性以上游权威时效结论为准**（`which`/`cite` 自带），不要仅凭公告年份或只字片语自行推断。同一公告常见新旧两个编号并存，JSON 的 `codes[].role` 标明身份：

| role | 含义 | 可用于引用 |
|---|---|---|
| `target_of_revision` | 局部修订后改写的新编号 | ✅ |
| `primary` | 本次公告正式发布的主编号 | ✅ |
| `weak_primary` | 弱证据指向（仅名称命中或句式管辖不明确） | ❌ 需要人工确认 |
| `superseded_ref` | 被代替/废止的旧编号 | ❌ 不要引用 |
| `mentioned` | 正文提及但无法确定身份 | ⚠️ 需要人工确认 |

`weak_primary` 单独出现时不足以自动定版：缺少 `primary`/`target_of_revision` 强证据时，工具会返回 `32` 交由人选。

### 3. 引用串带 `warnings` 不得直接入文

`cite --json` 若返回非空 `warnings`（如版本被替代/废止、编号未核实或存在歧义），**不得直接写入正式监理文书**；应要求加 `--strict` 硬校验或交人工复核。

---

## 五、不要做的事

1. 不要并发轰炸：工具内部已按 host 限速 `1.2s/请求`，**不要多进程/多线程并发轰炸命令**。
2. 不要把 `30/32/33/34` 当网络故障反复重试 —— 重试不会改变既有事实。
3. 不要自己手拼 `cite` 引用串绕过工具：条款号规范化与版本角色核验必须经过 `guiju cite`。
4. 不要尝试修改程序行为或绕过 `*.gov.cn` 域名限制与请求限速。
5. 不要对 `pull` 取得的官方 PDF 做公开镜像、二次分发或转售。

---

## 六、JSON 输出形状速查

```jsonc
// pull 成功
{"ok":true,"code":"GB 55037-2022","path":"/…/library/…__3754d6a9ccc6.pdf",
 "sha256":"…","bytes":5115350,"magic":"pdf","confidence":"parsed",
 "published_on":"2026-09-23","availability":"in_window","source_page":"…","attachment":"…"}

// cite 成功
{"ok":true,"cite":"《老年人照料设施建筑设计标准》JGJ/T 450-2018 第5.2.1条",
 "code":"JGJ/T 450-2018","code_key":"JGJ/T450-2018","clause":"5.2.1",
 "confidence":"parsed","warnings":[],"url":"…"}

// 失败（任何命令统一形状）
{"ok":false,"exit":33,"reason":"ambiguous_attachment","error":"…","hint":"…",
 "candidates":[{"label":"…","url":"…"}]}

// status 成功
{"ok":true,"announcements":3255,"editions":0,"mentions":0,"synced_at":"…",
 "coverage":{"full_text_since_year":2014,"in_window":1777,"pre_window":1477,"statement":"…"}}
```

`cite --strict` 失败时返回的错误体不包含 `cite` 字段，禁止从失败载荷猜测引用串；需要候选参考请去掉 `--strict` 重跑并显式审查 `warnings`。

---

## 七、最小心智模型

```text
政府公告（官方发布网页）
   └─ 可能含 0..N 个标准编号
         └─ 每个编号对应一个版本身份（codes[].role：primary / target_of_revision / superseded_ref / mentioned）
               └─ 可能附带 1 份官方正文（PDF，文件名带 sha256 指纹）
                     └─ 核对时效与角色后生成标准引用串（cite）
```
