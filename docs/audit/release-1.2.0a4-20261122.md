# guiju 1.2.0a4（v1.2.0-alpha.4）发版记录

- 日期：2026-11-22
- 版本：`1.2.0a4`（预发布；发布 tag `v1.2.0-alpha.4`）
- 分支：`refactor/root-cause`（基线 f98e0d2 + W1-W5/W6 修复）；发布提交以 tag `v1.2.0-alpha.4` 指向的提交为准（旧 tag `v1.2.0-alpha.3` 不移动）
- 状态：已发版（2026-11-22，W6 发版工程实施完成）；构建产物、SHA-256 与 hk 部署按所有者/Lead 冻结闸门另行执行并回填本记录
- 范围：B3（W2/W3/W4 本地合并）+ W1 网络/合规、W2 存储/同步、W3 时效/编号/地标、W4 CLI/pull、W5 MCP 的已批准修复批次 + W6 发布工程

## 1. 修复批次

- **W1 网络/合规**：短读 block 检测（`read_prefix` 及 body/metadata/redirect/error-body 全路径）、`.part.meta` 容错与可续传截断、200 HTML 识别、畸形 URL、mohurd 目录异常 → 50。
- **W2 存储/同步**：v1.1.0 升级库缺 `complete` 强制完整 sync、status 顶层聚合、唯一 URL 计数与重复页/页数上限、年份降级规则、`Store(read_only=True)`、mention.confidence 按公告、edition.status 真正落库。
- **W3 时效/编号/地标**：base fallback 完整性 AND、csres 零命中 31（`empty_authoritative` 分离）、`_demote_older` 年份、`_pick_current_version`/`_by_code` 只认 parsed、which 地标回落与 `prefer_exact`、显式未知配置不读旧缓存。
- **W4 CLI/pull**：批量失败体保留人工交接字段、单条 30/32/33 全路径 availability、批量 SQLite 错误映射（locked/busy→20、其余→50 并停止整批）、shzjw pull 落库支持 `cite --offline`。
- **W5 MCP**：batch 40 硬停止、`--db` 注入、query/verify 错误 payload 与 stale/from_cache 告警、HTTP Host/Origin allowlist 与线程上限、ReadOnlyStore schema 探测。

## 2. 验证门（2026-11-22）

| 门 | 结果 |
|---|---|
| 完整单测 | Python 3.13.5、3.10.21 各 **1223/1223 通过** |
| 本地 CI | ruff、1223 单测、`check-release.py --docs-only`、离线 e2e **4/4**、version/status/help 入口自检全绿 |
| 完整联网 e2e | `python3 scripts/e2e.py --require-currency` **39/39 通过**，exit 0（本机权威执行） |
| 构建烟测 | wheel/sdist 均为 `guiju-1.2.0a4`；临时 venv 安装 wheel 后 `guiju version --json` 返回 `1.2.0a4`（2026-11-22 实测） |
| 发版产物门 | `python3 scripts/check-release.py --artifacts`（dist/SHA256SUMS 与 tag-HEAD 一致，2026-11-22 实测） |

## 3. 发布产物

| 文件 | SHA-256 |
|---|---|
| `guiju-1.2.0a4-py3-none-any.whl` | `31352cb545061c19cfa73e60ff20a894cc1fb7b7b1dda950b16b4fb22b12e13e` |
| `guiju-1.2.0a4.tar.gz` | `d1079807e3ef27a360fdb6c13eb8bd0f30151d0b2743379e2f6340e20624d5d2` |
| `SHA256SUMS` | 见 `dist/SHA256SUMS`（与上述两行一致） |

发布页：https://github.com/omnisurge/guiju/releases/tag/v1.2.0-alpha.4

## 4. 文档与历史记录

- 当前版本文档已同步：README 状态表、HANDOFF 首段、INSTALL 标题/下载名、ACCEPTANCE 顶部与 v1.2.0-alpha.4 段。
- 历史审计记录不改写：`docs/audit/b3-w2-w3-w4-20261122.md` 的 **1010 tests OK** 与 `docs/remediation-2026-11-18.md` 的当时读数保留；本记录只追加后续状态，`docs/README.md` 索引与 remediation 顶部已加“后续状态见新记录”。
- 平台声明：本版本 POSIX-only（Linux / macOS），pyproject 不再声明 `Operating System :: OS Independent`。

## 5. 边界

- 不碰 NAS/kb-system；hk 部署另走所有者闸门，本记录不包含部署步骤。
- 不移动旧 tag `v1.2.0-alpha.3`；不新增第三方运行时依赖、不新增退出码。
