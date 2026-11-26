# 标准图集离线黄金 HTML 夹具（国标电子书库）

- 采集日期：**2026-11-26**
- 来源站点：`https://ebook.chinabuilding.com.cn`（中国建筑标准设计研究院官方平台）
- 边界约束：所有夹具**仅含公开检索/详情/简明目录 HTML 结构元数据**，不含任何图集 PDF、试读页面、登录态或用户会话信息。

| 文件名 | 大小 | 原始端点 | 覆盖验证目标 |
|---|---|---|---|
| `search_22g101_1.html` | 12,055 B | `/zbooklib/search?SiteID=1&contentType=book&SearchKey=22G101-1` | 精确现行命中（`bookID=153493`、`现行`、发布/实施/废止日期、`var data` JSON） |
| `search_16g101_1.html` | 17,143 B | `/zbooklib/search?SiteID=1&contentType=book&SearchKey=16G101-1` | **串号陷阱**：`B16G101-1`（中英文双语版 `现行`）排在 `16G101-1`（`废止`，2022-05-01）之前 |
| `search_g101_1.html` | 58,593 B | `/zbooklib/search?SiteID=1&contentType=book&SearchKey=G101-1` | 无年份基础号全代际召回及 `23G101-11`（答疑图解）/`11G101-1 英文版` 撞号边界 |
| `search_empty_99z999.html` | 4,320 B | `/zbooklib/search?SiteID=1&contentType=book&SearchKey=99Z999` | 权威空结果信号（`var data = {"fieldType":[],"showStat":"false"}`） |
| `detail_61268_16g101_1.html` | 2,296 B | `/zbooklib/book/detail/show?SiteID=1&bookID=61268` | 方案 A 废止替代解析：`废止日期：2022-05-01` + `代替标准号：22G101-1` |
| `detail_153493_22g101_1.html` | 2,805 B | `/zbooklib/book/detail/show?SiteID=1&bookID=153493` | 现行详情页解析：`被代替标准号：16G101-1` + 主编单位 |
| `concise_catalogue_sample.html` | 20,579 B | `/zbooklib/conciseCatalogueList?SiteID=1&sublibID=2118&currentSublibInnerCode=0029` | 全表 `th1–th6` 表头 + 22 行代表性样本（含国标、中南标 `21ZJ111`、28 省汉字前缀代表 `粤25G/T116`/`京05G69`/`冀00J201`、`DBJ/T02-81-2013-12D1`、合订本 `S901～902`/`D101-1～7`/`DX003～004`、`11SJ937-1（1）` 及按 `bookID` 去重用重复行） |

> 注：以上文件仅剥离通用页头/页脚/无关导航与冗余脚本，内部表格、卡片、`var data` 与原始换行空白均为原站响应逐段截取。
