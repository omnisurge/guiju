# 规矩 guiju — 交接文档

> 读这一份就够开工。产品设计与合规判断见 `docs/建筑规范CLI-蓝图.md`（v0.2）。
> 状态：M1 进行中，骨架可运行，扫描链路未压测。
>
> **2026-10-30 复核**：文件裁定见 `docs/audit-2026-10-30.md`，架构与语言决策见 `docs/ARCHITECTURE.md`（取代蓝图第三～七节）。
> 第六节的两条疑点已结论化，第七节第 1 条已作废 —— 冲突时以复核文档为准。

---

## 一、这是什么

从 `.gov.cn` 采集**工程建设标准正文 PDF** 的命令行工具，服务于监理的日常工作：
查规范 → 取正文 → 出条款引用串。

**中文名** 规矩 ｜ **命令** `guiju`（别名 `gj`）｜ **定位语** 以规范为准绳，让依据随手可取

首要用户是**作者本人 + 其 agent**，所以 `--json` 是主路径，人类可读输出是它的投影。

---

## 二、当前状态

| 模块 | 状态 | 说明 |
|---|---|---|
| `compliance.py` | ✅ 可用 | 域名白名单 / 限速 / 诚实 UA / 人机校验放弃 |
| `db.py` | ✅ 可用 | SQLite 四表，建表与查询已实测 |
| `sources/mohurd.py` 目录枚举 | ✅ 可用 | 已实测翻页、总数、条目解析 |
| `sources/mohurd.py` 详情与附件 | ⚠️ 部分验证 | 详情页可达、编号正则未在真页面上验证过 |
| `cli.py` sync | ✅ 可用 | 已实测 2 页 |
| `cli.py` search | ⚠️ 未实测 | 依赖 sync 出数据 |
| `cli.py` index / show / pull / cite / verify / provenance | ⚠️ 未实测 | 代码完整，端到端未跑过 |
| README / 打包 / CI | ❌ 未做 | |

**全量同步从未执行过**，`data/标准公告全目录.csv` 那份 3254 条是先前用临时脚本抓的，不是本 CLI 产出。

---

## 三、目录结构

```
guiju/
├── HANDOFF.md              ← 本文件
├── pyproject.toml           ← 入口 guiju / gj，纯标准库无依赖
├── guiju/
│   ├── cli.py               ← 8 个子命令 + 退出码
│   ├── db.py                ← announcements / codes / files / scanned / meta
│   ├── compliance.py        ← 唯一出站出口
│   └── sources/mohurd.py    ← 站点相关魔术值全集中在此，改版只改这里
├── docs/
│   └── 建筑规范CLI-蓝图.md   ← 产品+技术蓝图，含合法规避的四条红线
└── data/
    └── 标准公告全目录.csv     ← 3254 条历史目录（date,title,url）
```

---

## 四、怎么跑

```bash
cd /Users/user/Development/guiju
PY=/Users/user/.workbuddy/binaries/python/versions/3.13.12/bin/python3

$PY -m guiju --version
$PY -m guiju sync --pages 2 --page-size 20 --json
$PY -m guiju search 防火
```

- **依赖**：仅 Python 标准库（3.9+），无需 `pip install`。要装命令可用 `$PY -m pip install -e .`
- **数据库**：默认 `~/.guiju/catalog.db`，用 `GUIJU_HOME` 环境变量或 `--db` 覆盖
- ⚠️ 若在受限沙箱里跑，`~/.guiju` 可能被安全策略拦（报 `disk I/O error`），
  此时设 `GUIJU_HOME=/tmp/guiju` 即可，不要误判为代码 bug

---

## 五、已验证的事实（别重复试）

1. **诚实 UA 完全可用**。`guiju/0.1 (compliance-aware; official code fetcher)`
   访问列表接口和详情页均返回 200。**不需要伪装浏览器**，这是合规底线，不要动摇它。
2. **列表接口分页极易踩坑**：
   - `queryData` 必须**正好六项**：`webId / pageId / parseType / pageType / tagId / tplSetId`
   - **多传 `editType` 会返回空 `data`**（我在此浪费过一次）
   - 翻页靠 `paramJson={"pageNo":N,"pageSize":M}`，其中 `pageSize` 不能省
   - 总数从返回片段的 `count="3254"` 取
3. **列表条目 HTML 结构**：
   ```html
   <li class="date">
     <a class="fl" href="/gongkai/zc/wjk/art/..." title="标题">标题</a>
     <span class="date-info">2026-09-23</span>
   </li>
   ```
   日期在链接**之后**，不是 `<li>` 的属性，别写成 `re.match` 紧跟 `<li>`。
4. **UA 必须纯 ASCII**。HTTP 头走 latin-1，UA 里写中文会抛 `UnicodeEncodeError`。
5. **公告详情页常有两个附件**，第一个是「废止的强制性条文」(.doc)，第二个才是规范。
   `pick_attachment()` 已按「带规范名 + 非废止 + PDF」三级策略处理。
6. **旧链接已 100% 失效**：`/gongkai/zhengce/zhengcefilelib/YYYYMM/YYYYMMDD_ID.html`
   需映射为 `/gongkai/zc/wjk/art/YYYY/art_17339_ID.html`，见 `migrate_legacy_url()`。
7. **国标委全文公开系统不收录工程建设标准**（页面明文拒绝），不要往那条路走。

---

## 六、已结论化的疑点（2026-10-30 复核，原「未解决」）

**`pageSize=100` 单页返回 99 条 —— 已查明：不丢数据，可以放开使用。**

- 实体：33 页全量 = **3254 条**，唯一 URL **3254** 个；与 `data/标准公告全目录.csv` 双向比对 **0 差异**。
- 99 是接口单页渲染上限，不是数据缺口；总量 `count="3254"` 正确。
- 结论：全量同步 163 页 → **33 页**（约 1 分钟量级），把「本页 0 新增即停」保留，另加一次尾部抽样比对防站点静默截断。
- 编号正则已在真实详情页验证：2026 三页 3/3 命中；54 条分层样本编号覆盖 61%（其余正文确实无编号）。
- 真实的新风险不是漏数据，是**抓错**：多附件时 `pick_attachment` 的 `alive[0]` 兜底实测选中了无关文件（详见 audit 缺陷 #2）。

---

## 七、下一步建议（按优先级）

1. ~~查清 99 之谜~~ **已完成**：`pageSize=100` 全量 33 页 3254 条无缺失，设为默认（见第六节）。
2. **跑一次全量 sync**，确认落库 3254 条，与 `data/标准公告全目录.csv` 对账。
3. **验证编号提取**：`guiju index --match 防火 --limit 5`，看 `extract_codes()`
   在真实详情页上的召回率。注意详情页含「废止条文」附件说明，可能误抓条款号。
4. **端到端跑通 `pull`**：从编号到 PDF 落盘 + sha256，重点验证附件选取。
5. 补 README、`pip install -e .` 验证、给 `cite` 加单测（它是最不容易被改坏也最重要的）。
6. 装 `gj` 别名，确认 `gj search` 与 `guiju search` 等价。

---

## 八、合规红线（不可绕过，不要加 override 开关）

- 只允许 `*.gov.cn`，白名单在 `compliance.check_url()`，不要放开
- 限速 1.2s/请求，不要调快
- 命中验证码/人机校验即放弃，**不尝试绕过**
- 工具只搬运不生产内容，README 必须写清「不承诺条文准确性」
- 依据：《标准化法》第 17 条，强制性标准文本应当免费向社会公开

---

## 九、退出码约定

| 码 | 含义 | agent 该怎么做 |
|---|---|---|
| 0 | 成功 | 继续 |
| 10 | 已存在且校验通过 | 跳过，别重复下 |
| 20 | 源不可达 | 稍后重试 |
| 30 | 附件不存在或格式不符 | 换备源或报人工 |
| 31 | 编号未索引 | 先跑 `index` |
| 40 | 命中合规护栏 | **停止，不要重试** |
| 50 | 需人工介入（页面改版） | 上报 |
