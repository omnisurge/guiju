# guiju 开发约束与工作指南

> 本文件是 `guiju` 仓库唯一通用开发约束入口。修改代码、审查或准备发版前必须阅读；更多流程见 [CONTRIBUTING.md](CONTRIBUTING.md)、架构边界见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
>
> **分流提示**：如果你是被指派来**调用 `guiju` 查规范 / 取正文 / 出引用串**的消费方 agent（而非修改本仓源码），请读 [docs/AGENT-USAGE.md](docs/AGENT-USAGE.md)。

---

## 1. 架构与合规红线

- **只搬运政府主动公开正文**：PDF 正文采集严格限定 `*.gov.cn`（`BodyPolicy`），逐跳校验重定向（`RedirectGuard`），`1.2s/host` 限速（`Throttle`），命中验证码（`40`）立即停止。严禁修改或绕过 `guiju/net/`（AST 边界由 `tests/test_net_boundaries.py` 强制守护）。
- **标准图集版权边界**：国标电子书库（`chinabuilding`）仅用于只读元数据时效核验与简明目录索引（独立快照 `atlas-catalog.json`），**严禁下载图集 PDF 正文**，`pull` / `cite` / MCP `announcement` 对图集必须显式拒绝。
- **运行时零第三方依赖**：`pyproject.toml` 保持零运行时第三方依赖（POSIX-only，Python 3.10+）。
- **数据库零迁移兼容**：`catalog.db` 保持 `SCHEMA_VERSION = 2`；涉及产品边界、退出码契约、JSON/TSV 形状或数据结构变更前，必须先立 `docs/adr/NNNN-*.md`。

---

## 2. 开发与门禁检查

- 局部代码修改按对应单测集验证（建议加 `-W error::ResourceWarning`）；交付或提交前必须全部通过：
  - `ruff check .`（0 告警）
  - Python 3.10 与 Python 3.13 全量离线单测：`python3 -m unittest discover -s tests -t .`
  - 本地 CI：`GUIJU_CURRENCY=off bash scripts/ci.sh`（包含 `check-release.py --docs-only`、`test_hygiene.py` 与离线冒烟）
- 联网验收 `python3 scripts/e2e.py` 不挂 git push，仅在发版前本机手动运行。

---

## 3. 发版前与核心改动红队审查

- 进入发版冻结、完成跨模块/契约改动或 ADR 阶段收口前，必须先按 [red-team-review](.agents/skills/red-team-review/SKILL.md) 执行红队代码审查：每条缺陷须给出精确文件行号与具体触发路径，无法举证直接输出「通过」，禁止风格、命名与主观防御建议。
- 发现缺陷并修复后，须对修复 diff 再做一轮轻量复查直至输出「通过」。

---

## 4. 版本、仓库与生产权限边界

- `guiju/_version.py` 是唯一版本号来源，需与 `CHANGELOG.md`、`README.md`、`HANDOFF.md`、`INSTALL.md`、`docs/ACCEPTANCE.md`、`docs/releases/` 及 `docs/audit/` 同步（见 `scripts/check-release.py`）。
- **未经所有者明确授权**：严禁 `git push`、打 tag、删除分支、修改 hk 生产环境（`guiju-mcp`）或触碰 NAS。
