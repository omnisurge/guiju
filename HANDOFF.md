# 规矩 guiju — 交接文档

> 一句话：从官方站点取工程建设标准的**目录 + 正文 + 引用串 + 现行状态**，只搬运与校验，不生产内容。
> 状态：**v0.14.4**（2026-10-31）。单测 135 项、联网验收 42/42、本地 CI 全绿，仓库干净且已推送。
> ADR 0001（index / 时效端口化）**已 100% 收尾**；ADR 0002（合并 spec-currency）**期 2 已完成、期 1 待开工**。

## 文档地图（各司其职，不要互相抄）

| 文件 | 唯一职责 | 谁读 |
|---|---|---|
| `README.md` | 用户手册：安装、命令、退出码、合规与不做的事 | 人 |
| `AGENTS.md` | agent 操作契约：命令选择、退出码动作、必知事实 | agent |
| `docs/sources.md` | 四个数据源的现状与边界（含"为什么某些源不接"）+ 多源仲裁实测结论 | 维护者 |
| `docs/ARCHITECTURE.md` | 架构决策：分层、数据模型、里程碑 | 维护者 |
| `docs/adr/0001-currency-and-scope.md` | 废止 index、时效端口化（**已关闭**，含「后续变化」） | 维护者 |
| `docs/adr/0002-merge-into-one-product.md` | **下一步**：合并 spec-currency、`guiju mcp`、部署形态 | 接手人 |
| `docs/adr/0003-catalog-self-refresh.md` | 目录自保鲜（无 cron/launchd）与 `catalog` 反馈契约 | 维护者 |
| `docs/ACCEPTANCE.md` | 验收记录（可复跑 `scripts/e2e.py`） | 所有人 |
| `CHANGELOG.md` | 版本变更（0.9.0 → 0.14.4 的每一步都在里面） | 所有人 |
| 本文件 | 交接状态与"已验证/别重犯"的事实清单 | 接手人 |

## 一、现在怎么跑

```bash
cd ~/Development/guiju
python3 scripts/ci.sh            # 本地 CI（ruff → 单测 → 离线 e2e → 入口自检），pre-push 同一套门
python3 scripts/e2e.py           # 联网验收（42 项，约 3 分钟；权威执行只在本机）
uv tool install --editable .     # 已装：~/.local/bin/guiju 与 gj，任意目录可用

guiju status --json              # 库状态 / 时效适配器 / 实际端点 / 目录新旧
guiju which GB55037-2022 --json  # 编号 → 名称 + 现行状态 + 本地公告
guiju pull 绿色建筑评价标准 --json # 同名多版本会按时效自动定版（取现行的最新公告）
guiju pull --source shzjw DG/TJ08-2007 --json   # 上海地标（地方标准）
guiju cite GB55037-2022 5.2.1    # → 《建筑防火通用规范》GB 55037-2022 第5.2.1条
```

- 零第三方依赖（Python ≥3.10 标准库）；数据默认 `~/.guiju/`（`catalog.db` + `library/`），`GUIJU_HOME` / `--db` 可覆盖。
- 受限沙箱里 `~/.guiju` 可能被拦 → `export GUIJU_HOME=/tmp/guiju`。
- 受限环境跑测试时关掉自动保鲜：`GUIJU_NO_AUTO_SYNC=1`（否则会真去联网抓目录）。

## 二、这一轮做了什么（版本即内容）

| 版本 | 内容 |
|---|---|
| 0.10.0 / 0.11.0 | **上海地标源** `shzjw`：API 模式（无公告页），检索 + 断点续传下载；站点分「现行/废止 × 标准/设计标准」四栏，**状态照抄源站**；`which`/`cite` 在时效服务无记录时回落到源站 |
| 0.12.0 | 时效查询**本地落库**（`currency_cache`，只存元数据）；修回 `sync` 漏写的 `set_defaults(func=run)`（离线 CI 抓不到，联网验收抓到了） |
| 0.12.1 | 新增 `docs/sources.md`；修 `cite --strict --offline` 静默降级（→ exit 2）；清掉所有推荐 `guiju index` 的文案 |
| 0.13.0 | **删除 `index` 命令**（ADR 0001 收尾），守卫测试禁止回潮 |
| 0.13.1 | 多源仲裁**先探查**：实测无跨源同编号冲突；候选补 `source`/`url` |
| 0.14.0 | **同名多版本按时效自动定版**（版本迭代不是歧义）；结果带 `versions` |
| 0.14.1 / 0.14.2 | 时效适配器显式配置：`cmd` 缺二进制如实报错（不再静默退 mcp）、`GUIJU_CURRENCY_URL` 不再被丢弃；新增 `currency_endpoint` |
| 0.14.3 | `cmd` 适配器**本机实测**（build `spec-currency`，与 mcp 逐字段一致） |
| 0.14.4 | 部署形态定案：合并后落**香港服务器**；澄清 NAS"冻结"是变更冻结、不是故障 |

## 三、已验证的事实（别重复试，也别推翻）

### 住建部侧（一期踩出来的）

1. **诚实 UA 完全可用**：`guiju/<版本> (compliance-aware; official code fetcher)`。**不要伪装浏览器**。
2. **目录全量 = 3254 条 / 33 页**（`pageSize=100`）；单页渲染上限 99 条**不丢数据**。
3. **列表接口**：`queryData` 必须正好六项，多传 `editType` 返回空；`pageNo` 要放进 `paramJson`。
4. **条目日期在链接之后**（`<span class="date-info">`），不是 `<li>` 属性。
5. **UA 必须纯 ASCII**（HTTP 头 latin-1）。
6. **附件陷阱**：早期公告第一个附件常是「废止的强制性条文」(.doc)；英文版不是官方中文正文。**禁止恢复"取第一个"**。
7. **新旧双编号是常态**（`JGJ147-2016`/`2004`）；角色判定见 `domain/codes.py`。
8. **正文可得性分档**：2014 及以后基本有附件，2000–2013 多为只有名称与编号（`domain/availability.py`）。
9. **旧链接需映射**（`zhengcefilelib` → `art_17339_*`）。
10. **`mohurd.gov.cn/robots.txt` → 404**（无限制）。

### 上海地标侧（0.10–0.11 实测）

11. **上海有真下载接口**：`POST /JGBStandardWebApi/external/localStandard/list` 检索，`GET .../api/files/webDownload` 出 PDF（`Content-Disposition: attachment`，实测 2,084,644 B）。令牌是 `HMAC_SHA256("<fileId>:<ts>", "jkysh2jgbstandard")`，密钥写在公开前端 bundle 里。
12. **四栏是四个独立查询**：现行标准 548 / 废止标准 334 / 现行设计标准 37 / 废止设计标准 23；同一编号两栏都有时**保留现行**。
13. **编号原样采用**（`DG/TJ08-2007-2026` 含年版）；通用解析器会把 `-2026` 误当成年份并判歧义。
14. **地标不需要外部时效服务**：`which`/`cite` 在服务无记录时回落到源站（`currency_source: shzjw`）。

### 其他源（评估过，别再去试）

15. **`std.samr.gov.cn` 只能按名称查**：`searchText=55037` / `GB 55037` 全 0 条，`stdCode` 参数被忽略；字段里没有任何文件链接。
16. **`openstd.samr.gov.cn` 对普通 GET 一律 0 条结果**（标题就写着"全文阅读"，在线阅读不算下载）。
17. **多源仲裁暂无战场**：两源标准体系互斥（上海 942 条全是地方标准前缀；住建部 3254 条公告无备案/地方标准字样）。真正存在的是"同名不同编号"（19 条完全同名）——版本迭代走自动定版，跨体系同名仍 `exit 32`。

### 时效与配置（0.12–0.14）

18. **`GUIJU_CURRENCY` 显式取值要么照办要么报错**：`cmd` 无二进制 → `cmd(缺失)` + exit 20；未知值 → `xyz(未知)`；`mcp` 尊重 `GUIJU_CURRENCY_URL`（内联 URL 优先）。
19. **`currency_endpoint`** 在 `status`/`which` 里告诉你实际用了哪个端点——配置没生效会在这里露出来。
20. **缓存兜底是明示的**：`currency_from_cache=true` + `currency_cache_age_days`，stderr 也会提示。
21. **`cmd` 适配器已实测**：`spec-currency` 源码在 `~/Development/spec-currency`（Go 1.26），`go build -o /tmp/spec-currency ./cmd/spec-currency` 即可；结果与 mcp 逐字段一致。**按所有者决定不装 PATH**。

## 四、下一步（ADR 0002）

**期 2 已完成**（上海地标内容源）。剩下三期，**期 1 是唯一高风险期**：

| 里程碑 | 内容 | 门禁 |
|---|---|---|
| **M1** | `guiju/currency/` 骨架 + httpx（超时/退避/UA）+ **csres**（GBK HTML 表格解析） | `csres_gb50011.gbk` golden 离线比对通过 |
| **M2** | 前缀路由注册表 + fallback 链 + TTL 缓存 + `ccsn`/`stdsamr`/`jtst`/`dbba` | 8 份 golden 全过；离线可跑 |
| **M3** | `DG/TJ` 复合源（shzjw 权威 + csres 校验） | 两源状态一致 |
| **M4** | 与 NAS mcp **逐字段 diff**（30+ 编号，覆盖 GB/JGJ/CJJ/JTG/DB/DG/TJ） | 不一致即阻断；通过后默认走 native，`cmd`/`mcp` 降为调试通道 |

Go 侧路由链（照抄即可，别自己发明）：

```
GB / JGJ / CJJ  → Fallback( csres → ccsn → stdsamr )
JTG             → Fallback( csres → jtst )
DB*             → Fallback( csres → dbba )
DG/TJ           → Composite( shzjw 权威 ←→ csres 交叉校验 )
```

- `csres` 是主源：`GET http://www.csres.com/s.jsp?keyword=<号>`，**GBK 编码 HTML**，首次 TTFB 1–8s（靠重试+备源）。
- 元数据适配器**禁止**调用 `compliance.get`、**禁止**落文件（ADR 0002 决策 5，要用测试钉住）——`csres.com` 不是 `*.gov.cn`。
- 之后：期 3 `guiju mcp` 服务面 → 期 4 归档 spec-currency。
- **开工前不要动 `spec-currency` 仓**，也不要往 guiju 里再塞一份地标解析器（会产生双份实现）。

**已否决、不要再提**：特种作业/特种设备证书查询产品。三个官方站（应急部 `cx.mem.gov.cn` 瑞数反爬 412、住建部 `zlaq` 接口 400+验证码、市场监管总局 `cnse` 滑块+查询码）全部挡住自动化，且身份证号属敏感个人信息（PIPL 28–29）——已与所有者确认放弃。

## 五、红线（不可绕过，不要加 override 开关）

- 只允许 `*.gov.cn`，且**每一次重定向**都要过白名单（`compliance._GuardedRedirect`）；POST 走同一道闸门。
- 限速 1.2s/请求/按 host，不调快。
- 命中验证码/人机校验即放弃（exit 40），**不尝试绕过**。
- 只搬运不生产；不承诺条文准确性。
- 依据：《标准化法》第 17 条 —— 强制性标准文本应当免费向社会公开。

## 六、环境（非敏感事实）

| 项 | 值 |
|---|---|
| 本机 | macOS；`guiju` 经 `uv tool install --editable` 装在 `~/.local/bin`（v0.14.4） |
| 数据 | `~/.guiju/catalog.db`（3254 条公告）+ `library/`（内容寻址 PDF） |
| 时效服务 | NAS `http://100.66.1.x:18083/mcp`（spec-currency，**健康**；NAS 只是有 ADR0048 的**变更冻结**，不是故障） |
| spec-currency 源码 | `~/Development/spec-currency`（Go 1.26），**不装 PATH**；按需 build 到 `/tmp` |
| 合并后部署 | 香港服务器 `hk`（38.76.165.16），供外网使用；本机不再常驻服务 |
| GitHub | 私有仓 `omnisurge/guiju`，本地 CI 权威（托管 runner 解析不了 mohurd 域名） |
