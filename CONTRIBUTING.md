# 参与开发

> 一个人也要按流程走：流程不是给别人看的，是给"三个月后的自己"用的。

## 环境

```bash
python3 -m unittest discover -s tests -t .   # 单测：离线、零依赖
python3 scripts/e2e.py --offline             # 离线冒烟
python3 scripts/e2e.py                       # 端到端：联网，含一次正文下载
uv tool run ruff check .                     # 静态检查（不装进运行时）
```

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
