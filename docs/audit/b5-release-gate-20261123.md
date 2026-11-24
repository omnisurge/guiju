# B5 / 1.2.0 稳定版门禁评估（2026-11-23）

> 状态：**B5 工程门禁已通过（本机候选/测试/文档预检/回退演练）；1.2.0 稳定版尚未正式发布（owner gate 未打开）；hk 仍为旧 unit，强化部署未执行。**
> 依据：ADR 0004 第十节、ADR 0005 I4、`docs/remediation-2026-11-18.md` P5。
> 本记录不把“候选包已构建/已演练”冒充“已发版”，也不含 commit/tag/push/部署。

## 1. 前置：B4 已关闭

- reviewer-domain-mcp：**APPROVE**（B4 rev2，2026-11-23）。
- reviewer-cli：**APPROVE**（task-69，2026-11-23）。
- 证据：[b4-application-mcp-20261123.md](b4-application-mcp-20261123.md)；全量 1223×2、ruff、CI、docs-only、离线 e2e 4/4 通过。

## 2. 当前验证矩阵（本工作树）

| 门 | 结果 |
|---|---|
| Python 3.13 全量 | **1223/1223 OK**（74.1s） |
| Python 3.10 全量 | **1223/1223 OK**（72.3s） |
| `scripts/ci.sh` | exit 0（含 ruff、全量、docs-only 44、离线 e2e 4/4、入口自检） |
| `ruff check .` | All checks passed |
| `check-release.py --docs-only` | PASS（44 项，当前 `1.2.0a4` 基线） |
| `git diff --check` | 干净 |
| 完整联网 e2e | **39/39 通过，exit 0**（本轮工作树 2026-11-23 实测，`python3 scripts/e2e.py`） |

## 3. 1.2.0 候选构建（本地演练，不是发布物）

在临时副本中把 `__version__` 置为 `1.2.0`，从当前工作树构建：

| 文件 | SHA-256 |
|---|---|
| `guiju-1.2.0-py3-none-any.whl` | `fcaef5b6c0c88ac6df566282f0a42998fcaba0619e716e5ffc3ec3e953235dd9` |
| `guiju-1.2.0.tar.gz` | `975c739901b22f2981d43c8e8121ab5b5fd82b8a08d1cc61926498691d0fb257` |

暂存：`dist-candidates/1.2.0/`（含 `SHA256SUMS`）。该目录不是发布目录，未 tag、未 push、未上传。

发版检查器已补稳定版断言：`scripts/check-release.py` 的 `_release_section` 现在同时接受 `## v1.2.0（date）` 与预发布标题；`tests/test_hygiene.py` 在原有 docs-only 测试内加了该回归断言（不新增测试计数）。

稳定版文档预检：在临时副本 `/tmp/guiju-1.2.0-candidate2` 中生成 `1.2.0` 的 CHANGELOG/README/HANDOFF/INSTALL/ACCEPTANCE/发版记录/release notes，`check-release.py --docs-only` 通过 **44 项**；再以 `--artifacts --dist-dir dist-candidates/1.2.0 --skip-tag` 核对候选 wheel/sdist 与 ACCEPTANCE 哈希，通过 **58 项**（tag 检查按 `--skip-tag` 留待正式发版）。模板暂存在 `dist-candidates/1.2.0/release-docs/`。主工作树仍保持 `1.2.0a4`，未把这些发版文档直接应用。

静态 systemd 边界回归：`tests/test_hygiene.py` 已在原有 docs-only 测试内锁定 `deploy/guiju-mcp.service` 的 `User=guiju`、`Group=guiju`、`ProtectSystem=strict`、`ReadWritePaths=/var/lib/guiju`、`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`、`CapabilityBoundingSet=`，不新增测试计数。

B5 清理：`guiju/application/pull.py`、`guiju/application/cite.py` 已移除 `_ReporterProxy` 动态代理，改为直接 `from .. import report as render`；不再保留“静态不 import CLI、运行时经代理再取”的兼容层，全量测试仍通过。

## 4. 回退演练（wheel / DB / systemd）

演练目录 `/tmp/guiju-rollback-drill-20261123b`：

1. 新建 venv，安装候选 `guiju-1.2.0` wheel；`guiju version --json` → `"version": "1.2.0"`。
2. 在临时 `GUIJU_HOME` 构造 catalog.db，写入 `rollback_drill_marker=before-rollback`，备份 DB。
3. 强制安装上一发布 wheel `guiju-1.2.0a4` 模拟回退；恢复 DB 备份。
4. `guiju version --json` → `"version": "1.2.0a4"`；读取 `rollback_drill_marker` → `before-rollback`，DB 完整。
5. systemd：`deploy/guiju-mcp.service` 备份/恢复 `cmp` 通过（UNIT_ROUNDTRIP_OK），静态字段 User/Group/ProtectSystem/ReadWritePaths/RestrictAddressFamilies 已核；**未在 hk 部署/重启**。

结论：wheel 升级/回退与 DB 备份/恢复路径可复现；systemd 仅完成文件级回退演练，真实服务重启仍属于 hk 所有者窗口。

## 5. P2-4 / P2-5

- P2-4（csres 明文回退不落缓存）：所有者 2026-11-24 决定**保持现状**（安全默认）；不改为显式开关、不缓存明文结果。决策与多网络实测记录见 [p2-4-p2-5-decision-20261123.md](p2-4-p2-5-decision-20261123.md)。该产品债按决策关闭。
- P2-5（probe_sink 底座）：2026-11-23 round 2 已接线 `compliance/_body_client` → `sources` → `application.pull._probe_sink` → `Store.log_probe`；`body_sha256` 仍未加入 pull 成功 JSON，不能冒充 M4 完整实现。P2-5 接线不阻塞 B4/B5 工程门禁。

## 6. 关闭判断

已满足/已通过：B4 双审、全量双版本、CI、docs-only、离线 e2e、在线 e2e、候选 wheel/sdist 构建、wheel+DB 回退、systemd 文件回退、P2-5 probe_sink 接线。

仍未满足（owner gate）：

- 正式发版需要干净 Git 工作树、版本/CHANGELOG/README/HANDOFF/INSTALL/ACCEPTANCE 的 1.2.0 定稿、release notes、tag **v1.2.0** 指向 HEAD；
- 需要所有者授权 commit/tag/push/GitHub Release；
- 需要真实 hk 服务的 systemd 重启/健康检查/回滚演练（当前明确未做）；只读核查见 [b5-hk-preflight-20261123.md](b5-hk-preflight-20261123.md)：hk 当前仍是旧 unit（User/Group 空、ProtectSystem=no），尚无 guiju 用户，必须所有者批准后按 deploy/README 落地。

**因此当前只能标记“B5 工程门禁已通过（本机）”，不能标记 1.2.0 稳定版已发布，也不能声称 B5 全部关闭。**
