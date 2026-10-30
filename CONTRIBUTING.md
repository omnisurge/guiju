# 参与开发

> 一个人也要按流程走：流程不是给别人看的，是给"三个月后的自己"用的。

## 本地 CI 是权威门

```bash
scripts/install-hooks.sh   # 一次性：启用 .githooks/pre-push
scripts/ci.sh              # ruff + 单测 + 离线冒烟 + 入口自检
```

- `pre-push` 会自动跑 `scripts/ci.sh`，**没过不推**；`SKIP_CI=1 git push` 可强制跳过（不建议）。
- **不依赖 GitHub**：本地 CI 是判断"能不能提交"的唯一依据；GitHub 上的 `ci.yml` 只是同一套门的旁证。
- 联网的端到端验收**不在 push 上跑**（不拿政府站点当压测对象），发版前手动跑：
  `python3 scripts/e2e.py`（有时效服务 → 38 项；没有 → 23 项 + 1 组 SKIP）。
- **托管 runner 跑不了这个验收**：实测 GitHub runner 解析 `www.mohurd.gov.cn` 会
  `Temporary failure in name resolution`（跨境 DNS 限制）。`e2e-live.yml` 只是便利入口，
  带站点预检、遇解析失败会直接说明"环境不合格，不是产品回归"；**权威执行始终在本机**。
- 硬约束已写成测试（`tests/test_hygiene.py`）：CI 矩阵必须覆盖 `requires-python` 下限、
  联网验收不得挂 push、`.gitignore` 必须挡住 `*.pdf`/`library/`/`*.db`、版本号与 CHANGELOG 同源。

**运行时零第三方依赖**是有意为之（可分发性 + 合规审计）。不要为了省事往 `dependencies` 里加包。

## 分支与合并

- `main` 永远可发布；改动走分支 + PR。
- PR 合并前必须：`ruff` 干净、单测全绿、`e2e --offline` 通过。
- 提交信息：`<type>: <中文摘要>`，type 用 `feat|fix|docs|chore|test|refactor`；
  行为变化要写清"为什么"和"实测证据"，不要只写"优化"。

## 什么时候必须写 ADR

命中任意一条就要写 `docs/adr/NNNN-*.md`：

- 改变产品边界（抓什么、不抓什么、谁的服务面）
- 改变退出码契约、JSON 形状、TSV 契约（有下游消费者）
- 引入或放弃一个重要组件或数据表
- 推翻旧 ADR 的结论（**必须在旧 ADR 顶部标注"被 NNNN 取代"并回链**）

## 版本与发布

1. `guiju/_version.py` 是唯一版本源（`CHANGELOG.md` 同版本落一条）
2. 验收：`python3 scripts/e2e.py` 全绿，把结果补进 `docs/ACCEPTANCE.md`
3. `git tag -a vX.Y.Z -m "..."` + `gh release create`
4. 破坏性变化（退出码、JSON 字段）用 `!` 标注，并在 CHANGELOG 顶部写迁移说明

## 红线（不要加 override 开关）

只抓 `*.gov.cn`、逐跳校验重定向、1.2s/请求限速、命中验证码即放弃、不伪造 UA。
细节见 `README.md`「边界」与 `AGENTS.md`「不要做的事」。
