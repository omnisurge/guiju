# 数据源现状与边界（2026-10-31 实测）

> 目的：把"哪些源能用、哪些用不了、为什么"写成可复查的记录。
> 每条结论后面都跟着当时的实测证据，下次怀疑"是不是源坏了"时先看这一页。

## 一、已接入

### 1. 住建部 · www.mohurd.gov.cn（主源，国标 / 行标）

| 项 | 内容 |
|---|---|
| 能力 | 公告列表 → 详情页 → 正文 PDF 附件 |
| 量级 | 3254 条公告（33 页 × 100），全量同步 ≈50–60s，增量 ≈3s |
| 编号 | 只出现在详情页正文里，需解析（`classify` + 角色判定） |
| 状态 | 公告本身不表态"现行/废止"，现行性靠外部时效服务 |
| 接入 | `guiju/sources/mohurd.py`（默认源） |
| 合规 | `*.gov.cn` 白名单 + 逐跳重定向校验 + 1.2s/host |

### 2. 上海 · bzjg.zjw.sh.gov.cn:9175（地方标准，正文直取）

| 项 | 内容 |
|---|---|
| 能力 | 检索 + **正文 PDF 直接下载**（`Content-Disposition: attachment`） |
| 端点 | `POST /JGBStandardWebApi/external/localStandard/list`；`GET /JGBStandardWebApi/api/files/webDownload` |
| 令牌 | `receivedToken = HMAC_SHA256("<fileId>:<timestamp>", "jkysh2jgbstandard")`，密钥写在公开前端 bundle 里（非破解） |
| 分栏 | 现行标准 548 / 废止标准 334 / 现行设计标准 37 / 废止设计标准 23 |
| 状态 | **源站自己表态**，适配器照抄：`abolishTime`/`isAbolish` → `withdrawn`，否则 `current` |
| 编号 | 含年版（`DG/TJ08-2007-2026`），原样采用，不套通用解析器 |
| 接入 | `guiju/sources/shzjw.py`，`api_mode = True`（没有公告页可抓） |
| 实测 | `guiju pull --source shzjw DG/TJ08-2007` → 2,084,644 B，`%PDF-1.7`，sha256 `d4651231c9b1…` |

## 二、评估后不接入

### 3. 国家标准信息公共服务平台 · std.samr.gov.cn

| 结论 | **只能按名称关键词检索，不能按编号查 —— 对我们的场景没有用** |
|---|---|
| 接口 | `GET /gb/search/gbQueryPage?pageSize=&pageNumber=&searchText=` |
| 可用 | `searchText=防火` → 200，`total=89` |
| 不可用 | `searchText=55037` / `GB 55037` / `GB55037-2022` → `total=0`；`stdCode=55037` 参数被忽略（返回全部 79826 条） |
| 字段 | 只有 `id / C_C_NAME / C_STD_CODE / STD_NATURE / ACT_DATE / STATE / ISSUE_DATE / PROJECT_ID` —— **没有任何文件或正文链接** |

判定：我们的场景是"手里只有编号、不知道名称"，它恰好答不出来。硬接只会得到一个永远答不出问题的源。

### 4. 国家标准全文公开系统 · openstd.samr.gov.cn

| 结论 | **页面标题就是「国家标准全文阅读」；普通 GET 一律返回 0 条结果，无法程序化取用** |
|---|---|
| 实测 | `/bzgk/gb/std_list?p.p1=0&p.p90=circulation_date&p.p91=desc&p.p2=55037` → 200，页内 `共 0 条标准` |
| 实测 | 换成中文标准名同样 0 条（结果表依赖前端会话渲染） |
| 政策 | 该系统只提供**在线阅读**，没有下载入口；这类"在线阅读"不当作"下载"计入能力 |

## 三、由此产生的替代方案

"国标编号 → 名称/状态"拿不到官方程序接口，于是改用**本地时效缓存**（v0.12.0）：

- 查成功的编号落库（`currency_cache` 表，只存元数据）；
- 时效服务不可达时用缓存回答，并明确标出 `currency_from_cache=true` + 缓存天数；
- 空结果不落库，避免把"没查到"固化下来。

边界诚实说明：**没查过的编号，服务断了仍然查不到** —— 这是官方不开放接口的直接后果，不是实现缺陷。

## 四、接入新源的检查清单

按这五条过一遍，过不了就别接（接进来只会变成"永远答不出"的源）：

1. **能按编号查吗？** 只能按名称查的源，除非我们已经知道名称，否则没有价值。
2. **正文是官方原件吗？** 需要 `Content-Disposition: attachment` 或明确的 PDF 响应；在线阅读/图片流不算。
3. **状态口径是源站自己给的吗？** 自己按年份猜的状态一律不用（宁可 `unknown`）。
4. **域名在 `*.gov.cn` 内吗？** 不在就不进 `compliance` 白名单，不做例外。
5. **有稳定 API 还是只能抓 HTML？** 只能抓 HTML 的要预留"站点改版 → 50 needs_human"的维护成本。

## 五、红线（不因增加源而改变）

- 只抓 `*.gov.cn`，逐跳校验重定向，1.2s/host 限速，遇验证码退出 40。
- 不镜像、不转存、不把 PDF 再分发出去。
- 不为了"多一个源"在白名单或限速上开口子；POST 也必须走同一道闸门（`compliance.post_json`）。
