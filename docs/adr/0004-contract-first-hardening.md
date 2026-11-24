# ADR 0004 · 契约优先的根源性修复（停止按点止血）

- 状态：**已批准（2026-11-18，所有者回复"按默认，开始 A1"）**
- 决策人：所有者
- 触发：2026-11-18 全仓代码通读（10 P1 / 20 P2 / 约 30 P3）
- 影响：出站网络、目录同步、时效空值、存储错误路径、应用层、MCP 契约、引用语义、部署单元、1.2.0 发布
- 关系：补充 ADR 0002（合并为一个产品）、ADR 0003（目录自保鲜），不推翻；问题清单见 `docs/audit/code-review-2026-11-18.md`；实施计划见 `docs/remediation-2026-11-18.md`

> **执行状态提示（2026-11-24，最终）**：D1-D6 仍是现行实施依据。B4 application/ports/adapters 与 MCP 服务面已于 2026-11-23 通过 reviewer-domain-mcp、reviewer-cli 双 APPROVE；P2-4 由所有者 2026-11-24 决定保持安全默认，P2-5 probe_sink 已接线。**1.2.0 已于 2026-11-24 正式发布（tag `v1.2.0`）**，wheel/sdist/DB/systemd 回退演练完成，hk 已部署强化 unit 与 1.2.0 并完成真实回退/再部署；NAS 未触碰。ADR 0004 第十节关闭条件已满足。2026-11-22 外部评审复核见 [ADR 0005](0005-external-review-2026-11-22.md)；B4/B5 证据见 [b4-application-mcp-20261123.md](../audit/b4-application-mcp-20261123.md)、[b5-release-gate-20261123.md](../audit/b5-release-gate-20261123.md)。

---

## 一、背景：为什么"再修一轮"不够

2026-11-12 的评审修复了 5 个 High、22 个 Medium；2026-11-18 的评审又发现 10 个 P1。逐条核对后，至少 7 条是上一轮修复的残留或副作用，不是新长出来的：

| 上轮项 | 提交 | 当时做法 | 留下的洞 | 本轮 |
|---|---|---|---|---|
| H1 目录缺口 | d4d9cc5 | 加 catalog_complete 门禁；空页标 completed | 完成判定不看 seen/total；补丁本身把"空页"当完成；全局 meta | P1-1 |
| H4 限速并发 | d4d9cc5 | 只给 compliance._throttle 加锁 | currency/httpx.py 的同构代码未动 | P1-5 |
| M5 多 primary | 434e328 | 给 primary 分支加分族检查 | target_of_revision 分支仍 max(code_key)，且优先于 primary | P1-9 |
| M6 cite 提示 | 434e328 | 修的是 DB 兜底 confidence；SourceUnreachable 的 note 仍在 current_hint | note 不展示，strict 仍只看 warnings | P1-6 |
| M9 落库缓存 | 434e328 | CachedCurrency(store=store) | native 分支提前 return，store 缓存不可达 | P3 |
| M18 分页 | 01937c5 | 给 csres 加 pagination 字段与 warning | 仍只请求 pageNum=1；字段没有消费者 | P3 |
| M21 负缓存 | 434e328 | 注释澄清"空成功可缓存" | 缺 key/解析失败仍返回 [] 并缓存 | P1-4 |
| M4 源路由 | 434e328 | lookup/cite 按 URL 归属源 | pull._pull_one 仍用 args.source | P2 |
| M12 MCP 降级 | 434e328 | which/CLI 暴露 stale | MCP query/verify 未接 | P2-12 |

这些修复有一个共同形态：**修在报告指向的那一行，没有修在产生这一类问题的地方**。具体有七种：

1. 只修症状闸门：H1 加了"是否已完整"的闸门，却没有定义"完整"的证据。
2. 只修一套实现：H4 只修 compliance，另一套 HTTP 栈原样保留。
3. 用字段/告警代替行为：M18 加了 pagination 字段，没有真的翻页。
4. 只修一个分支：M5 修了 primary，漏了 target。
5. 把提示塞进不可见槽位：M6 的 note 进了 current_hint，strict 看不到。
6. 注释澄清代替语义修复：M21 明知负缓存有张力，只加注释。
7. 只修 CLI，漏服务面：M12/M4 在 CLI 修了，MCP 与 pull 的另一条路径没修。

还有三条是同一模式：P1-2（Low 的 416 HEAD 比对只覆盖一种续传异常，没校验 200/206 的剩余长度）、P1-7（M17 把 native 的 31 改成 34，但 cite 没接住）、P1-8（M15 给 _batch 加了非 GuijuError 兜底，没在 40 时停）。合起来，10 个 P1 里至少 7 个能追到上一轮修复的残留或副作用。

如果继续按"报告条目"修，下一轮还会以另一种形式出现。必须先把契约立起来，再让所有实现收口。

## 二、根因

**R1 双实现漂移。** 同一职责有两套代码：正文出站 compliance 与元数据出站 currency/httpx；源归属 lookup.source_of 与 pull.args.source、localstatus.local_records；CLI 编排与 MCP 编排；JSON 适配器与 HTML 适配器的空结果。修一套，另一套继续错。

**R2 状态与空值不可表示。** list[Record] 的空列表同时表示"源明确说没有""响应缺字段""页面改版解析失败""服务不可达但被吞掉"；bool complete 同时表示"爬到底""早停""截断""上一轮已完整"；confidence 字符串同时承担"解析证据"与"展示文案"。类型不承载语义，调用方只能猜。

**R3 全局单值。** catalog_complete/synced_at 不按源；module-level _last_call；MCP 共享 Store 连接；schema 版本硬编码。多源、并发、多客户端下必然互相污染。

**R4 修复粒度错位。** 以"报告条目"为单位，不以"不变量"为单位；没有同类扫描、没有跨面测试、没有把修复写回契约。测试钉住的是某个函数的当前输出，不是"这类问题不许再发生"。

**R5 服务面与 CLI 同构。** MCP 复用了 CLI 的文件路径能力（batch list/out）、默认 Store、root 运行；HTTP 没有浏览器/CSRF 防护。CLI 的便利被原样带进了网络服务。

**R6 文档与结构失真。** ARCHITECTURE.md 描述 application/ports/adapters/migrations 与"唯一闸门"，实际代码是扁平 cli/lookup.py + 两套出站栈；退出码表缺 34；e2e 硬编码 3254。维护者按文档找结构，修的时候只能就近落刀。

## 三、决策

### D1 单一出站层：guiju/net/

规则：

1. 全仓只有 guiju/net/ 可以 import urllib.request / http.client 并发起网络 I/O。
2. net 提供两个策略：
   - **BodyPolicy**：只允许 *.gov.cn；可以写文件；只能由 net.body 写；负责正文下载完整性。
   - **MetadataPolicy**：只允许注册表里的 host+path；只返回字节，不写文件、不返回正文。
3. 两个策略共享：per-host 限速（锁保护）、重定向逐跳校验、超时/重试/退避、body 大小上限、block marker 检测、IDNA、probe 记录。
4. 正文下载必须校验 Content-Length（200）或 Content-Range 起点/总长（206）；续传带 If-Range（ETag/Last-Modified）；不符即失败并保留 .part。总长为 `*` 时视为不可验证，按 fail-closed 返回 20；不静默接受未知完整性的文件。
5. 不再有模块级可变状态；Throttle 是实例，注入 clock/sleep，便于并发测试。
6. compliance.py 与 currency/httpx.py 变成兼容 facade，1.2.0 删除；新代码不得直接引用。

为什么：P1-2/P1-5 与 P2-14/15/16/17 都是同一类"出站策略"问题；只要还有第二套栈，就还会漂移。

### D2 状态与空值契约

规则：

1. 目录页返回 CatalogPage(items, total, has_more)，不再用裸 tuple 让调用方猜页大小。
2. 同步完成必须有证据：seen >= total（total 已知）；has_more is False；或源明确声明结束。空页只有在 seen >= best_total 时才算完成；否则 complete=0 + complete_reason。
3. 同步状态按源命名空间化：sync:<source>:complete/synced_at/total/seen/reason；旧全局键只作为 mohurd 的兼容读。autosync 按相关源判断，aggregate 取最坏。
4. 时效适配器：缺必需字段、结构不符、页面有数据痕迹但 0 条 → NeedsHuman(50)；只有"源明确回答没有"才返回 []。
5. 只有权威空结果可以进缓存；解析失败、异常、部分结果一律不写负缓存。
6. fallback 的错误类型取所有分支中最需要人处理的级别（50 > 20）；主源成功空结果仍不转备源（ADR 0002 裁决不变）。
7. 第一阶段不升 schema：用 corpus_meta 的命名空间键实现；确需新表时，先补 v2→v3 迁移框架，再动 schema。

为什么：P1-1/P1-4/P2-1 的根因是"完成/没有"被压成 bool 与空列表；不把语义写进类型和存储键，修一处还会漏一处。

### D3 应用层收口

规则：

1. 新增 guiju/application/：resolve、sync、pull、cite、status。所有编排在这里；CLI 命令只解析参数、调用、渲染。
2. AppContext 显式携带 store、currency、out_dir、offline、allow_network、read_only、home；由入口构造，禁止在深处 new Store()/default_backend()。
3. CLI 与 MCP 共用同一 application 函数；MCP 用 read_only=True 的 Store，--db/--home 与 CLI 一致。
4. cli/lookup.py 保留为兼容 shim，测试逐步改为 import application；1.2.0 删除重复实现。
5. CLI/MCP 契约测试矩阵：同一 ref 在两边输出同一 code/status/exit/告警。

为什么：P1-6/7/8/10、P2-10/11/12/13 都涉及"同一条业务在两处实现"；不共用编排，修一边必漏另一边。

### D4 领域确定性

规则：

1. 公告名称是多值：标题里所有《》名称都要保留（names），doc_name 只作展示；不再用第一个名称代表整条公告。
2. 附件选择必须知道用户要哪一本：按名称 pull 用该名称匹配；按编号/公告ID pull 时，如果标题有多本且无法唯一映射，返回 33（candidates），绝不用 doc_name 兜底。
3. classify 的 target_of_revision 分支与 primary 分支同规则：多个 target、target 与 primary 跨族并存 → ambiguous(32)，不取 max(code_key)。
4. cite 的时效检查是类型化结果（ok/unreachable/unsupported/no_record/offline）：
   - 非 strict：不可达/未覆盖/无记录都输出引用串 + warning；status=unknown。
   - strict：不可达 → 20；未覆盖/无记录 → 34；不静默通过。
   - confidence 从 primary mention 推导；name/ID/URL 路径不再恒为 unknown。
5. which 只做一次时效查询；34 的语义保留给"编号体系未覆盖"的状态查询；cite 不再因为 34 丢掉已生成的引用串。
6. 编号正则承认 DG/TJ08-2007-2026 这类"年版"；"批准"进入发布词表；"废止GB ..." 的语序要能判旧版。

为什么：P1-3/P1-9、P1-6/P1-7、P2-11/P2-19 都是领域判定在边界上"猜"；产品的核心竞争力是不抓错，必须把"无法唯一确定"变成显式 32/33。

### D5 MCP 安全边界

规则：

1. MCP 工具只做纯查询：query/verify/announcement；batch 最终只接受内联编号列表，返回结果，不接收文件路径。文件批量留在 CLI。过渡期（1.1.1-1.2.0）允许 GUIJU_HOME 下白名单路径并标 deprecated，1.3.0 删除。
2. MCP 不使用默认 Store；由入口注入只读 Store（sqlite mode=ro 或只读连接），不跑写 DDL。
3. HTTP：默认只绑 loopback；远程必须显式 --allow-remote 且要求 Bearer；校验 Origin/Host；只接受 application/json；socket timeout；线程上限；GET /mcp 返回 405；JSON-RPC 通知不回包。
4. systemd：User=guiju、Group=guiju、ProtectSystem=strict、ReadWritePaths 只给 GUIJU_HOME、PrivateTmp、NoNewPrivileges、RestrictAddressFamilies。
5. 部署文档写明 token 轮换、Caddy 侧来源限制、MCP 不暴露 batch 文件能力。

为什么：P1-10 不是参数校验问题，是"服务面拥有 CLI 的文件系统能力 + root + 公网"的模型错误。

### D6 修复协议与文档门禁

规则：

1. 每个修复 PR 必须包含：不变量陈述、同类扫描结果（grep 所有同构点）、跨面契约测试（至少 CLI + 一个内部调用面）、文档/CHANGELOG 更新。
2. 禁止"只加字段/告警不改行为"；如果字段没有消费者，不许作为修复交付。
3. 禁止用注释澄清代替语义修复；注释只能解释已经正确的行为。
4. 新增模块级可变状态、新增第二套网络/路径/空值实现，一律评审不通过。
5. ARCHITECTURE 与实际目录同源；退出码表从 errors.EXIT_TABLE 生成/校验；e2e 不硬编码目录条数。
6. 每个阶段结束跑全量 CI + 新增契约测试 + 真实目录回归；不满足不进入下一阶段。

## 四、后果

**正面**：P1 中 8 条能在同一套契约下被"关一类"而不是"关一条"；CLI/MCP 行为一致；多源/并发正确；MCP 安全边界清晰；文档与测试不再钉住旧行为。

**代价**：改动面大（net/application/工具链）；短期内 32/33/50 会变多（这是"不抓错"的代价，不是退步）；MCP batch 是 breaking change，需要消费者协调；1.2.0 之前需要保留 facade 与兼容测试。

**兼容**：退出码不新增、不改语义（D4 的 cite strict 20/34 映射需同步文档）；JSON 只加字段；数据库不升 schema；旧模块保留一个 minor。

## 五、分期与门禁

| 阶段 | 目标 | 交付 | 退出条件 | 估算 |
|---|---|---|---|---|
| P0 | 冻结与基线 | 分支、characterization 测试、CI 绿、ADR 批准 | 任何改动前有可对比输出 | 2-3 人日 |
| P1 | 安全与语义修复 | 10 个 P1 的最终语义 + 契约测试；A1 1.1.1 + B1 1.2.0-alpha | 10 P1 全关；无回归；文档更新 | 9-13 人日 |
| P2 | 出站收口 | guiju/net/ + facade | 只有 net 发网络；并发/截断/续传测试过 | 8-10 人日 |
| P3 | 状态与空值收口 | CatalogPage/SyncOutcome/按源 meta；适配器 fail-closed；存储加固 | 多源/空页/负缓存/并发测试过 | 10-12 人日 |
| P4 | 应用层与 MCP 收口 | application/ + AppContext + 纯 MCP + systemd | CLI/MCP 矩阵过；MCP 无任意路径 | 12-14 人日 |
| P5 | 文档、门禁、发布 | ARCHITECTURE/AGENTS/README/CHANGELOG；CI；1.2.0 | 全量 CI + 回退演练 | 4-5 人日 |

一人顺序执行约 45-57 人日（9-11 周）；两人按 W 并行约 6-7 周。估算只用于排期，不构成承诺。

## 六、兼容、迁移与回退

- **退出码**：不新增；34 的适用面在 cite 上收窄（非 strict 成功 + warning，strict 34），需在 README/AGENTS/errors 同步。
- **JSON**：只加字段（catalog.sources、sync.complete_reason、currency_check 等），不改已有字段含义；MCP 错误体补 exit/reason/hint。
- **MCP batch**：从"文件路径"改为"内联编号列表"是 breaking change；1.2.0 前提供 deprecation 与消费者清单（kb-system/jl-docx/DSH），逐一确认后再切。
- **数据库**：P1-P3 不升 schema，用 corpus_meta 命名空间键；如确需新表，先实现 v2→v3 迁移、备份、回滚测试，再动 schema。
- **兼容 facade**：compliance.py、currency/httpx.py、cli/lookup.py 保留到 1.2.0；新代码不得新增调用。
- **回退**：每阶段独立可 revert；DB 先备份；hk 部署保留旧 unit 与旧 wheel；NAS 不动（ADR0048 冻结）。

## 七、风险

| 风险 | 缓解 |
|---|---|
| 大改导致回归 | P0 characterization 测试；分阶段 strangler；每阶段全量 CI；不一次删除旧路径 |
| 语义变严被当成退步 | 发布说明写清"更多 32/33/50 是安全行为"；status/日志给出原因；AGENTS 更新 |
| MCP 消费者中断 | deprecation 窗口；消费者清单；batch 先加内联参数，再删路径；保留旧工具名 |
| 数据库损坏/迁移失败 | 不改 schema；任何 schema 变更先做备份与迁移测试；损坏库走 50 而不是 traceback |
| 工期膨胀 | 明确不做清单；每阶段可独立交付；不顺手加新功能 |
| 服务面加固影响可用性 | systemd 先用 User=guiju + ReadWritePaths 最小集；灰度到 hk；保留旧 unit |

## 八、明确不做

- 不换语言、不加运行时第三方依赖、不新增退出码。
- 不做 UI、常驻抓取、并发下载、全库正文、团体标准/图集。
- 不动 NAS（ADR0048 冻结）；不在本 ADR 内改生产部署，P4 的 systemd 加固另开窗口。
- 不做"大爆炸"重写；不做与本次契约无关的重构（命名、目录美化、类型全覆盖）。
- 不用"注释澄清"或"只加字段"结案。

## 九、所有者拍板记录

2026-11-18 所有者回复"按默认，开始 A1"：以下 8 项按推荐默认执行；后续如需变更，另开 ADR 或在本节追加记录。

| # | 决策 | 采用 | 备选 |
|---|---|---|---|
| 1 | ADR 粒度 | 一个程序级 ADR 0004（本文件），计划单独一份 | 拆成 0004-0007 四个小 ADR |
| 2 | MCP batch | 内联编号、无文件路径 | 仅允许 GUIJU_HOME 下白名单目录 |
| 3 | cite 遇 34 | 非 strict 输出引用串 + warning；strict 34 | 保持整条失败（与 offline 不一致） |
| 4 | 多名称公告 | 无法唯一映射 → 33 | 先做 code→name 映射再选 |
| 5 | schema | 不升 v3，命名空间 meta | 新增 sync_state 表 |
| 6 | Windows | 声明 POSIX-only（当前 CI/部署） | 实现 msvcrt 锁并加 Windows CI |
| 7 | 版本 | 1.1.1 安全修复 + 1.2.0 架构 | 全部并入 1.2.0 |
| 8 | 跨进程限速 | 单进程锁 + 文档（AGENTS 已要求不并发调用） | 文件锁/令牌桶 |

## 十、关闭条件

- 10 个 P1 全部关闭，且每条都有跨面回归测试（不是只测报告指向的函数）。
- 出站只剩 guiju/net；目录状态按源；空值有类型/异常契约；application 是唯一编排；MCP 不接受任意路径（batch 文件路径在 1.3.0 前删除）。
- 文档、退出码表、e2e 与代码同源；ARCHITECTURE 描述的结构真实存在。
- 1.2.0 发布，完成一次回退演练（wheel/DB/systemd）。
