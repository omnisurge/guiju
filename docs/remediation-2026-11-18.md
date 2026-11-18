# 根源性修复方案（2026-11-18）

- 状态：**已批准，A1 已完成（2026-11-18），B1 待开工**；与 ADR 0004 配套
- 关系：问题清单见 `docs/audit/code-review-2026-11-18.md`；架构决策见 `docs/adr/0004-contract-first-hardening.md`
- 目标：不是"把 10 条 P1 修一遍"，而是把产生这 10 条的六个根因收口，做到"修一类、不留同类"。
- 交付：1.1.1（安全热修，不改退出码/DB/工具形状；MCP 路径收紧是安全例外）+ 1.2.0（契约与架构收口）。

---

## 一、复盘：止血修复的七种形态

上一轮（2026-11-12）修了 5 High + 22 Medium；这一轮仍发现 10 P1。逐条对照，至少 7 条是残留或副作用：

| 上轮项 | 提交 | 当时做法 | 留下的洞 | 本轮 |
|---|---|---|---|---|
| H1 目录缺口 | d4d9cc5 | 加 catalog_complete 门禁；空页标 completed | 完成判定不看 seen/total；补丁本身新增"空页=完成"；全局 meta | P1-1 |
| H4 限速并发 | d4d9cc5 | 只给 compliance._throttle 加锁 | currency/httpx.py 同构代码未动 | P1-5 |
| M5 多 primary | 434e328 | 给 primary 分支加分族检查 | target_of_revision 分支仍 max(code_key)，且优先于 primary | P1-9 |
| M6 cite 提示 | 434e328 | 修的是 DB 兜底 confidence；SourceUnreachable 的 note 仍在 current_hint | note 不展示，strict 仍只看 warnings | P1-6 |
| M9 落库缓存 | 434e328 | CachedCurrency(store=store) | native 分支提前 return，store 缓存不可达 | P3 |
| M18 分页 | 01937c5 | 给 csres 加 pagination 字段与 warning | 仍只请求 pageNum=1；字段没有消费者 | P3 |
| M21 负缓存 | 434e328 | 注释澄清"空成功可缓存" | 缺 key/解析失败仍返回 [] 并缓存 | P1-4 |
| M4 源路由 | 434e328 | lookup/cite 按 URL 归属源 | pull._pull_one 仍用 args.source | P2 |
| M12 MCP 降级 | 434e328 | which/CLI 暴露 stale | MCP query/verify 未接 | P2-12 |

还有三条是同一模式：P1-2（Low 的 416 HEAD 比对只覆盖一种续传异常，没校验 200/206 的剩余长度）、P1-7（M17 把 native 的 31 改成 34，但 cite 没接住）、P1-8（M15 给 _batch 加了非 GuijuError 兜底，没在 40 时停）。合起来，10 个 P1 里至少 7 个能追到上一轮修复的残留或副作用。

归纳成七种形态：

1. **只修症状闸门**：H1 加了"是否已完整"的闸门，但没有定义"完整"的证据。
2. **只修一套实现**：H4 只修 compliance，另一套 HTTP 栈原样保留。
3. **用字段/告警代替行为**：M18 加了 pagination 字段，没有真的翻页。
4. **只修一个分支**：M5 修了 primary，漏了 target。
5. **把提示塞进不可见槽位**：M6 的 note 进了 current_hint，strict 看不到。
6. **注释澄清代替语义修复**：M21 明知负缓存有张力，只加注释。
7. **只修 CLI，漏服务面**：M12/M4 在 CLI 修了，MCP 与 pull 的另一条路径没修。

### 为什么测试没拦住

- 测试钉的是"当前输出"：csres 的 pagination 字段被测试钉住，但没人断言真的翻了第 2 页；e2e 硬编码 total_in_db == 3254，把"当前条数"当成契约。
- 覆盖缺口：compliance.get/_get_once/_detect_block 在 601 个测试中零执行（行覆盖 57.4%）；sources/mohurd.py 的目录/详情/附件解析覆盖 43.9%，attachments() 的 HTML→Attachment 没有 fixture。
- 没有跨面矩阵：CLI 有测试，MCP 没有同一 ref 的对照；which 有 stale，MCP query 没有。
- 没有"同类扫描"步骤：修 _throttle 时没有 grep 全仓所有 _throttle；修 target 时没有看 primary 的同类分支。
- 文档与测试互相背书：ARCHITECTURE 描述不存在的 application/ports，文档说"唯一闸门"而代码有两套栈。

## 二、根因

| 编号 | 根因 | 表现 | 收口决策 |
|---|---|---|---|
| R1 | 双实现漂移 | 两套 HTTP、两套源路由、两套编排、两套空结果 | D1/D3 |
| R2 | 状态与空值不可表示 | [] 同时表示四种含义；bool complete 同时表示四种状态 | D2/D4 |
| R3 | 全局单值 | catalog_complete、_last_call、共享 Store、schema 硬编码 | D1/D2 |
| R4 | 修复粒度错位 | 按报告条目修，不做同类扫描/跨面测试/契约更新 | D6 |
| R5 | 服务面与 CLI 同构 | MCP 拥有文件能力、默认 Store、root、无 HTTP 防护 | D5 |
| R6 | 文档与结构失真 | ARCHITECTURE 描述不存在的层；退出码表缺 34；e2e 硬编码 | D6/W7 |

## 三、修复原则

1. **一个 PR 一个不变量**：先写"这类问题不许再发生"的断言，再改代码。
2. **先改契约，再改实现**：类型、异常、存储键、退出码先定；实现按契约收口。
3. **同类一次扫完**：每次修复必须 grep 所有同构点（_throttle、source_of、empty return、default_backend、Store()、code_key max）。
4. **跨面测试**：至少覆盖 CLI + 一个内部调用面（MCP/application/store），不测报告指向的单函数。
5. **不新增全局可变状态**：module-level 缓存、默认 Store、隐式单例一律禁止。
6. **不用字段/告警代替行为**：字段必须有消费者和断言；告警必须改变决策或退出码。
7. **文档/测试与代码同源**：退出码表、目录条数、e2e 基线从代码/运行结果生成，不硬编码。

## 四、目标架构

目标目录（1.2.0）：

    guiju/
      net/                 # 唯一出站层
        transport.py       # 超时/重试/退避/重定向/限速/IDNA/大小上限
        policy.py          # BodyPolicy / MetadataPolicy
        body.py            # 正文下载（唯一可写文件）
        metadata.py        # 元数据 GET/POST（绝不写文件）
      application/         # 唯一编排层
        context.py         # AppContext
        resolve.py         # 编号/名称/URL/ID 定位
        sync.py            # 目录同步
        pull.py            # 正文获取
        cite.py            # 引用串 + 时效检查
        status.py          # 目录/版本状态
      domain/              # 纯逻辑：编号角色、引用、附件、可用性
      sources/             # 站点解析：HTML/JSON → DTO，不做编排、不写文件
      store/               # SQLite：SQL 与迁移
      cli/                 # 参数解析 + 渲染（变薄）
      mcp.py               # 传输 + 薄适配（或 mcp/ 包）
      compliance.py        # 兼容 facade（1.2.0 删除）
      currency/            # 适配器 + 缓存（httpx 删除）

依赖规则：

- cli / mcp → application → domain / sources / store / net
- domain 不 import net/store/sources
- store 不 import net/sources
- net 不 import 业务模块
- sources 只做解析；网络调用通过注入的 net 或 application 传入的原始字节
- 只有 net.body 可以写正文文件；application 决定 out_dir；sources 不写盘

数据流：

    cli/mcp → application.resolve/pull/cite
      → domain 判定（角色/歧义/引用）
      → store 读写（只读或事务）
      → sources 解析（不联网）
      → net.body/net.metadata（唯一出站）
      → render/JSON 或 MCP result

## 五、工作流

### W1 出站与下载（net）

**目标**：P1-2、P1-5、P2-14/15/16/17 一次收口；全仓只剩一套出站。

**范围**：新增 guiju/net/；迁移 guiju/compliance.py、guiju/currency/httpx.py；改 sources/mohurd.py、sources/shzjw.py、currency/*.py 的调用。

**步骤**：

1. 抽出 Throttle 类：per-host 锁包住"读 last → 计算 sleep → 写 last"；实例持有状态，注入 clock/sleep；单测用 Barrier 强制读-读交错。
2. 抽出 RedirectGuard：逐跳校验 host policy；跨 host 重新限速；拒绝跨源正文重定向。
3. 抽出 Request：timeout、重试/退避、body 上限、block marker、IDNA、probe 记录；错误类型统一为 SourceUnreachable/NeedsHuman/ComplianceDenied。
4. BodyPolicy：gov.cn 白名单；content-type/magic；下载完整性（Content-Length/Content-Range）；If-Range 续传；.part 只在 out_dir；416 不盲信。
5. MetadataPolicy：注册表 host+path 白名单；GET/form/json；不写文件、不返回正文；与 BodyPolicy 共享 block 检测和限速。
6. compliance.py 变成 facade：get/post_json/download 调 net.body/net.metadata；currency/httpx.py 的 HttpClient 调 net.metadata。
7. 删除重复的 _last_call/_throttle/_check_url/_MetadataRedirect；grep 确认没有第三套。

**测试**：

- 两线程并发 _throttle：currency 路径必须 sleep 1.2s（现为零）。
- 截断响应（声明 10009 实收 509）必须抛错，不能返回 509。
- 206 起点/总长不符、If-Range 不匹配必须丢弃 .part 重下。
- 403/429/200 人机页必须 40；post_json 的 200 HTML 也必须 40。
- 跨 host 重定向、IDN、超大 body、probe 记录。
- AST/grep：只有 guiju/net 可以 import urllib.request/http.client；只有 net.body 写文件。

**验收**：P1-2/P1-5 关闭；P2-14/15/16/17 关闭；e2e 与真实下载回归；无行为回归。

**回退**：facade 保留旧入口；按阶段 revert；不设运行时开关（避免双路径长期存在）。

### W2 目录与同步状态

**目标**：P1-1、P2-1、P2-2；完成判定有证据，状态按源。

**范围**：syncengine.py、autosync.py、store/sqlite.py（meta 读写）、cli/commands/sync.py、status.py、cli/lookup.py（catalog 注入）。

**步骤**：

1. 定义 CatalogPage(items, total, has_more) 与 SyncOutcome(new, seen, best_total, complete, complete_reason, truncated, source)；sources 的 catalog_page 返回 CatalogPage，旧 tuple 在适配层兼容。
2. 完成规则：has_more is False → complete；total 已知且 seen >= best_total → complete；空页但 seen < best_total → incomplete(empty_page_before_total)；total 未知且 has_more 未知 → incomplete(total_unknown)。空页不再直接 complete。
3. 按源命名空间 meta：sync:<source>:complete/synced_at/total/seen/reason；旧 catalog_complete/synced_at 只作 mohurd 的兼容读；写入时同时更新旧的 synced_at 供旧版读取（过渡期），但不再写旧 complete。
4. autosync 按源判断：哪个源的相关状态过期就刷新哪个；至少保证 shzjw 的 sync 不会把 mohurd 标 fresh。catalog payload 输出 sources 数组与 aggregate（最坏）。
5. safe_sync：显式 sync 与 autosync 共用 flock；锁失败返回 refreshing 并说明"另一个进程在同步"，不再谎报；冷却写入用 suppress 包住，失败不阻断命令。
6. status 的 age/schema 从常量/各源状态计算；输出每源 complete/total/seen/reason。

**测试**：空页（total=10, page1=2, page2=[]）；99/100 页大小；total=0；两源先后 sync；autosync 冷却写失败；时钟回拨；显式 sync 抢锁。

**验收**：P1-1/P2-1/P2-2 关闭；真实目录 3256 条连续两次增量无缺口；status/catalog 能解释每个源的 complete 原因。

**回退**：旧全局 meta 仍可读；syncengine 保留旧返回字段；逐步切。

### W3 时效空值与适配器

**目标**：P1-4、P2-17/18；空结果不再伪装，解析失败不再负缓存。

**范围**：currency/{httpx,ccsn,stdsamr,dbba,csres,jtst,shzjw,native,fallback,codes}.py；store/currency.py。

**步骤**：

1. 契约：适配器缺必需 key、结构不符、页面有数据痕迹但 0 条 → NeedsHuman(50)；只有源明确回答"没有"才返回 []。
2. 提供 require_keys(payload, keys) 与 parse_or_fail(rows, evidence)；三个 JSON 适配器先校验 List/Total/records/total。
3. NativeCurrency 只缓存权威空结果；解析失败/异常不 put；add 单测断言 puts=0。
4. fallback 错误类型取最严重（50 > 20）；主源成功空仍不转备源（ADR 0002 裁决）。
5. csres：实现 pageNum 翻页（fixture 25/页）；无 total 时返回 has_more=None，不把部分当完整；告警必须进 Outcome/CLI。
6. 状态口径：ccsn "在编" → unknown（除非源明确"即将实施"）；AbolishDate + 无 ReplaceStandardCode → withdrawn；有 ReplaceStandardCode → superseded。
7. same_family 用完整基号（JTG/T 3360-01 与 -02 不同族）；_check_url 规范化 ;jsessionid。
8. csres 明文 http：优先 https；若站点不支持，显式记录风险并禁止用于正文。

**测试**：错误 JSON、改版 HTML、真实空、部分空、分页、状态映射、同族边界、;jsessionid。

**验收**：P1-4/P2-17/P2-18 关闭；错误 body 不再产生 24h 假阴性；csres 第 2 页可见。

**回退**：保留旧解析函数名；新契约测试先红后绿。

### W4 存储与错误路径

**目标**：P2-3/5/6/7/8/9；存储失败可解释，事实不被污染。

**范围**：store/sqlite.py、store/schema.sql、cli/__init__.py、autosync.py。

**步骤**：

1. Store 构造的 sqlite3.DatabaseError/OperationalError 映射为 NeedsHuman(50)/BadUsage(2)，main 统一输出 JSON；损坏库不再裸 traceback。
2. PRAGMA journal_mode=WAL、busy_timeout；写路径对 locked 做有限重试；OperationalError 映射为可重试的 20/50，不再 internal。
3. upsert_announcement 改 ON CONFLICT DO UPDATE（title/doc_name/category/published_on/fetched_at），保留 id 与首次 fetched_at 的语义写清。
4. persist_codes 在事务内先删该 ann 的旧 mention 再写；mentioned→superseded_ref 只在同族同号时成立；非 primary 的 confidence 按证据写（decisive→parsed，否则 ambiguous），不再一律 parsed。
5. find_by_code 的 LIKE fallback 转义 %/_；多 key 仍判 ambiguous；补 resolve("GB%") 测试。
6. _ensure_additions 只在表缺失时执行 DDL；只读模式跳过；close 明确 commit/rollback；cross_thread 加锁或禁用。
7. schema 版本策略：不升 v3；若未来升，先做 v2→v3 迁移、备份、回滚测试；未知版本给可操作提示。

**测试**：损坏库 CLI JSON；双进程/双线程写；重扫少 mention；无关 mention 不被降级；upsert 更新；LIKE 通配；只读 HOME。

**验收**：P2-3/5/6/7/8/9 关闭；资源警告清零；并发写不再 exit 1。

### W5 应用层与 MCP

**目标**：P1-8/P1-10、P2-4/10/11/12/13；CLI/MCP 共用编排，服务面无文件能力。

**范围**：新增 guiju/application/；cli/commands/*；cli/__init__.py；mcp.py；deploy/guiju-mcp.service；docs/deploy。

**步骤**：

1. AppContext（store/currency/out_dir/offline/allow_network/read_only/home）；入口构造；禁止深处 new Store()/default_backend()。
2. application.resolve/sync/pull/cite/status：把 cli/lookup.py 的编排、cite 的时效检查、pull 的附件选择搬入；cli/lookup.py 变 shim。
3. CLI 命令只做参数/渲染；MCP 用同一 application，read_only=True；--db 生效。
4. MCP batch：先限制 list/out 在 GUIJU_HOME 白名单目录（1.1.1 安全热修）；1.2.0 增加 codes 内联参数，标记 list/out deprecated；消费者确认后删除文件路径。
5. MCP stdio：handle 校验 params 类型；serve_stdio 每行 try/except；HTTP 兜底回显 id。
6. HTTP 硬化：loopback 默认；--allow-remote 才可外绑；Origin/Host；Content-Type application/json；socket timeout；线程上限；GET /mcp 405；通知不回包。
7. systemd：User=guiju、ProtectSystem=strict、ReadWritePaths 只 GUIJU_HOME、PrivateTmp、NoNewPrivileges；保留旧 unit 回退。
8. 退出码：batch 40 立即 break；cite strict 20/34；pull --source 未知 → 2；--json 无子命令 → 2 且 help 到 stderr。

**测试**：CLI/MCP 同一 ref 矩阵；MCP 无路径读写；--db；stdio 畸形 params；batch 40 后零调用；Origin/Content-Type/timeout；systemd unit 语法。

**验收**：P1-8/P1-10、P2-4/10/11/12/13 关闭；MCP 在只读 Store 下无写盘（除 native 缓存需显式关闭/隔离）。

### W6 领域确定性

**目标**：P1-3/P1-9、P1-6/P1-7、P2-11/P2-19；不猜编号、不抓错书。

**范围**：domain/announcement.py、domain/attachment.py、domain/codes.py、cli/commands/pull.py、cli/commands/cite.py、cli/commands/which.py。

**步骤**：

1. extract_names(title) 返回全部《》名；doc_name 保留第一个仅作展示；7 条真实多名称公告做 fixture。
2. pick 接收 target_names/ref_name：按名称 pull 用该名称；按编号/ID 且标题多本 → 33（candidates），不用 doc_name 兜底；单本公告保持现状。
3. classify 的 target 分支与 primary 同规则：多 target、target+primary 跨族 → ambiguous(32)；单个 target 才 parsed。
4. cite 的 CurrencyCheck：ok/unreachable/unsupported/no_record/offline；非 strict 输出 + warning；strict 不可达→20、未覆盖/无记录→34、其他 warning 维持 32；confidence 从 primary mention 推导。
5. which 去掉第二次 currency lookup；34 保留给状态查询；cite 不再因 34 丢引用串。
6. 发布词表加"批准"；_CODE_RE 支持 DG/TJ08-2007-2026；"废止GB ..." 语序判旧版。

**测试**：7 条真实标题；按第二名称/编号/ID pull；多 target 两种句式；cite 断网/34/无记录/offline；which from_cache；批准分段；DG/TJ 年版。

**验收**：P1-3/P1-9/P1-6/P1-7/P2-11/P2-19 关闭；多名称公告不再抓错；cite 语义与 AGENTS 一致。

### W7 工程、文档、测试

**目标**：P2-20 与 F 组；文档/测试不再钉旧行为。

**范围**：scripts/ci.sh、scripts/e2e.py、tests/*、README/ACCEPTANCE/INSTALL/ARCHITECTURE/AGENTS、.github/workflows。

**步骤**：

1. 测试关闭 Store/McpServer（ResourceWarning 清零）；GUIJU_HOME 用 fixture 恢复；test_guard 补断言。
2. e2e：total_in_db == total_reported；baseline 用 fixture/最小条数，不写死 3254/40。
3. CI：ruff 缺失时明确报错或跳过并标注；新增 docs 检查（退出码表含 34、README 测试数不写死）；覆盖率门禁（net/compliance/source parse）。
4. 文档：ARCHITECTURE 按 1.2.0 实际结构重写；README/ACCEPTANCE/INSTALL 版本与测试数从脚本更新；AGENTS 补 cite 34/strict、MCP batch 变更。
5. Windows：若选 POSIX-only，pyproject/README 明确；否则 msvcrt 锁 + Windows CI。
6. 发布说明：更多 32/33/50 是安全行为；MCP batch 迁移；DB 不变。

**验收**：CI 绿；ResourceWarning 0；e2e 不因公告增减误红；文档与代码一致。

## 六、阶段计划

| 阶段 | 版本 | 内容 | 退出条件 | 估算 |
|---|---|---|---|---|
| P0 | — | 冻结、分支、characterization 测试、ADR 批准、跟踪清单 | 任何改动前有可对比输出 | 2-3 人日 |
| A1 | 1.1.1 | 安全热修：P1-2、P1-5、P1-8、P1-10（路径白名单）、P2-2、P2-3、P2-4 | 退出码/DB/工具形状不变；回归绿；hk 灰度 | 3-5 人日 |
| B1 | 1.2.0-alpha | 契约修复：P1-1、P1-3、P1-4、P1-6、P1-7、P1-9、P2-5/6/7/11/19 | 10 P1 全关；契约测试过 | 6-8 人日 |
| B2 | 1.2.0-beta | net 收口（W1 全量） | 只有 net 发网络；并发/截断/续传过 | 8-10 人日 |
| B3 | 1.2.0-beta2 | 状态/空值/存储收口（W2/W3/W4 全量） | 多源/空页/负缓存/并发过 | 10-12 人日 |
| B4 | 1.2.0-rc | application/MCP/领域收口（W5/W6 全量） | CLI/MCP 矩阵；MCP 无文件能力 | 12-14 人日 |
| B5 | 1.2.0 | 文档/门禁/发布（W7） | 全量 CI + 回退演练 | 4-5 人日 |

一人顺序执行约 45-57 人日（9-11 周）；两人按 W 并行约 6-7 周。A1 可独立先行，B1 与 B2 可部分并行（W6 与 W1 文件不重叠）。

## 七、缺陷映射

### P1（10 条）

| 编号 | 问题 | 工作流 | 阶段 | 关键测试 |
|---|---|---|---|---|
| P1-1 | 同步完成判定 | W2 | B1/B3 | 空页、99/100、total=0 |
| P1-2 | 下载截断 | W1 | A1/B2 | 10009→509 必须失败 |
| P1-3 | 多标准附件错配 | W6 | B1 | 7 条真实标题、第二名称/编号/ID |
| P1-4 | 空结果负缓存 | W3 | B1/B3 | 错误 JSON、改版 HTML、空结果 |
| P1-5 | currency 限速无锁 | W1 | A1/B2 | 两线程 Barrier |
| P1-6 | cite strict 静默 | W6 | B1 | 断网 + strict |
| P1-7 | cite 34 丢引用串 | W6 | B1 | 未覆盖前缀 + 非 strict |
| P1-8 | batch 40 继续 | W5 | A1 | 40 后零后续调用 |
| P1-9 | 多 target 选错 | W6 | B1 | 两本修订、发布+修订 |
| P1-10 | MCP 任意文件 | W5 | A1/B4 | /etc/hosts、../、symlink |

### P2（20 条）

| 编号 | 问题 | 工作流 | 阶段 |
|---|---|---|---|
| P2-1 | 多源水位 | W2 | B3 |
| P2-2 | autosync 冷却写失败 | W2 | A1 |
| P2-3 | 损坏库裸 traceback | W4 | A1 |
| P2-4 | stdio 非 dict 打死服务 | W5 | A1 |
| P2-5 | mention 角色/confidence 污染 | W4 | B1 |
| P2-6 | 旧 mention 不清理 | W4 | B1 |
| P2-7 | announcement insert-only | W4 | B1 |
| P2-8 | find_by_code 通配 | W4 | B3 |
| P2-9 | 无 WAL/并发写 | W4 | B3 |
| P2-10 | MCP 忽略 --db | W5 | B4 |
| P2-11 | cite confidence unknown | W6 | B1 |
| P2-12 | MCP 丢 exit/stale | W5 | B4 |
| P2-13 | MCP HTTP 无防护 | W5 | B4 |
| P2-14 | 403/429 归 20 | W1 | B2 |
| P2-15 | 续传无 If-Range | W1 | B2 |
| P2-16 | _is_pdf 失效 | W1 | B2 |
| P2-17 | ;jsessionid/同族 | W3 | B3 |
| P2-18 | ccsn 状态/fallback 50→20 | W3 | B3 |
| P2-19 | "批准" 分段 | W6 | B1 |
| P2-20 | Windows fcntl | W7 | B5 |

### P3（约 30 条）

按工作流归并：

- W1：block 检测 8KB/2MB、GET 无 body 上限、跨进程限速、csres 明文 http。
- W2：status 硬编码 schema/age、catalog_meta_corrupt、status 无 per-source。
- W3：csres 单页、ccsn 截断文案、HasNextPage bool、HttpClient.close、legacy 缓存键。
- W4：_ensure_additions 重复 DDL、cache_currency 提前 commit、cross_thread 无锁、backup 秒级、close 静默回滚。
- W5：ReadOnlyStore 黑名单、announcement 写 currency-cache、cache 参数忽略、api_mode verdict、attachment_url null、JSON-RPC 通知/错误码、GET /mcp 405。
- W6：_VERSION_SUFFIX 死正则、_CODE_RE 年版、"废止GB" 语序、_PUBLISH_WORDS。
- W7：ResourceWarning、GUIJU_HOME 泄漏、test_guard 空断言、ci.sh 离线 ruff、e2e 硬编码、workflow 死配置、README/ACCEPTANCE/INSTALL 漂移、exit 34 文档。

## 八、迁移、兼容与发布

1. **1.1.1（A1）**：只做安全热修，不改成功语义/退出码/DB/MCP 工具形状；MCP batch 只把路径限制到 GUIJU_HOME 白名单（消费者若用 /tmp 路径需改）。灰度到 hk 时保留旧 unit/wheel。
2. **1.2.0（B1-B5）**：语义变化集中在这里；发布说明逐条列"更多 32/33/50 是安全行为"。
3. **MCP batch**：B1 先加 codes 内联参数；B4 标 list/out deprecated；1.3.0 再删。消费者清单：kb-system、jl-docx、DSH 当前配置，逐个确认。
4. **数据库**：P0-P4 不升 schema；用 corpus_meta 命名空间键。若 B3 发现必须新表，先做 v2→v3 迁移 + 备份 + 回滚测试，再升。
5. **兼容层**：compliance.py、currency/httpx.py、cli/lookup.py 保留到 1.2.0；1.2.0 删除时同步改测试/文档。
6. **回退**：每阶段独立 revert；DB 备份；MCP 旧 unit；NAS 不动（ADR0048 冻结）。

## 九、质量门禁

新增契约测试（至少）：

1. test_sync_empty_page_is_not_complete
2. test_sync_server_short_page_respects_total
3. test_sync_total_unknown_is_incomplete
4. test_download_truncated_content_length_raises
5. test_download_resume_rejects_changed_etag
6. test_currency_missing_keys_raises_needs_human
7. test_currency_parse_failure_is_not_cached
8. test_currency_throttle_serializes_threads
9. test_multi_name_announcement_requires_target_name
10. test_multi_target_classification_is_ambiguous
11. test_cite_strict_fails_when_currency_unreachable
12. test_cite_survives_no_currency_record_with_warning
13. test_batch_list_stops_after_compliance_denied
14. test_mcp_non_dict_params_does_not_kill_stdio
15. test_mcp_batch_rejects_paths_outside_home
16. test_cli_mcp_parity_same_ref
17. test_store_corrupt_db_returns_json
18. test_persist_codes_does_not_demote_unrelated_mention

CI 门禁：

- 全量 unittest（两 Python 版本）+ ruff + e2e。
- docs 检查：退出码表含 34；README 不写死测试数；ARCHITECTURE 目录存在。
- 覆盖率：net 90%、compliance 80%、sources 解析 70%、store 80%（阈值可按实际调整，但不得低于当前）。
- e2e：total_in_db == total_reported；baseline fixture；不写死 3254。
- 真实目录回归：连续两次增量无缺口；hk MCP smoke。

## 十、风险

| 风险 | 缓解 |
|---|---|
| 大改回归 | P0 characterization；分阶段 strangler；每阶段全量 CI；旧 facade 保留 |
| 语义变严被当成退步 | 发布说明/AGENTS 写清；status 里给原因；新增 32/33/50 的 hint |
| MCP 消费者中断 | deprecation 窗口；codes 参数先行；消费者清单；保留旧工具名 |
| 迁移/DB 失败 | 不升 schema；备份；迁移测试；损坏库 50 而非 traceback |
| 工期膨胀 | 不做清单；每阶段可独立交付；不顺手加功能 |
| 加固影响可用性 | systemd 最小权限先灰度 hk；保留旧 unit；Caddy 侧可回退 |

## 十一、不做

- 不换语言、不加运行时依赖、不新增退出码。
- 不做 UI、常驻抓取、并发下载、全库正文、团体标准/图集。
- 不动 NAS（ADR0048 冻结）；不在本方案内改生产部署（systemd 加固另开窗口）。
- 不做与契约无关的重构（命名、目录美化、类型全覆盖）。
- 不用"注释澄清"或"只加字段"结案。

## 十二、跟踪清单

### P0

- [x] ADR 0004 批准（2026-11-18 所有者按默认批准）
- [x] 建 A1 分支 fix/a1-safety-20261118（P2 前再建 refactor/root-cause）
- [ ] 补 characterization 测试（同步/下载/附件/cite/MCP）
- [x] 记录当前 3256 条目录 baseline（评审报告 §0）
- [x] 建立缺陷→工作流→阶段的跟踪表（本文件第七节）

### A1（1.1.1）

- [x] P1-2 下载长度校验
- [x] P1-5 currency 限速锁
- [x] P1-8 batch 40 停止
- [x] P1-10 MCP 路径白名单（systemd 降权放 B4）
- [x] P2-2 autosync 冷却 suppress
- [x] P2-3 损坏库 JSON
- [x] P2-4 stdio 兜底

A1 实施记录（2026-11-18）：分支 fix/a1-safety-20261118；测试 601→617（新增 16 个 A1 契约测试）；
scripts/ci.sh 与 Python 3.10 全量单测均通过；未改退出码/DB schema/MCP 工具形状；
systemd 降权按计划留到 B4。

### B1（1.2.0-alpha）

- [ ] P1-1 同步完成判定 + 按源状态
- [ ] P1-3 多名称附件 33
- [ ] P1-4 适配器 fail-closed + 禁负缓存
- [ ] P1-6/P1-7 cite 语义
- [ ] P1-9 多 target 32
- [ ] P2-5/6/7 存储事实
- [ ] P2-11/P2-19 confidence/批准

### B2-B5

- [ ] W1 net 收口
- [ ] W2/W3/W4 状态/空值/存储收口
- [ ] W5 application/MCP 收口
- [ ] W6 领域收口
- [ ] W7 文档/CI/发布
- [ ] 1.2.0 回退演练
