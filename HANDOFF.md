# 规矩 guiju — 交接文档

> 一句话：从 `*.gov.cn` 取工程建设标准的**目录 + 正文 + 引用串**，只搬运与校验，不生产内容。
> 状态：**v0.5.0 全命令面验收通过**（31/31，见 `docs/ACCEPTANCE.md`），单测 69 项；
> M1 目录对账达成（3254/3254），`index` 已废止为主路径依赖。

## 文档地图（各司其职，不要互相抄）

| 文件 | 唯一职责 | 谁读 |
|---|---|---|
| `README.md` | 用户手册：安装、命令、退出码、合规与不做的事 | 人 |
| `AGENTS.md` | agent 操作契约：命令选择、退出码动作、必知事实 | agent |
| `docs/ARCHITECTURE.md` | 架构决策：分层、数据模型 v2、里程碑、为什么不是 Rust | 维护者 |
| `docs/audit-2026-10-30.md` | 审计证据：逐文件裁定 + 十条缺陷 + 实测数据 | 维护者 |
| `docs/adr/0001-currency-and-scope.md` | 为什么废止 index、为什么与 spec-currency 不合仓 | 维护者 |
| `docs/ACCEPTANCE.md` | 端到端验收记录（可复跑 `scripts/e2e.py`） | 所有人 |
| `CHANGELOG.md` | 版本变更 | 所有人 |
| 本文件 | 交接状态与"已验证/别重犯"的事实清单 | 接手人 |

## 一、现在怎么跑

```bash
PY=/Users/user/.workbuddy/binaries/python/versions/3.13.12/bin/python3
cd ~/Development/guiju

$PY -m unittest discover -s tests -t . -v     # 33 项，离线，零依赖
$PY -m guiju status                            # 库状态 + 可得性口径
$PY -m guiju sync                              # 首次全量 33 页 ≈ 60s
$PY -m guiju index --match 防火 --limit 20      # 详情页 → 编号（可断点续跑）
$PY -m guiju pull GB55037-2022 --json          # 幂等，已存在则退出 10
$PY -m guiju cite GB55037-2022 5.2.1           # → 《…》GB 55037-2022 第5.2.1条
```

- 零第三方依赖（Python 3.10+ 标准库）；`pip install -e .` 后得到 `guiju` 与 `gj`
- 数据默认 `~/.guiju/`（`catalog.db` + `library/`）；`GUIJU_HOME` 或 `--db` 覆盖
- 受限沙箱里 `~/.guiju` 可能被拦（`disk I/O error`）→ `export GUIJU_HOME=/tmp/guiju`

## 二、已验证的事实（别重复试，也别推翻）

1. **诚实 UA 完全可用**：`guiju/0.2.0 (compliance-aware; official code fetcher)` 访问列表与详情均 200。**不要伪装浏览器**，这是合规底线。
2. **目录全量 = 3254 条 / 33 页**（`pageSize=100`）。实测与 `tests/fixtures/catalog-2026-10-30.csv` 双向零差异。单页渲染上限 99 条**不丢数据** —— 旧文档"不要用 pageSize>20"的警告已作废。
3. **列表接口**：`queryData` 必须正好六项，多传 `editType` 返回空；`pageNo` 必须塞进 `paramJson` 的 JSON 字符串；总数从 `count="3254"` 取。
4. **条目 HTML**：日期在链接**之后**（`<span class="date-info">`），不是 `<li>` 的属性。
5. **UA 必须纯 ASCII**（HTTP 头 latin-1），中文会 `UnicodeEncodeError`。
6. **附件陷阱**：早期公告的第一个附件常是「废止的强制性条文」(.doc)；英文版公告的附件不是官方中文正文。现已由 `domain/attachment.py` 显式裁定，**禁止恢复"取第一个"的兜底**。
7. **编号是新旧双份的常态**：`JGJ147-2016`/`2004`、`GB/T50362-2022`/`2005`；局部修订原文写「标准编号修改为 JGJ/T450-2018」。角色判定见 `domain/codes.py`，**不要简化成取第一个**。
8. **正文可得性分档**：2014 及以后基本都有附件；2000–2013 多为"只有名称与编号"。全库实测约 52%（54 条分层样本）。这条已写进代码（`domain/availability.py`）与所有失败提示。
9. **旧链接 100% 失效**：`/gongkai/zhengce/zhengcefilelib/YYYYMM/...` 需映射为 `art_17339_*`（`mohurd.migrate_legacy_url`）。
10. **国标委全文公开系统不收录工程建设标准**，不要往那条路走。
11. **`https://www.mohurd.gov.cn/robots.txt` → 404**（无 robots 限制）。若实现 robots 解析，404 应视为全允许。

## 三、下一步（按优先级）

1. ~~M1 全量 index~~ **已废止**：`index` 不再维护（见 `docs/adr/0001-currency-and-scope.md`）。编号 → 名称交给 `spec-currency`，名称 → 公告交给本地三层召回检索。
   剩下要做的是：给 `status` 补 `probe` 失败分布、以及把 `currency` 的降级情况计数。
2. **M2 收尾**：37 本强制性规范 `pull` 全量验收（0 垃圾文件），并跑一次 `verify`。
3. **M2 加分项**：`guiju mcp`（stdio JSON-RPC 薄壳），让任意 agent 直接调。
4. **M3**：上海 `zjw.sh.gov.cn` / 四川消防两源，配冲突仲裁（同版本多源：官方发布公告优先）。
5. **M4**：`probe` 台账 → 适配器健康度与"页面指纹变更"告警（`announcement.body_sha256` 已有字段，待填充）。

## 四、红线（不可绕过，不要加 override 开关）

- 只允许 `*.gov.cn`，且**每一次重定向**都要过白名单（`compliance._GuardedRedirect`）
- 限速 1.2s/请求/按 host，不调快
- 命中验证码/人机校验即放弃，**不尝试绕过**
- 只搬运不生产；README/LICENSE 已写明不承诺条文准确性
- 依据：《标准化法》第 17 条 —— 强制性标准文本应当免费向社会公开
