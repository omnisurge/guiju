# guiju 代码评审报告 — 2026-11-12

> 范围：`guiju/` 包全量（约 52 个 Python 文件，~5,500 行）。
> 方法：三人分工通读（cli 层 / currency 层 / domain·sources·store·compliance·mcp·引擎层），Lead 交叉复核关键证据；mcp.py 崩溃路径已实际复现。
> 基线：`python3 -m pytest tests -x -q` → 466 passed（全绿）。
> 状态：**仅评审记录，未改任何代码**。修复优先级见文末。

---

## High（建议优先修）

> ✅ 全部 5 条已于 2026-11-12 修复（工作区未提交）。pytest 466 → 488 passed，无回归。
> H1 syncengine.py（catalog_complete flag + 截断不写 synced_at）；
> H2 mcp.py（_call/do_POST 异常兜底 → -32603/isError）；
> H3 lookup.py（3 处 catch-all 前补 NeedsHuman/ComplianceDenied/QueryCancelled 上抛）；
> H4 compliance.py（_throttle 加锁 + 重定向目标 host 节流）；
> H5 currency/__init__.py（_idna_url + DEFAULT_MCP_URL 改 punycode）。
> 新增测试：tests/test_fix_20261112*.py 共 22 个用例。
> 已知影响：存量库升级后首次 sync 需全量爬底一次（≈50s）；throttle 锁为全局保守串行；crawl payload 新增 complete/truncated 两键。



### H1. 增量同步有永久性缺口风险 — syncengine.py:57-64
`quiet_pages >= 2` 即停，且不管是否爬到底都写 `synced_at`/`catalog_total`。
首轮若中途失败，重跑时前两页全是旧条目 → 连续 2 页零新增即停，缺口之后永远爬不到；
autosync 认为目录 fresh，7 天内不再触发，缺口被无限期掩盖。
**修法**：未到 `total` 时 quiet-stop 不生效；或截断时不刷新 `synced_at`。

### H2. MCP 服务无异常兜底，一异常崩整个服务 — mcp.py:215-253, 311-317
`handle()`/`_call`/`do_POST` 只 catch `GuijuError`。
后端抛任意其他异常（已实测 `RuntimeError`，如 sqlite3 OperationalError，ThreadingHTTPServer 并发下极易出现）：
- stdio 模式 → serve 循环整体死掉，服务崩溃；
- HTTP 模式 → handler 线程断连，客户端收不到 JSON-RPC 错误体。

**修法**：`_call` 外层包 `except Exception → {code:-32603}`；`do_POST` 加兜底。

### H3. except Exception 系统性吞错，把 exit-50/40/取消伪装成 32 — lookup.py:200, 229-230, 417-418
- L200 / L417：详情页抓取失败 `continue` —— `mohurd.py:88` 站点改版会抛 `NeedsHuman`（应交人 50），被静默吞掉；
- L229-230：时效查询任意异常 `return None` → "定不出版本" → AmbiguousTarget(32)。

`NeedsHuman` / `ComplianceDenied`（必须 40 立即停止）/ `QueryCancelled` 全部越权吞掉，
agent 按"歧义交人"处理白耗一轮。
**修法**：catch-all 前加 `except (NeedsHuman, ComplianceDenied, QueryCancelled): raise`。

### H4. 限速器无锁 + 重定向逃逸 — compliance.py:87-95, 70-78
- `_throttle` 读-睡-写无锁：MCP `--http` 的 ThreadingHTTPServer 并发下两请求可同时通过 1.2s 节流 → 对政府站并发轰炸可能触发人机校验/封禁自伤（影响 hk 线上 guiju-mcp.service）；
- `_GuardedRedirect` 只对每跳 `check_url`，不重写 `_last_call`：跨 host 重定向后新 host 未计节拍。

**修法**：`_throttle` 加 `threading.Lock`；重定向前对新 host 同样节流。

### H5. 默认时效 URL 是中文 IDN，urllib 必然握手前崩溃 — currency/__init__.py:39
`DEFAULT_MCP_URL = "https://guiju.守夜人.中国/mcp"`：host 未 punycode，
`http.client.putheader("Host", host)` latin-1 编码抛 `UnicodeEncodeError`
→ `GUIJU_CURRENCY=mcp`（显式机制与缺省 fallback）死路一条。
**修法**：URL 解析时 `host.encode("idna")`，或默认值改 punycode/ASCII 域名。

---

## Medium

> ✅ 大部分已于 2026-11-12 修复（第二轮，工作区未提交）。pytest 488 → 543 passed。
> 已修：M1/M3/M4/M5/M6/M7/M8/M9/M11/M12/M13/M14/M18/M19/M20/M21/M22 + 遗留 SourceUnreachable→20。
> 跳过：M2(fallback 溯源-评估后属设计取舍)、M16/M18 重定位后按归属/冻结授权跳过、M15(目录增补属产品决策)、M17(exit-code 语义待所有者裁决)。
> 裁决项：test_currency_ccsn.py:89 「局修完成+abolish→superseded」钉住用例编码旧错误行为，已改为 unknown。
> 新文件：tests/test_fix_med_20261112.py / test_fix_med_core.py / test_fix_med_currency.py。
> 待人工/跨域：sync.py:18/27 count_announcements 双查；非上海地方源接入需改 localstatus PREFIXES；ccsn 多页翻页契约未确认；兄弟适配器 csres/stdsamr/dbba 同类问题未修；AGENTS.md §4 角色表需补 weak_primary 一行。



| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| M1 | native.py:122-129,153-155 | 缓存读写只兜 OSError；backend 抛 sqlite3.Error/ValueError 打死查询并禁用 stale 降级 | 兜底 (OSError, ValueError, sqlite3.Error) 或除不可恢复类外全兜 |
| M2 | model.py:as_payload + __init__.py:344,363 | 落库只存 5 字段，fallback/replaces 丢失 → 备源结果落库后伪装成主源 | as_payload 补 fallback/replaces，_load 回填 |
| M3 | mcp.py:_announcement 156-213 | docstring 承诺"不下载、不落盘"，实际 resolve(allow_network=True) 抓详情页并 persist_codes 写库；并发写 sqlite 无 busy 重试 | resolve 传 allow_network=False 或加 read_only；或更新文档；写加重试 |
| M4 | lookup.py:197,407 + cite.py:53 | 三处硬编码 registry()["mohurd"] 抓详情页；shzjw 公告被用错解析器 | 按 lookup.source_of(url) 选源；api_mode 源跳过 detail |
| M5 | codes.py:203-206, 259-261 | "发布/施行"句内编号即升 PRIMARY 过强；多 PRIMARY 静默取最大年仍标 PARSED | 按 code_stem 分族，异族交人；提升要求旧版佐证 |
| M6 | cite.py:121-134 | SourceUnreachable 提示写进未展示的 current_hint 槽位，用户看不到"时效服务不可达" | 并入 warnings 或独立字段 |
| M7 | __init__.py:166-187 | tools/call 不查 isError:true（工具级错误被当空结果→误报 31）；漏 notifications/initialized 与协议头 | 检查 result.isError；补握手与头 |
| M8 | sync.py:18-20 | --if-stale 0 行为与 help "0=只在库为空时同步"不符 | 改 help 或补空库判断 |
| M9 | lookup.py:74,183 | CachedCurrency() 不传 store → pull 路径磁盘缓存死代码，"服务断了用本地缓存"不生效 | 传 store 或文档说明 |
| M10 | sqlite.py search/find_by_code | per-mention 扇出再 LIMIT 计数不符；LIKE %key% 前缀过匹配可能静默返回错码 | 调整扇出与匹配边界 |
| M11 | which.py:24,39-44 | 同一编号上游查两次（cur.lookup + resolve 内部再查）；TTL 未命中 → 两次 HTTP，故障期延迟×2；第二次覆盖 last_outcome，currency_from_cache/age 反映的是错误的那次 | resolve 复用已取 records，或 NativeCurrency 进程级 memo |
| M12 | which.py:76 + native.py:163-164 | native 降级告警双重吞掉：stderr 只对 != "native" 告警；currency_from_cache=true 混淆"TTL 正常命中"与"上游失败用 ≤30 天旧缓存"，upstream_error 丢失 | JSON 加 currency_stale/currency_upstream_error；告警按 stale 判 |
| M13 | lookup.py:191,236 | 命中缓存行时 has_attachment=None，with_file 过滤把缓存命中有效结果静默剔除 | 缓存行补 True 或过滤区分 None/False |
| M14 | cite.py:135 + lookup.py:427 | 编号比较用展示串/半成品归一化（JGJ/T450-2018 vs JGJ/T 450-2018 误报不一致） | 统一 normalize_key 后比 |
| M15 | pull.py | _wrap confidence 与落库口径不一致（payload 报 unknown、库存 parsed）；--list 的 split("#") 截断含 fragment 的 URL；_batch 非 GuijuError 中断整批且无汇总 | 解析成功时用 codes.confidence 覆盖；只跳行首 # 注释；批内兜底 |
| M16 | registry.py:42-58 | "DB31-xxx"（缺 /T）归一化后既不在上海前缀也不在 DB 黑名单 → NotIndexed(31) 而非 needs_human | 边界补判 |
| M17 | native.py:136 | NoCurrencySource(NotIndexed→exit31) 语义不当（路由未覆盖≠没收录），hint 仍指向旧设计 | 独立错误类或改 hint/映射 |
| M18 | ccsn.py 等 | 各 HTTP 时效源只取第 1 页（size≈20-100），多版本规范可能丢版本 | 翻页至空页或 total>pageSize 记 warning |
| M19 | ccsn.py SEARCH_URL | ".../StandardQuery.assx" 疑似 .ashx 笔误（未联网验证） | 人工/CI 冒烟核一次 |
| M20 | ccsn.py status_ccsn | "局修完成"直接判 superseded，但 docstring 明说不足以判断 → 猜了权威结论 | 无法判定时回 unknown |
| M21 | store/currency.py vs native.py | 空 results 也写缓存（负缓存）与 docstring"不伪装负缓存"有张力 | 注释澄清语义 |
| M22 | native.py:87-93 | _guard 中 admit(code) 每次守卫重放（一次 query 2-4 次）；接真实配额门禁会翻倍计数 | admit 只入口调一次 |

---

## Low / 优化

> ✅ 大部分已于 2026-11-12 修复（第三轮，工作区未提交时验证）。pytest 543 → 619 passed / 1118 subtests。
> 已修：_is_pdf 子串误判、_CODE_RE \d{1,6}、search --limit<0→exit2、NeedsHuman.hint 泛化、
>       localstatus 收紧上海前缀、_CATEGORY_RULES 收口 domain.announcement、untitled-ann<id> 命名、
>       autosync notify 锁内重读、post_json 非JSON→20、416 HEAD 比对、lookup 三小修（dedupe/日期/name层级）、
>       mcp Content-Length 上限+非回环告警+source_id→NeedsHuman+共享Store(cross_thread+锁)、
>       sqlite catalog_meta_corrupt、sync 单查、AGENTS.md 补 weak_primary、
>       registry DB31 裸编号→shzjw 路由、CACHE_OFF 全后端生效+显式参数优先、memo 五元组重放、
>       _load 时钟回拨钳零/不可信、jtst+csres+stdsamr+dbba 分页/告警/close、fallback admit 拆分。
> 钉住不改（验证后为正确行为/有意取舍）：native admit 2次/上游查询（入口+put前复验）、
>       memo/cache 键用原样 code（normalize_key 只在 sqlite 层）、prune_expired（幻影项，不存在）、
>       MisconfiguredCurrency→SourceUnreachable（非 NeedsHuman）。
> 第四轮收尾（2026-11-12）：M17 已裁决——新增 exit 34 no_currency_record（NoCurrencyRecord/NoCurrencySource 归并）；
>       _FORM_HOSTS 已注册表驱动（Route.form_post + 端点级 Route，httpx 侧 lazy 查询）。pytest 627 passed/1128 subtests。
> 未动（设计取舍待 owner）：fallback 主源空不查备源（需对 Go spec-currency 语义，见 owner 项）。
> 已澄清：spec-currency 是 guiju 的时效上游（which/cite 数据源），未被淘汰、不可删。
> 已知取舍：旧库裸 untitled__sha.pdf 不可幂等命中会重下一次；shzjw rec 无 id → 裸 untitled 保留。



- localstatus.py:15-20 — 裸 'DGJ'/'DBJ' 前缀把其他省市地标（DBJ50 等）误判为上海 → 多发一次错误外呼
- attachment.py:60-70 — _is_pdf 子串判断，"PDF版.zip" 混入候选
- compliance.py:267-269 — 断点续传 416 直接视为完成；远端变小/.part 损坏时会以坏文件命名成功（建议 416 时 HEAD 比对 Content-Length）
- compliance.py:147 — post_json 对 WAF HTML 错误页抛裸 ValueError → exit 1 而非 20
- codes.py:19-22 — _CODE_RE 序号 \d{2,6} 漏 1 位序号老规范（CJJ 1-90）
- domain/announcement.py 与 sources/mohurd.py — _CATEGORY_RULES 两处定义，新增类别会漂移
- naming.py — code 与 name 都空时 stem=untitled 文件名互撞，find_cached glob 会串号（fallback 带 ann_id）
- search.py:19 — --limit 负数反向切片无校验
- errors.py:114 — NeedsHuman.hint 只点名 mohurd.py，shzjw 源同样会抛
- autosync.py — notify 文案用抢锁前快照；refreshing 与 auto_sync_failed_at 语义混用
- sqlite.py:243-245 — catalog_age_days 解析失败一律当无 meta，抹掉"库脏"信号
- currency/__init__.py — _cache memo：key 不含 endpoint、无 TTL、from_cache 标志粘滞；GUIJU_CURRENCY_CACHE_OFF 只对 CommandCurrency 生效；native:foo arg 语义与 cmd:/mcp: 不一致
- currency/__init__.py:380-382 — _load fetched_at 用本地时钟，时钟回拨/时区会得负 age
- lookup.py:475-483 — _dedupe 首个空 code_key 保留其余丢；_filter_by_version_window 缺日期按 9999-99-99 排序被滤；_name_matches 宽松子串可能误并
- mcp.py:do_POST — Content-Length 无上限 rfile.read；--host 非回环时建议打印安全提示
- mcp.py:173 — registry().get(source_id) or registry()["mohurd"] 对未知源静默回落，应显式报错
- native.py:_guard 顺序 — 先 admit 后 cancelled，统计上多记一条未查
- MCP 优化 — 每请求新建 Store() 跑 DDL + 锁竞争，建议共享只读连接；_announcement 加 read_only 路径
- jtst.py — 交通部源单页无翻页；_TreeParser 对未闭合标签隐式推断，改版易静默解析空（建议空结果记 warning）
- fallback.py — 主源空结果不查备源（设计如此，但与 Go 版语义需 owner 确认一致）
- httpx.py — _FORM_HOSTS 白名单硬编码两家，加源需改代码（设计约束）
- fallback.py 待办 — _guard 拆 admit/cancelled；CurrencyCache 契约 memo 键统一走 normalize

---

## 已确认无问题（故意设计，勿改）

- shzjw 严格后缀匹配给 NeedsHuman（AGENTS 明示严码）
- fallback 主源空结果不查备源（docstring 声明；与 Go 版行为一致性留 owner 判定）
- parse_tsv 跳过 # 行（契约允许）
- attachment 单活件名称不符仍取（注释明写）
- files.py sha256 幂等/.partial 断点续传（仅 416 信任有缺口，见 Low）
- 合规白名单/5 跳/人机校验即放弃
- codes.py doc_name 含编号即 PRIMARY、_code_stem 前缀族
- shzjw HMAC token、isAbolish=False、cite strict+offline 互斥
- accepts_record 只认 current/withdrawn（源站分栏只产这两种）

---

## 修复优先级建议

1. **H1 syncengine** — 数据正确性，缺口永久掩盖
2. **H2 mcp.py 兜底** — 已上线服务稳定性
3. **H3 lookup 吞异常** — 契约正确性（50/40 伪装 32）
4. **H4 throttle 锁** — 合规红线，hk 线上风险
5. **H5 IDN URL** — 默认机制死路
6. M11/M12 which 双查询与 stale 信号丢失、M9 pull 路径缓存死代码、M3 _announcement 契约、M2 fallback 溯源

修复时建议每修一条跑一次 `pytest tests -x -q` 回归。
