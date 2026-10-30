# 变更记录

## 0.2.0 — 2026-10-30（M0：能站住）

按 `docs/audit-2026-10-30.md` 的裁定执行重构，行为契约与数据模型同时升级。

### 修复（每条都有实测证据）

- **合规红线**：白名单现在对**每一次重定向**生效（原实现只在首请求校验，`urllib` 自动跟随 30x 可绕过）。`compliance._GuardedRedirect`
- **抓错文件**：附件裁定删除"兜底取第一个"；实测 2022-01-18 英文版公告曾选中无关文件。现在多候选即 `33`，英文版公告直接 `not_applicable`
- **引用错版本**：编号引入 `role`（`primary`/`target_of_revision`/`superseded_ref`/`mentioned`）与原文 `evidence`；同号旧版本自动降级，`cite` 不再可能引用被代替版本
- **`guiju --version --json` 退出 2**：全局开关改为 `default=SUPPRESS` + 父 parser，任意位置可用
- **未预期异常裸奔**：统一兜底为 `exit=1` + 结构化 JSON，traceback 只进 stderr
- **`provenance` 依赖绝对路径**：改为按 sha256 反查，文件移动后仍可溯源
- **`--out` 默认写当前目录**：默认改为 `~/.guiju/library`
- **限速不分 host**：改为按 host 计间隔
- **反爬检测阈值**：只对 text/html·json 且 <2MB 的响应抽头部 8KB 扫描

### 数据模型 v2（旧库自动备份重建）

- `announcement` / `edition` / `mention`(role+evidence) / `scan` / `artifact`(sha256 内容寻址) / `probe` / `corpus_meta`
- `PRAGMA user_version` 管版本；检测到旧库即备份为 `catalog.db.legacy-*` 后重建
- 允许"无编号但有正文"（39% 的公告确实没有编号）

### 新增

- `guiju status`：库状态、覆盖度、可得性口径
- `guiju cite --strict`：有未核实项即 `32`
- 退出码 `32`（编号歧义）/ `33`（附件歧义）
- 可得性分档进代码：`domain/availability.py`，`FULL_TEXT_SINCE_YEAR = 2014`；所有失败体都带 `availability` 与中文 `hint`
- `tests/` 33 项零依赖单测（编号角色 / 附件裁定 / 引用串 / 合规闸门 / 目录解析 / 可得性）

### 结构

- 拆分为 `cli/ commands/ domain/ store/ sources/` + `errors.py`（错误→退出码单一映射）
- 新增 `README.md`、`AGENTS.md`、`LICENSE`、`.gitignore`
- `data/标准公告全目录.csv` → `tests/fixtures/catalog-2026-10-30.csv`（黄金快照，线上零差异）
- 版本号单一来源：`guiju/_version.py` + `pyproject` 动态读取
