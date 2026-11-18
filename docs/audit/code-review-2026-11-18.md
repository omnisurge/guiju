# 规矩（guiju）全仓代码通读报告

- 日期：2026-11-18
- 范围：仓库 main（048620c）全部产品代码、测试、脚本、部署与文档；产品代码 53 个 Python 文件约 6924 行，测试约 8554 行。
- 方法：Lead 自读核心链路 + 6 个只读子评审分工通读（网络/文件、存储/同步、CLI、时效层、领域/MCP、测试/工程），Lead 对关键结论逐条交叉复现。
- 边界：只读评审；未改任何产品代码；未联网（所有实验用本地临时 GUIJU_HOME、假 HTTP 服务或静态代码路径）；新增的只有本报告。
- 基线：scripts/ci.sh 全绿（ruff + 601 个单测/1117 个子测试 + 离线 e2e 4/4 + 入口自检）；Python 3.13.5 与 3.10.21 均 601 通过；运行时零第三方依赖的结论复核成立。
- 修复方案：[根源性修复方案](../remediation-2026-11-18.md)（提议）
- 架构决策：[ADR 0004 · 契约优先的根源性修复](../adr/0004-contract-first-hardening.md)（2026-11-18 所有者按默认批准）
- A1 进展（2026-11-18）：P1-2/P1-5/P1-8/P1-10（路径部分）/P2-2/P2-3/P2-4 已修复并测试（617 单测通过）；其余 P1 见方案 B1。

## 0. 结论摘要

总体判断：这是一套工程纪律相当好的代码库——分层清楚、错误分类细、退出码契约完整、测试密度高、零依赖约束守得住。本轮没有发现 P0（无越权逃逸、无 SQL/路径注入、无任意网络出口），也没有发现"整体架构需要推倒"的问题。

但"状态"和"空结果"两个语义在多处被混用，产生了 10 条 P1。它们不是风格问题，是会直接造成抓错、缺口、假阴性、合规越线或主机失守的问题：

| 编号 | P1 问题 | 主位置 | 本轮验证 |
|---|---|---|---|
| P1-1 | 目录同步"完成"判定不看已抓条数：空页/服务端页大小小于请求值都会提前判完成，缺口被永久标成 fresh | syncengine.py:47-50,68-79；sources/mohurd.py:72-74 | 已实测两种触发 |
| P1-2 | 流式下载不校验 Content-Length/Content-Range，截断的半个 PDF 以成功落盘 | compliance.py:299-306 | 已实测 509/10009 字节无异常 |
| P1-3 | 多标准公告按第二本标准名 pull，会下载第一本的附件并 exit 0 | domain/attachment.py:79-99；cli/commands/pull.py:193 | 已用真实目录 7 条多名称公告复现 |
| P1-4 | 时效层把"缺字段/解析失败"当成"权威无记录"，并负缓存 24 小时 | currency/httpx.py:180-206；ccsn/stdsamr/dbba/csres/jtst；native.py:177-185 | 已实测三源返回 []、二次命中缓存 |
| P1-5 | 第二套出站闸门 currency/httpx._throttle 无锁，MCP 并发可绕过 1.2s/host 限速 | currency/httpx.py:90-95；mcp.py:368,393 | 已用读-读交错实验复现 |
| P1-6 | cite --strict 在时效服务不可达时仍 exit 0、warnings 为空 | cli/commands/cite.py:105,112,142-170 | 已实测 |
| P1-7 | cite 遇 34（前缀未覆盖）把已生成好的引用串整条丢掉 | cli/commands/cite.py:151-155 | 已实测 cite 由 0 变 34 |
| P1-8 | pull --list 命中 40 后继续处理后续编号，违反"40 立即停止" | cli/commands/pull.py:284-311 | 已实测 40 后仍跑 B/C |
| P1-9 | 多个 target_of_revision 时无条件取 max(code_key)，静默选错编号且 confidence=parsed | domain/codes.py:257-260 | 已实测两种真实句式 |
| P1-10 | MCP batch 的 list/out 无路径限制；按 deploy 单元以 root 运行，可任意读写主机文件 | mcp.py:171-210；deploy/guiju-mcp.service | 已实测任意路径写入成功 |

P2 共 20 条、P3 约 30 条，集中在：同步水位跨源共用、数据库损坏/并发路径、MCP 协议与结果面、citation 契约、时效源身份与状态口径、下载续传、工程门禁与文档漂移。

建议的修复顺序见第 6 节：先修 P1-1/2/3/9（数据正确性）与 P1-8/10（合规与安全），再修 P1-4/5/6/7 和同步/存储的 P2，最后处理 MCP 契约与清理项。

---

## 1. P1 详情

### P1-1 同步"完成"信号不可靠：空页和页大小都会提前收工

位置：
- guiju/syncengine.py:47-50：if not items: completed = True; break
- guiju/syncengine.py:70：if total and page * page_size >= total: completed = True; break
- guiju/syncengine.py:43、75-79：完成即写全局 catalog_complete=1 和 synced_at
- guiju/sources/mohurd.py:72-74：data.html 为空时返回 ([], 0)，是"有效但空"，不是异常

触发一（空页）：任一轮抓取中某页返回 HTTP 200 但 html 为空（WAF 软拦截、后端瞬时故障、分页参数被改）。crawl 把这一页当"目录到底"，即使 seen 远小于已报告的 total，也写 complete=1 + synced_at。

实测（Lead）：假源 total=10，第 1 页 2 条，第 2 页返回 ([], 0)。结果 crawl complete=True、new=2、total_reported=10、total_in_db=2，catalog_complete=1、synced_at 已写。下一次健康源在第 3 页有两条新公告，但增量在第 2 页就 quiet-stop，两条永远进不来，且 synced_at 继续刷新。

触发二（页大小）：mohurd 实际每页只回 99 条，但代码按请求的 page_size=100 计算 page * page_size >= total。total=3300 时第 33 页后即判完成，实际只收 3267 条。当前真实目录约 3254-3256 条还没触发（临界点 3268），也就是说总量再涨 12-14 条就会开始漏最后一页，而且仍然写 complete=1。

后果：H1 修好的 quiet-stop 闸门（第 68 行，依赖 catalog_complete）被一次误判永久打开；后续每次增量都在缺口前停住；agent 看到 catalog.state=fresh，会把"没收录"当事实（31 误导）。这同时是"不抓错优先于抓不到"和"否定结论必须可信"两条产品约定的破坏。

建议修法：
- 记录本轮见过的最大非零 total（best_total）；只有 seen >= best_total 才允许 completed=True。
- total 未知（0）时，空页/短页只能结束本轮，不能置 catalog_complete；或先重取该页一次再判。
- page_size 按服务端实际返回页大小校准，或干脆用 seen >= total 替代 page * page_size。
- 完成状态按源命名空间化（见 P2-1），并在 payload 里输出 seen/total/complete_reason，便于 agent 判断。

回归测试：空页不置 complete；total=3300/实际 99 条不置 complete；正常 total=3254 置 complete。

### P1-2 下载不校验声明长度：截断文件以 exit 0 落盘

位置：guiju/compliance.py:299-306。循环读取 chunk，读到空即 break，从不检查 resp.length 剩余量；随后校验 magic 只看文件头。Content-Length 已在响应里，但被忽略。

实测（Lead，本地假 HTTP 服务）：响应头声明 Content-Length: 10009，实际只发 509 字节（以 %PDF- 开头）后关闭连接。compliance.download 返回 {bytes: 509, resumed_from: 0}，没有异常；magic 检查会通过（%PDF-），pull 会把 509 字节当正文落盘并返回 exit 0。真实 probe 表里正文大小 4.6-67.5MB，网络抖动/Range 边界/代理截断都可能命中。

关联风险：续传没有 If-Range/ETag（P2-15），远端换版后旧前段 + 新后段会拼成一个 magic 正确但内容错乱的文件，同样 exit 0；416 且 HEAD 拿不到长度时仍接受旧 .part（P3）。

后果：坏 PDF 被当"已完成"；find_cached 按文件名 sha256 指纹判定，截断文件自身指纹会被记住，重跑返回 10 而不是重新下载；用户拿到的正文缺页/损坏，工具却报成功。这直接违反"不抓错"。

建议修法：
- 下载后按 Content-Length（200）或 Content-Range 的 total/start（206）校验实际字节数；不足即抛 SourceUnreachable，保留 .part 供下次续传。
- 206 必须校验 Content-Range 起点等于请求 start、总长一致。
- 续传请求带 If-Range（ETag/Last-Modified），不匹配就丢弃 .part 重新下载。
- 落盘后再校验 magic、大小下限与 sha256；测试用假服务覆盖截断、错位、换版三种情形。

### P1-3 多标准公告：按第二本标准名 pull，抓到第一本的正文

位置：
- guiju/domain/announcement.py:23-30：doc_name 只取标题里第一个《》。
- guiju/cli/commands/pull.py:193：pick(atts, ann["doc_name"], ...)，没有把用户查询的名称传进去。
- guiju/domain/attachment.py:91-99：名称完全匹配即返回唯一附件。

触发：公告标题形如"关于发布《A》《B》两项标准的公告"，doc_name=A。用户 guiju pull "B" 时，rank 因标题包含 B 命中该公告，_by_name 返回唯一结果；随后 pick 用 doc_name=A 匹配到 A 的附件，返回 OK。

实测（Lead，真实目录）：全库有 7 条标题含两个书名号的公告，例如 id=293 的"自然保护区工程项目建设标准 / 湿地保护工程项目建设标准"，doc_name 是"自然保护区…"。用临时目录构造同名公告，按第二本"自然保护区规范"检索命中该公告（via=name），pick 返回第一本"湿地保护规范.pdf"，verdict=ok。也就是说真实场景下会下载错书。

后果：正文抓错，且 exit 0、无 warnings；这是产品最严重的失败模式（比抓不到更糟）。多标准合订公告在住建部目录里不是孤例（已确认 7 条）。

建议修法：
- 公告解析保留标题里的全部《》名称（names 列表），不要只留第一个。
- pull 把用户查询的 ref/name 传给 pick；pick 在所有名称中匹配，匹配到第二本时选对应附件。
- 若公告有多本、用户名称无法唯一对应到某一本，返回 33（让人选附件），不要用 doc_name 兜底。
- 回归测试直接用真实 id=293 一类标题与两个附件。

### P1-4 "空结果"语义混淆 + 24 小时负缓存

位置：
- guiju/currency/httpx.py:180-188：json_rows 缺 key 返回 []；:200-206 json_int 缺 key 返回 0。
- guiju/currency/ccsn.py:31-33、stdsamr.py:31-33、dbba.py:34-36：没有校验 List/Total/records/total 等必需 key。
- guiju/currency/csres.py:115-124、jtst.py:129-133、stdsamr.py:82-91、dbba.py:85-94：检测到"有数据痕迹但 0 条"只 warnings.warn，仍返回 []。
- guiju/currency/native.py:177-185：空列表照常 cache.put。
- guiju/currency/fallback.py:47-50：主源成功空结果即终局，不转备源（这是所有者裁决，本身合理）。

触发：上游 200 + 合法 JSON 但不是预期结构（WAF 错误体、站点把 List 改名、接口报错），或 HTML 页面改版导致解析 0 条。适配器返回 [] → NativeCurrency 写缓存 → 24h 内同一编号第二次直接 from_cache=True，CLI 报"没收录/先 sync"。

实测（Lead）：
- parse_ccsn / parse_stdsamr / parse_dbba 对 b'{"code":500,"message":"system busy"}' 都返回 []。
- 用返回 [] 的假适配器：第一次 lookup 后 puts=1，第二次 lookup from_cache=True、puts 仍为 1，即空结果被当权威结论缓存了一整天。
- csres 的注释自己写着"不许静默成无命中"，但实现只是 stderr warning，返回值仍是 []。

后果：一次站点改版或 WAF 误伤，会让整个编号体系（GB/JGJ/CJJ/JTG/DB 等）在 24 小时内静默显示"无此规范"，且不会触发 50/备源；agent 会据此给出否定结论。这是"假阴性比抓不到更危险"的典型。

建议修法：
- 适配器显式要求必需 key（require_keys），缺失即抛 NeedsHuman(50)。
- 把"解析失败"与"成功但确实 0 条"分开：前者抛 NeedsHuman 或返回带 parse_failed 的结果；后者才允许空结果。
- NativeCurrency 只缓存明确权威的空结果；解析失败不写负缓存。
- 主源空=终局的裁决不变，但必须先把"解析失败"从"成功空"里拆出去。
- 回归测试：错误 JSON、改版 HTML、真实空结果三种路径。

### P1-5 第二套出站闸门无限速锁，MCP 并发可绕过 1.2s/host

位置：guiju/currency/httpx.py:90-95（_last_call 的读-睡-写无锁）；guiju/mcp.py:368 用 ThreadingHTTPServer，do_POST 每请求一线程，query/verify/batch 共享同一个 NativeCurrency。

实测（Lead）：用 BarrierDict 让两个线程都先读到空的 _last_call 再各自写，两个线程的 _throttle 都没有调用 sleep（calls=[]），即同时放行；同一实验下 compliance._throttle 的对照为 [1.2]，被锁串行。compliance.py:101-107 的 H4 修复只覆盖了正文下载，元数据栈未修。

后果：对 csres/ccsn/stdsamr/dbba/jtst 的元数据端点出现并发突发，违背"限速是产品一部分"；CHANGELOG 已记录本机 csres 曾被限流。可能触发 IP 封禁或人机验证，进而让时效层整体失效。

建议修法：与 compliance 完全一致，用模块级 threading.Lock 包住"读 last → 计算 sleep → 写 last"（持锁 sleep），或每 host 一个 Semaphore；MCP 的并发上限也应显式配置。回归测试用两个线程+Barrier 断言第二次一定 sleep。

### P1-6 cite --strict 在时效不可达时静默通过

位置：guiju/cli/commands/cite.py:105（status, current_hint, records = _check_currency）、:112（if args.strict and warnings）、:142-170（SourceUnreachable 时 return "unknown", note, records）。

触发：GUIJU_CURRENCY 端点不可达，或任何 SourceUnreachable；_check_currency 把"未做现行性校验"放进 note，作为 current_hint 返回；但 warnings 只在 status 为 superseded/withdrawn 时追加，strict 只看 warnings。

实测（Lead）：按编号引用 CJ/T 类本地已解析编号，Down.lookup 抛 SourceUnreachable，cite 输出 ok=true、status=unknown、warnings=[]、exit 0。--strict 完全没有起到"未核实则拒绝"的作用。

后果：AGENTS 承诺 warnings 非空才是必须复核的信号，--strict 是硬闸门；实际断网时它静默放行，用户会把未做现行性校验的引用写进正式文书。

建议修法：把 note 转成 warning（例如"时效服务不可达：本引用未做现行性校验"），或在 strict 下直接失败；离线/unknown 与"已校验为现行"必须可区分。回归测试：SourceUnreachable + --strict 必须非零。

### P1-7 cite 遇 34 丢掉已生成的引用串

位置：guiju/cli/commands/cite.py:151-155，只 catch SourceUnreachable，NoCurrencyRecord(34) 直接冒泡。

触发：编号前缀不在原生路由覆盖内（CJ/T、T/CECS 等），但该编号已在本地公告正文解析并落库。cite 先成功解析出 target 和引用串，随后 _check_currency 抛 34，整条命令失败，已生成的 cite 字段不会返回。

实测（Lead）：本地播种 CJ/T 123-2020 + primary mention，patch CachedCurrency.lookup 抛 NoCurrencyRecord。cite 直接以 34 结束，无 cite 字段；--offline 同一数据返回 0。也就是说联网比离线更不可用，这是契约倒挂。

后果：大量行业标准无法用 cite，尽管本地有可信的 parsed 编号；34 的语义是"时效路由没覆盖"，不等于"不能引用本地已核实编号"。

建议修法：_check_currency 捕获 NoCurrencyRecord（以及 MisconfiguredCurrency 的可恢复情形），按 unknown + warning 处理；只有 ComplianceDenied/QueryCancelled 继续冒泡。若产品坚持 34 必须整条失败，则 --offline 也应失败，不能两套语义。回归测试：34 下 cite 仍输出引用串并带 warning。

### P1-8 pull --list 命中 40 后继续处理后续编号

位置：guiju/cli/commands/pull.py:284-311，_batch 对每个 ref 捕获 GuijuError 后继续循环；最后才按最严重级别返回。

实测（Lead）：patch _pull_one，A-1 抛 ComplianceDenied(40)，B-2/C-3 正常。实际调用顺序 ['A-1','B-2','C-3']，返回 40；即工具在"命中人机校验、必须立即停止"之后又发了两次请求。

后果：直接违反合规红线（40 必须立即停止，不重试、不换 UA、不找镜像），也是对目标站点的继续施压；批量场景下可能把一次人机校验升级为 IP 封禁。

建议修法：捕获到 ComplianceDenied/exit 40 时立即 break，返回 40，并在汇总里列出未处理编号；其余错误（20/50/31）可以按现有汇总策略继续。回归测试断言 40 后 0 次后续调用。

### P1-9 多 target_of_revision 无条件取 max(code_key)，静默选错编号

位置：guiju/domain/codes.py:257-260：targets = [...]; if targets: return max(targets, key=lambda m: m.code_key)。没有像 primaries 分支那样做分族/唯一性检查；classify 随后把 target 视为决定性证据，confidence=parsed。

实测（Lead）：
- 句式 A"《甲标准》局部修订，编号修改为 GB/T 50362-2022；《乙标准》局部修订，编号修改为 GB/T 50378-2019"→ primary=GB/T50378-2019（乙），confidence=parsed；按甲名称 pull/cite 会拿到乙的编号。
- 句式 B"现批准《甲标准》…编号为 GB 55037-2022…；《乙标准》局部修订，编号修改为 GB/T 50378-2019"→ primary=GB/T50378-2019（target 优先于 primary），confidence=parsed；公告明明发布的是甲，工具却把乙的修订号当 primary。

后果：编号/文件名/引用串错配到另一本规范，且没有任何 ambiguous 信号。这类错误在监理文书里是事故。

建议修法：
- targets 分支也做分族检查：多个 target 或 target 与 primary 并存且分属不同规范族时返回 None（ambiguous）。
- 只有唯一 target 且其名称/上下文与本次公告对应时才作数；否则交 32。
- 补测试：两本同时局部修订、发布甲+修订乙、同族两版本三个用例。

### P1-10 MCP batch 任意文件读写 + root 服务

位置：guiju/mcp.py:171-210：list 直接 Path(list_path).read_text()，out 直接 Path(out_path).write_text()，无任何目录限制、无 symlink 检查。deploy/guiju-mcp.service 没有 User=，systemd 默认 root；服务监听 127.0.0.1:18083，经 Caddy 的 Bearer 端点对外。

实测（Lead）：McpServer.handle 调 tools/call batch，list=/etc/hosts、out=/tmp/.../mcp-arbitrary-write.tsv，返回"总数 35"，并在任意路径成功写入 85 字节文件。也就是说任意能调用 MCP 的客户端可以读任意文件的行数、写任意路径文件（含覆盖）。

后果：若 MCP token 泄露、或本机浏览器被恶意页面利用（HTTP 侧不校验 Origin/Content-Type，见 P2-13），攻击者可在 root 权限下覆盖 /etc/cron.d、authorized_keys、systemd unit 等，直接拿下 hk 主机。这不是"工具参数不严谨"，是主机安全问题。

建议修法：
- batch 的 list/out 只允许 GUIJU_HOME 或显式配置的白名单目录；resolve 后检查前缀，拒绝 ..、symlink 与已存在目录外的路径。
- 更彻底：MCP 面只提供纯查询（query/verify/announcement），批量盘点回到 CLI；MCP 不承担文件系统能力。
- systemd 单元加 User=guiju、ProtectSystem=strict、ReadWritePaths 限制；Caddy 侧保持 Bearer 并加 IP/来源限制。
- HTTP 校验 Origin/Host 与 Content-Type: application/json；给 socket 设 timeout。
- 回归测试：list=/etc/passwd、out=/tmp/../etc/x、symlink 三种必须拒绝。

---

## 2. P2（20 条）

| 编号 | 位置 | 问题与后果 | 建议 |
|---|---|---|---|
| P2-1 | syncengine.py:43,75-79；autosync.py:150 | catalog_complete/synced_at 是全局单值，不按源命名空间化。sync --source shzjw 会把"上海源已完整"写成全局状态，autosync 随后判 fresh；mohurd 的缺口既不被发现，也会被别的源的完成标志授权 quiet-stop。 | meta 键加 source 维度（source:<id>:complete/synced_at/total）；autosync 按命令实际使用的源判断；catalog payload 输出 per-source 状态。 |
| P2-2 | autosync.py:164-177 | 刷新失败后的冷却写入（set_meta + commit）自身失败（DB 被锁/只读 HOME）时异常冲出 evaluate，CLI exit 1 internal。ADR 0003 明写"刷新失败不阻断命令"，被违反。 | 冷却写入用 contextlib.suppress 包住；无论写没写成功都返回 refresh_failed 快照。 |
| P2-3 | store/sqlite.py:45-54；cli/__init__.py:83-86 | Store 构造在 main 的通用 try 之外；损坏库/不可打开时抛 sqlite3.DatabaseError，裸 traceback、stdout 0 字节、exit 1，违反 --json 契约。已实测：printf garbage > catalog.db 后 status --json。 | 把 Store() 移入统一异常处理；DatabaseError 映射为 50 或 2，并输出 JSON；损坏库给修库/重建提示。 |
| P2-4 | mcp.py:304-339；serve_stdio:342-359 | initialize/tools/call 对非 dict params 直接 .get，AttributeError；stdio 循环无兜底，单个畸形请求打死整个服务。已实测：第二行 ping 无响应。 | handle 开头校验 params 类型并返回 -32602；serve_stdio 对每行 handle 加 try/except，返回 -32603 后继续。 |
| P2-5 | store/sqlite.py:313-322 | persist_codes 把所有非 primary 的 mentioned 一律改写成 superseded_ref，且非 primary edition 一律写 confidence=parsed。line 317 注释声称与 domain._demote_older 一致，但后者只在同族同号时才降级。无关引用（"参考 GB 50016-2014"）会被 show 说成被代替/废止。已实测：GB 55037 公告里的 GB 50016-2014 落库为 superseded_ref/parsed。 | 降级逻辑与 _demote_older 共用；confidence 按 mention 的真实证据写（decisive→parsed，其余 ambiguous）；补单测。 |
| P2-6 | store/sqlite.py 的 persist_codes | 重扫时只增不删：新解析结果比旧结果少时，旧 mention 留在库里，错误编号永久可见。 | 重扫先删该 ann 的 mention 再写，或在同一事务里做差集删除。 |
| P2-7 | store/sqlite.py:106-118 | upsert_announcement 是 insert-only（ON CONFLICT DO NOTHING），标题/分类/日期/fetched_at 永不更新；修好解析器也修不到存量 3256 条。 | 改为 DO UPDATE，保留 id 与首次抓取时间，记录 updated_at。 |
| P2-8 | store/sqlite.py find_by_code | 精确匹配失败后走 LIKE %key%，用户输入里的 %/_ 未转义；实测 resolve("GB%") 命中 GB50016-2014 并当作确定结果。可能把通配符查询变成一本真书。 | 转义 LIKE 通配符；或只在 key 形态合法时走 fallback；返回多 key 时仍判 ambiguous。 |
| P2-9 | store/sqlite.py:45-46 | 无 WAL、无 busy_timeout 重试、journal_mode=delete；CLI 与 MCP 并发写时 5s 后抛 OperationalError（非 GuijuError），exit 1"程序缺陷"。 | PRAGMA journal_mode=WAL、busy_timeout；把 sqlite3.OperationalError 映射为可重试的 20/50；写路径加重试。 |
| P2-10 | cli/commands/mcp.py:11-15 | run(store, args) 忽略传入的 store 与全局 --db；McpServer 懒加载 Store() 用默认 GUIJU_HOME。同一 --db 下 show 正常而 MCP 读默认库，announcement 返回 31。 | 把 store/path 注入 McpServer；或明确文档"mcp 不支持 --db"并在解析时拒绝。 |
| P2-11 | cli/commands/cite.py:88-92 | name/公告ID/URL 路径的 confidence 恒为 unknown，即使 code_key 来自 parsed primary；payload 自相矛盾（confidence unknown 但 code_key 有值）。 | target 的 confidence 从 primary mention 推导；unknown 只用于真正无证据的情况。 |
| P2-12 | mcp.py:88-89,153-169 | _as_error 只回 message，丢掉 exit/reason/hint；query/verify 不读 NativeCurrency.stale/from_cache，上游挂掉用 30 天旧缓存回答时 MCP 无告警。 | 错误结果返回完整契约字段；查询结果附 from_cache/stale/cache_age_days 与 source。 |
| P2-13 | mcp.py:368-431 | HTTP 不校验 Origin/Host/Content-Type，text/plain 的跨站 POST 可执行工具（本地 CSRF）；无 socket 读超时，半截 body 永久占线程；GET /mcp 返回 404 而非 405。 | 校验 Origin/Host 与 application/json；设置 self.timeout；GET 返回 405；限制线程数。 |
| P2-14 | compliance.py:307-322；140-173 | _get_once 对 403/429 直接归 20，没读响应体检测 BLOCK_MARKERS（实测 403"访问过于频繁"→exit 20，agent 会重试）；post_json 的 200 响应不做 block 检测。 | 4xx 先读有限 body 做 block 检测，命中即 40；post_json 复用 _detect_block。 |
| P2-15 | compliance.py:282-306 | 续传无 If-Range/ETag/Last-Modified；同 URL 换版后旧前段 + 新后段拼接，magic 正确但内容错乱，exit 0。 | 带 If-Range；校验 Content-Range 起点/总长；不匹配丢弃 .part 重下。 |
| P2-16 | domain/attachment.py:69-76 | _is_pdf 只看 URL 路径 .pdf 或标签 .pdf；真实 mohurd api-gateway 下载 URL 路径无 .pdf（probe 表 10 条附件 URL 中 9 条如此），对真实 PDF 恒 False，"唯一 PDF"消歧失效。 | 结合 Content-Type/扩展名/下载接口特征；或把 api-gateway 视为 PDF 候选。 |
| P2-17 | currency/httpx.py:52-69；currency/codes.py:23-25 | _check_url 精确匹配 path，正常 /s.jsp;jsessionid=... 被判 40（永久停止）；same_family 只取第一个横杠前的前缀，JTG/T 3360-01 与 -02 被判同族，可能混入兄弟子册。 | 规范化 path（去 ;jsessionid、多余斜杠）；same_family 用更完整的基号（含 -01/-02 段）；补边界测试。 |
| P2-18 | currency/ccsn.py:14-25；fallback.py:64-65 | ccsn 把"在编"→upcoming、未知状态+AbolishDate→superseded，把"编制中"说成"即将实施"、把"废止"说成"被替代"；fallback 最终错误类型只看主源，备源 NeedsHuman(50) 被降成 SourceUnreachable(20)。 | 状态照录源站或 unknown；错误类型取所有分支中最需要人处理的级别（50 优先）。 |
| P2-19 | domain/codes.py:37,209-215 | _PUBLISH_WORDS 不含"批准"；"现批准《X》为国家标准，编号为…。自…实施。"（句号分段）会被判 weak_primary/ambiguous，cite --strict 对明确公告硬失败。 | 加入"批准"；或对"批准+书名号+编号"给更强的句法规则；补真实排版测试。 |
| P2-20 | guiju/autosync.py:17；pyproject.toml | import fcntl 在 Windows 直接失败，CLI 不可用；pyproject 声明 OS Independent，CI 只跑 Linux/macOS。 | Windows 用 msvcrt.locking 或 portalocker 的零依赖等价实现；或明确声明仅 POSIX。 |

---

## 3. P3（清理、契约细节、文档）

死代码与冗余：
- Store.search() 已无调用方（search 走 catalog_entries+rank）；files.write_atomic() 无调用方；native._memo_ttl() 的 cache.ttl 分支在 memo 仅 cache is None 时永远走不到；attachment._VERSION_SUFFIX 在 _norm 先删除括号后永不匹配；CatalogItem 等旧模型残留。清理可降低误读成本。
- currency/__init__.CachedCurrency 的落库缓存在 native 分支提前 return（:146-153），GUIJU_CURRENCY=native 下 store 缓存永远不可达；M9 的"服务断了也能答"实际由 native 磁盘缓存承担，旧 sqlite currency_cache 表是死路径。要么删除，要么只在非 native 端点启用。

CLI/输出契约：
- guiju --json（无子命令）把中文 help 打到 stdout 且 exit 0；pull --source nope 抛 KeyError（exit 1 internal）而不是 2 bad_usage。两者都可用参数校验修掉。
- search/show 的人读结果走 render.note（stderr），--quiet 会把主结果一起吞掉；歧义候选的人类输出打印"候选：None"（候选对象没有 code/label/key 字段）。
- pull --list 不剥 BOM（U+FEFF 会让首个 ref 走错路由）；行尾注释只识别空白分隔的 #；批量汇总丢 candidates/availability，32/33 无法在批量结果里交人选择。
- which 先 cur.lookup（:49）再 resolve 内部第二次 lookup；第二次 TTL 命中会把 from_cache 覆盖成 true，agent 误以为服务不可达（M11 残留）。
- status 硬编码 schema_version: 2、age > 7；阈值与 schema 版本应来自常量/配置；status 缺 catalog_meta_corrupt 等诊断字段。
- 损坏/未知 schema 版本的文案说"超出本程序认知（2）"，没有前向迁移；未来 SCHEMA_VERSION=3 会锁死所有存量库；备份名只到秒，copy2 可能覆盖唯一旧备份。

MCP：
- schema 里的 cache 参数三个工具全部忽略；announcement 对 API 源返回词表外的 verdict=api_mode，且 shzjw 有下载链接却永远 attachment_url=null。
- _ReadOnlyStore 的 docstring 说"白名单比黑名单抗未来改动"，实现却是 _WRITES 黑名单；子类新增写方法或直接 ro.db.execute("INSERT") 都能落库。应用 sqlite mode=ro 或真正的代理白名单。
- announcement 会写 currency-cache/native-v1-*.json，且 Store() 构造会跑建表 DDL；"不落盘"的措辞与事实不符（虽不写公告库）。
- 未知 notification（无 id）会收到 -32601 错误包；JSON-RPC 规范要求通知不回包；未知名方法应 -32600 而非 -32601；HTTP 兜底不回显请求 id。GET /mcp 404 应为 405。

抓取/时效细节：
- _detect_block 只看前 8KB，>2MB 响应跳过，application/xhtml+xml 不在检查类型；compliance.get 无 body 上限。
- 限速只在进程内；两个 CLI 进程并行时仍可超 1.2s/host（AGENTS 已要求调用方不要并发，但 sync 与 autosync 的内部并发没有互斥）。
- csres 永远只查 pageNum=1、has_more=None（fixture 明示 pageSize=25，命中第 2 页起会丢）；jtst/stdsamr/dbba 的分页边界也未全部覆盖。
- ccsn 截断告警"第 {page}/{page} 页"两个占位符相同；HasNextPage 用 bool() 判断，字符串 "false" 会被当 True 多翻 4 页。
- HttpClient 没有 close()，适配器 close()/__exit__ 是空操作；当前无 socket 泄漏，但"已修"只是形式。
- csres 元数据走 http://www.csres.com/s.jsp（明文），同源结果决定现行性；建议只允许 https 或至少文档标注风险。compliance 也仍允许 http。
- legacy sqlite 缓存键只有 normalize_key(code)，不含 endpoint/route；退役的 cmd 端点理论上可重放旧记录（当前无实际路径）。
- Store._ensure_additions 与 schema.sql 重复定义 currency_cache 等 DDL，每次构造都执行 CREATE TABLE IF NOT EXISTS 并 commit；只读 HOME 下可能失败。
- Store.close() 静默回滚未提交事务；跨线程共享连接（cross_thread=True）内部无锁，注释称 threadsafety=1，实测 Python 3.13 为 3，但双线程无锁共写仍可 SystemError（B 组 600 写只落 302）。
- upsert_announcement 的 fetched_at 只在首次写；文档/监控若依赖它判断"最近抓取"会误判。

测试/工程/文档：
- 3 条 sqlite3 ResourceWarning 来自测试未关闭 Store/McpServer（tests/test_mcp.py、test_fix_med_core.py、test_fix_low_20261112.py），不是产品泄漏；但会掩盖真实泄漏，建议用 contextlib.closing/with。
- tests/test_fix_low_core.py 直接 os.environ["GUIJU_HOME"]=... 不恢复，完整套件跑完后该变量指向已删除的临时目录；后续测试顺序依赖。
- tests/test_guard.py 相对重定向用例 except Exception: pass 且零有效断言；未来 ValueError 回归会静默通过。
- scripts/ci.sh 在无 ruff 且 uv cache 为空时 uv tool run ruff 离线直接 exit 1，脚本在 1/4 步终止；"不依赖网络"的声明不成立。
- 覆盖缺口：compliance.get/_get_once/_detect_block 在 601 个测试中零执行（行覆盖 57.4%）；sources/mohurd.py 的目录/详情/附件解析覆盖 43.9%，attachments() 的 HTML→Attachment 无 fixture。站点改版目前只能靠人工 e2e 发现。
- scripts/e2e.py 硬编码 total_in_db == 3254 与 total >= 40；上游公告正常增减就让发版门误红。建议改 total_in_db == total_reported + baseline fixture。
- .github/workflows/e2e-live.yml 仍配置 GUIJU_CURRENCY_URL，但该变量已随 spec-currency 退役删除；配置无作用，误导维护者。
- 文档漂移：README 写 101 项单测（实际 601）、36 项 e2e（另一处 38）；ACCEPTANCE 仍称 v1.0.0/446；INSTALL 指向 v1.0.0；README/ARCHITECTURE 的退出码表缺 34；ARCHITECTURE 描述的 application/ports/migrations 目录并不存在。
- pyproject 声明 OS Independent，但 fcntl 使 Windows CLI 直接不可用；CI 只在 Linux/macOS 跑。

---

## 4. 上轮评审（2026-11-12）残留核对

| 上轮项 | 本轮结论 |
|---|---|
| H1 quiet-stop | 已修闸门，但"完成信号"未校验 total：空页/页大小仍可提前判完成并置 complete（P1-1 残留）。 |
| H2 MCP 异常兜底 | _call/do_POST 已兜底；stdio 的 handle 仍无兜底，非 dict params 打死服务（P2-4 残留）。 |
| H4 限速锁 | compliance 已加锁；currency/httpx.py 同构缺陷未修（P1-5 另一模块残留）。 |
| M4 pull 按源解析 | 主下载路径仍用 args.source 选适配器，不按公告 URL 归属源（P2 级，跨源公告会走错协议）。 |
| M6 cite note | 未修：note 被塞进 current_hint，strict 仍静默（P1-6）。 |
| M9 落库缓存 | native 分支提前 return，store 缓存不可达（P3 死路径）。 |
| M10 find_by_code 扇出 | Store.search 已成死代码，但 find_by_code 的 LIKE 通配仍会猜码（P2-8）。 |
| M11 两次 lookup | 仍在（which :49 + resolve 内部；P3）。 |
| M12 MCP 降级告警 | MCP query/verify 仍不暴露 stale/from_cache（P2-12）。 |
| M18 分页 | csres 仍只查第 1 页（P3）；其余适配器分页有改善但边界未测试。 |

这说明上一轮修复方向是对的，但"修在一个模块、漏在另一个模块"和"修了症状、没修判定条件"是本轮最值得警惕的模式。

---

## 5. 架构建议

1. 把 application 层真正落位。cli/lookup.py 613 行承担了解析、源归属、版本定版、时效查询、持久化，实际是 application 层；docs/ARCHITECTURE.md 却描述了一个不存在的 application/ports/migrations 结构。建议拆成 application/resolve.py、application/currency.py、application/attachment.py，CLI 只做参数解析与渲染；端口（Store/Currency/AnnouncementSource）显式注入，便于单测。
2. 合并两套出站网络栈。compliance.py 与 currency/httpx.py 各自实现白名单、重定向、节流、大小限制、block 检测，已经漂移（H4 只修一处、P1-5 残留）。建议抽 guiju/net/：一个 HttpClient 端口 + 策略对象（host allowlist、per-host 令牌桶、redirect 规则、body 上限、block markers、resume 校验），compliance 与 currency 都走它。这样"唯一闸门"才是事实，不是文档。
3. 目录状态按源命名空间化，并把完成判定写成可审计的状态机。meta 表用 source:<id>:complete/synced_at/total；crawl 返回 complete_reason（seen>=total / empty-with-total / truncated / server-short-page）；autosync 按源判断。这样多源、增量、quiet-stop 三者才不互相污染。
4. 空结果类型化。定义 LookupResult(records, authoritative_empty, parse_failed, source)；适配器契约明确"缺字段=失败"、"成功空=authoritative_empty"，缓存层只缓存后者。把这一条写进 domain 层文档和 contract test，避免每个适配器各自解释。
5. 公告名称模型改为多值。Announcement.names: list[str]（标题里全部书名号），doc_name 仅作展示；pull/cite 用用户 ref 在所有名称中匹配，无法唯一对应就 33。这样多标准合订公告不会再抓错。
6. MCP 面收紧为"只读查询 + 无文件系统副作用"。batch 的文件读写回 CLI；MCP 只保留 query/verify/announcement/纯内存 batch（或显式白名单目录）。HTTP 加 Origin/Host/Content-Type 校验、socket timeout、线程上限；systemd 降权。JSON-RPC 的错误码与通知语义按规范对齐。
7. 契约与文档同源。退出码表、JSON schema、catalog 字段从代码常量生成文档（或至少 CI 校验文档包含全部 reason）；e2e 断言退出码表；README/ACCEPTANCE 的版本与测试数由脚本更新。避免"文档说 34 不存在，代码已经返回 34"。
8. 测试门禁补 12 条高价值回归（列在第 6 节）。这些用例的共同点是：它们守护的是"产品承诺"（不抓错、40 停止、否定结论可信），而不是函数行覆盖。

---

## 6. 建议修复顺序

第一批（数据正确性 + 合规 + 安全，建议本周）：
1. P1-1 同步完成判定（seen >= best_total；空页不置 complete）。
2. P1-2 下载长度/Content-Range 校验。
3. P1-3 多标准公告按查询名称选附件。
4. P1-9 多 target 分族/歧义判定。
5. P1-8 批量 40 立即停止。
6. P1-10 MCP batch 路径限制 + systemd 降权 + HTTP Origin/超时。

第二批（语义与契约）：
7. P1-4 空结果/解析失败分离 + 禁止负缓存解析失败。
8. P1-5 currency 限速锁。
9. P1-6/P1-7 cite strict 与 34 语义。
10. P2-1 多源水位、P2-2 autosync、P2-3 损坏库、P2-4 stdio 兜底。

第三批（MCP 与时效源细节）：
11. P2-10 至 P2-18；P2-12/13 MCP 结果面与 HTTP；P2-14/15/16 抓取完整性；P2-17/18 时效身份与状态。

第四批（清理与门禁）：
12. P3 死代码、文档漂移、测试 ResourceWarning、e2e 硬编码、CI 离线 ruff。

回归测试清单（建议新增）：
- test_sync_empty_page_is_not_complete
- test_sync_server_short_page_respects_total
- test_download_truncated_content_length_raises
- test_download_resume_rejects_changed_etag
- test_pull_multi_name_announcement_picks_requested_name
- test_cite_strict_fails_when_currency_unreachable
- test_cite_survives_no_currency_record_with_warning
- test_batch_list_stops_after_compliance_denied
- test_mcp_non_dict_params_does_not_kill_stdio
- test_mcp_batch_rejects_paths_outside_home
- test_classify_two_revision_targets_is_ambiguous
- test_currency_missing_keys_raises_needs_human
- test_currency_throttle_serializes_threads

---

## 7. 复现与证据

Lead 本轮新增脚本（/tmp/guiju-review-20261118/）：
- lead-verify-store.py：空页完成、resolve("GB%") 通配。
- lead-role.py：无关 mention 被落库成 superseded_ref/parsed。
- lead-truncated-download.py：声明 10009 实收 509，download 无异常。
- lead-multi-att.py：多标准公告按第二名称检索、pick 第一本附件。
- lead-cite34.py：NoCurrencyRecord 下 cite 抛 34。
- lead-batch40.py：ComplianceDenied 后仍调用 B/C。
- lead-throttle4.py：两线程读-读交错，currency _throttle 零 sleep。
- lead-mcp-write.py：MCP batch 任意路径写入。
- lead-verify-codes.py：多 target 选错、批准分段弱证据、三源缺 key 返回 []。
- lead-negcache2.py：空结果首次 put、二次 from_cache=True。
- lead-evidence.py：真实库 7 条多名称公告、probe 中 api-gateway 无 .pdf 比例。

子评审报告（/tmp/guiju-review-20261118/）：
- A-network-files.md（网络/下载/附件，P1 4 条）
- B-store-sync.md（存储/同步，P1 2 条）
- C-cli.md（CLI，P1 3 条）
- D-currency.md（时效层，P1 3 条）
- E-domain-mcp.md（领域/MCP，P1 4 条）
- F-tests-quality.md（测试/工程，P1 1 条）

说明：子评审的 P1 与 Lead 的 P1 有重叠，本报告已合并去重；个别子评审的 P1（如 stdio 崩溃）在本报告按影响重新分级。所有"已实测"均指本地离线复现，未对真实政府站点发起额外请求。

---

## 8. 复核后认为做得好的部分（不要改坏）

- 退出码即决策表的整体设计，以及 30/32/33 交人、40 停止的价值观，是这套工具最有价值的部分；本轮问题多是"某个分支没守住"，不是方向错。
- 正文出站白名单、跨源重定向拒绝、307/308 保留方法、.partial 续传、文件名 sha256 指纹幂等、原子写，整体是正确的。
- 时效缓存 TTL 24h / 陈旧上限 30d、时钟回拨防护、取消检查、admit 计数、registry 最长前缀路由、shzjw 严格身份与四栏映射，代码质量较高，未发现越权或注入。
- 附件选择在"多候选/废止/英文版/唯一 PDF"上的裁决逻辑（33 交人）方向正确，问题只在多名称公告的输入不完整。
- 601 个单测、1117 个子测试、两版本 Python 兼容、零第三方依赖、ruff 通过，是很好的底盘；本轮所有 P1 都能用 1-3 条针对性回归锁住。

---

## 附：评审分工与边界

- Lead：核心链路自读（compliance/syncengine/mcp/store/currency/native/cli 关键路径），并对 P1-1/2/3/4/5/6/7/8/9/10 逐条独立复现；负责最终合并、分级与取舍。
- reviewer-net：compliance、sources、files、attachment、naming、availability。
- reviewer-store：store、syncengine、autosync、localstatus、schema。
- reviewer-cli：cli/__init__、lookup、render、commands 全部。
- reviewer-currency：currency/ 全部 14 个文件。
- reviewer-domain-mcp：domain/codes、domain/shjzw、mcp、cite。
- reviewer-tests：tests、scripts/ci.sh、e2e、workflows、文档一致性。

约束：全程只读产品代码；未联网抓取；未改 NAS；未提交 Git；报告本身是新增文件，未 commit。
