# ADR 0008 · 开发态与使用态 Agent 约束/技能解耦及跨 Harness 分发

- 状态：**已接受（2026-11-27）**
- 决策人：所有者
- 触发：`v1.4.0` 收口时已将仓库根 `AGENTS.md`（开发者/发版约束）与 [AGENT-USAGE.md](../AGENT-USAGE.md)（消费方使用契约）拆分，并在本仓 `.agents/skills/red-team-review/SKILL.md` 沉淀了开发审查技能；但下游消费者安装 `guiju` 后，仍缺少随产品包分发、通用于任意 AI agent / harness 且内容彻底净化的产品态使用契约与可移植 Agent Skills。
- 影响：`pyproject.toml`、`guiju/errors.py`、`guiju/mcp.py`、`guiju/cli/__init__.py`、新增 `guiju/agent_assets/`（含 `__init__.py`）、新增 `guiju/domain/agent_assets.py`（纯逻辑）、新增 `guiju/store/agent_receipts.py`（持久化与文件锁）、新增 `guiju/application/agent.py`（路径解析与编排）、新增 `guiju/cli/commands/agent.py`、`scripts/check-release.py`、`README.md`、[AGENT-USAGE.md](../AGENT-USAGE.md)、`tests/`
- 关系：延续 [ADR 0004](0004-contract-first-hardening.md)（契约优先）、[ADR 0006](0006-v1-3-0-architecture-and-mcp-purification.md)（架构分层与 MCP 纯净化）及 [ADR 0007](0007-atlas-currency-and-catalog.md)（PEP 639 打包治理）；遵守 [ARCHITECTURE.md](../ARCHITECTURE.md) 依赖方向铁律；具体迭代排期与发版门禁见对应版本 Spec（首期为 [spec-1.5.0.md](../spec-1.5.0.md)）。

---

## 一、背景与已核实的架构前提

1. **职责混杂与内容级泄露**：开发约束关心源码分层、`guiju/net/` AST 边界、`scripts/ci.sh` 与发版审查；消费方 agent 关心 `--json` 协议、退出码动作决策、`which/pull/cite/atlas` 选用与 MCP 工具边界。仅靠构建时“文件路径排除”不够——现行 [AGENT-USAGE.md](../AGENT-USAGE.md) 正文中已核实存在三处仓内/开发态泄露，直接打包或打印给离仓消费者会构成死链或越层泄露：
   - 顶部保留仓内相对链接 `../README.md` 与 `../AGENTS.md`（`docs/AGENT-USAGE.md:4`）；
   - 退出码 `50` 描述直接点名修改源码文件 `guiju/sources/mohurd.py`（`docs/AGENT-USAGE.md:73`）；
   - “不要做的事”列出源码内部模块与类 `guiju/net/（BodyPolicy / MetadataPolicy / RedirectGuard / Throttle）`（`docs/AGENT-USAGE.md:158`）。
2. **多头副本破坏单一真源**：不仅跨目录不能双写，包内 `guiju/agent_assets/` 内部同样不能并行维护 `contract.md`、`SKILL.md` 与手工提炼的 `mcp-instructions.txt` 三份正文副本。
3. **分层边界约束（B1）**：按 [ARCHITECTURE.md](../ARCHITECTURE.md#L168-L182)，`domain/` 必须保持纯逻辑（不碰磁盘、网络与 DB，也不解析 `Path.home()`/`cwd`），持久化与原子 JSON 写入归 `store/`（对标 `guiju/store/atlas.py`），路径解析与编排归 `application/`。
4. **退出码独立语义**：既有 `EXIT_TABLE` 中，`1` 专指未捕获程序缺陷并附 stderr traceback，`10` 专指规范正文/本地同名文件已存在（幂等成功），`30–50` 属规范检索与采集业务域。资产安装冲突、用户手工修改、非受管卸载拒绝与符号链接拦截需要明确独立的退出码。
5. **跨机与跨工具现实边界（B3/B4）**：`--scope project` 生成的 skill 文件可能随项目仓库提交而跨机分发到缺少本机 `GUIJU_HOME` 收据的新环境（或沙箱临时切换 `GUIJU_HOME`），或在团队成员升级后通过 `git pull` / `git checkout` 更新已登记工作区的磁盘内容，因此无论是否已有本机收据都需要干净官方资产的**认领（adopt）与平滑升级/同步**能力；同时必须补齐 `--force`、卸载（含目录已手工删除）、收据损坏与符号链接防逃逸的完整语义，并将单次发版门禁从本 ADR 解耦出去。

---

## 二、决策

### D1 双层净化隔离（路径级 + 内容级）

- **路径级隔离与验证分工**：
  - 仓库根 `AGENTS.md`、`.agents/`、`CONTRIBUTING.md`、`docs/`、`scripts/`、`tests/` 属源码与治理资产，**严禁打入 wheel**；
  - 离线单测负责静态校验 `pyproject.toml`（`guiju/agent_assets/__init__.py` 与 `[tool.setuptools.package-data]`）；**真实构建产物 wheel 的路径黑/白名单检验纳入 `scripts/check-release.py --artifacts`**，避免离线单测在缺 `setuptools` 或严格断网环境下触发构建隔离失败。
- **内容级词法与链接纯净守卫（扫描范围写死）**：
  - 自动化测试必须全量扫描以下 **5 处消费方可见面**：
    1. `guiju/agent_assets/*` 原始资产及运行时拼装生成的 `SKILL.md`；
    2. `McpServer.initialize` 返回的 `instructions`；
    3. `MCP TOOLS[*]["description"]`；
    4. `guiju --help` 尾注及 `EXIT_TABLE` 渲染文案；
    5. 消费方命令路径可触达的错误 `hint`（其中 `NeedsHuman`（`50`）仅作“站点可能改版，带错误输出上报维护者”，不得泄露源码路径、测试文件或内部策略类名；历史错误常量注释随版本整改同步净化）。
  - 以上消费方文本**严禁出现**：仓内相对路径与相对 Markdown 链接（如 `../`、`./docs/`）、源码内部路径与类名（`guiju/net/`、`guiju/sources/`、`BodyPolicy`、`MetadataPolicy`、`RedirectGuard`、`Throttle`、`scripts/`、`test_*.py`、`pyproject.toml`）、以及开发态规范与技能名称（`red-team-review`、本仓开发 `AGENTS.md`、`git push`、`scripts/ci.sh` 等）。

### D2 真正单一真源（SSOT）与通用跨 Agent 两种交付形态

- **包内唯一真源结构（消除三份正文副本，B2）**：
  - `guiju/agent_assets/` 为含 `__init__.py` 的标准 Python 子包（确保 `setuptools.packages.find` 纳入，避免带连字符的目录名 `guiju-standards` 误充当包名），目录内仅放三个资产文件并通过 `pyproject.toml` `[tool.setuptools.package-data]` 以路径 glob 显式打包（含 `*.md`、`*.yaml`、`*.json`）：
    1. `contract.md`：**唯一消费方正文真源**。正文内使用成对 HTML 注释标记 `<!-- guiju:mcp-summary:begin -->` … `<!-- guiju:mcp-summary:end -->` 标定供 MCP 引导截取的核心片段；
    2. `skill_frontmatter.yaml`：仅含开放 Agent Skills 标准 YAML frontmatter（`name: guiju-standards`、`description: ...`），**零规则正文**；
    3. `history_hashes.json`：记录当前版本及后续历史已发布版本（自 `1.5.0` 起逐版累积，并供单测注入模拟历史版本）的受管相对文件路径 → SHA-256 映射表，用于已登记及未登记目标的官方版本干净态判定与跨机认领；`scripts/check-release.py --artifacts` 强制校验表中必须存在当前 `guiju._version.__version__` 条目且其文件哈希映射与当前包内资产运行时派生结果逐字节一致，离线单测强制校验已发布历史版本（`< __version__`）条目仅追加、不可删改。
- **确定性运行时派生（零手工抄写）**：
  - 完整 `SKILL.md` 由纯函数在运行时将 `skill_frontmatter.yaml` + `contract.md` 原文确定性拼接生成；单测断言剥除 frontmatter 后与 `contract.md` **逐字节一致**；
  - `McpServer.initialize` 的 `instructions` 在运行时按标记直接从 `contract.md` 确定性截取并格式化（不另存 `mcp-instructions.txt` 副本）；单测断言其逐字源于 `contract.md` 对应标记区间；
  - `docs/AGENT-USAGE.md` 改写为**极简指针页**，仅指向 `guiju/agent_assets/contract.md` 与 `guiju agent show --kind contract`，彻底消除第二份规则正文。
- **两种交付形态与宿主边界**：
  1. **标准 Skill 目录（`guiju-standards/SKILL.md`）**：面向支持开放 Agent Skills 的任意 harness，通过 `guiju agent install` 落地；
  2. **工作区指令片段（`guiju agent show --kind contract`）**：面向只读项目指令文件（如工程项目根目录下的 `AGENTS.md`、`CLAUDE.md` 或自定义 system prompt）的消费者，提供自包含使用片段；
  - **绝不越界接管宿主工作区**：`guiju` 仅作为无状态规范取源工具，不整份生成、不接管、不覆写任何消费方项目的 `AGENTS.md`。

### D3 严格分层、带外收据、认领平滑升级与完整写删语义

- **分层落地（B1，对齐 `ARCHITECTURE.md:168/182`）**：
  1. **纯领域层 `guiju/domain/agent_assets.py`**：仅承载纯函数（`SKILL.md` 与 MCP `instructions` 确定性派生、规范化受管相对路径表、历史版本哈希匹配、输入「收据条目 + 磁盘快照/symlink 标志 + 当前/历史哈希」输出确定性动作计划），**零磁盘/环境变量读取、零 `Path.home()`/`cwd`、零网络**；
  2. **存储层 `guiju/store/agent_receipts.py`**：对标 `guiju/store/atlas.py`，承接 `GUIJU_HOME/agent-receipts.json` 的读取、严格 schema 校验、损坏检测与 `.corrupt-<YYYYMMDD-HHMMSS>` 备份、以及 `temp + fsync + os.replace` 原子写入；读-改-写全程持有基于 POSIX `fcntl.flock(..., fcntl.LOCK_EX)` 的同目录文件锁（如 `agent-receipts.lock`），防止并发 `install`/`uninstall` 更新丢失；**不触碰 `catalog.db`**（保持 `SCHEMA_VERSION = 2`）；
  3. **应用层 `guiju/application/agent.py`**：承接参数校验、目录解析（项目根默认为当前工作目录 `cwd`，用户目录为 `Path.home()`）、`lstat` 符号链接递归探测、磁盘快照采集与持锁执行写删/刷新收据。
- **带外收据与纯净目标目录**：
  - **严禁**向目标 skill 目录 `guiju-standards/` 写入 `.manifest.json` 等任何私有元数据文件；目标目录内除开放标准要求的文件（当前仅 `SKILL.md`）外零私有文件；
  - 收据统一带外落盘于 `GUIJU_HOME/agent-receipts.json`。
- **完整状态机（含跨机认领 B3 与团队同步/切分支 R1）**：
  - **首次安装**（目标路径不存在，且先经 `lstat` 排除悬空符号链接）→ 写盘 + 记收据 → `exit 0`, `action="installed"`；
  - **已登记目标干净态判定（先查当前包版本，再查 `{本机收据记录, history_hashes.json 任一历史官方版本}`）**：目标在收据中已登记，且无符号链接、无多余脏文件时，只要磁盘完整文件集合与 SHA-256 精确匹配 `{本机收据记录, 当前安装包版本, history_hashes.json 任一历史官方版本}` 中任意一个即视为干净受管（非用户篡改）：
    - **无变更（匹配当前安装包版本）**：若本机收据已匹配当前版本，零写盘 → `exit 0`, `action="unchanged"`；若本机收据仍记录旧版（如团队成员升级提交后经 `git pull` 同步到本机工作区），则零改目标文件、仅将本机收据刷新为当前版本 → `exit 0`, `action="unchanged"`；
    - **平滑升级（不匹配当前安装包版本，但精确匹配本机收据或 `history_hashes.json` 任一历史官方版本）**：覆盖常规本机升级与 `git checkout` 切回旧版官方分支场景 → 直接原子更新目标文件至当前版本并刷新收据 → `exit 0`, `action="upgraded"`（**不需要 `--force`**）；
  - **跨机认领（B3）**：目标已存在、无符号链接、本机收据无该路径登记，且磁盘完整文件集合与 SHA-256（无多余脏文件）**精确匹配当前版本或 `history_hashes.json` 中任一历史版本**（同样优先匹配当前版本）→
    - 匹配当前版本：零改目标文件，仅补写本机收据 → `exit 0`, `action="adopted"`；
    - 匹配历史版本：平滑更新目标文件至当前版本并补写本机收据 → `exit 0`, `action="adopted_and_upgraded"`（**不需要 `--force`**）；
  - **非强行冲突拒绝**：本机收据无登记且存在未受管文件又不匹配任何官方版本哈希 → `exit 11`（`agent_unmanaged_conflict`）；本机收据已登记，但磁盘不匹配 `{本机收据记录, 当前安装包版本, history_hashes.json 任一历史官方版本}` 中的任何一个或存在多余脏文件（例如用户手工改动，或本机 `guiju` 仍停留在旧版而仓库拉取了本机未知的新版内容）→ `exit 11`（`agent_user_modified`）。
- **四项硬边界语义（B4 + R1 + R4）**：
  1. **`install --force` 清理命名空间脏文件**：在确认目标路径及其内部无符号链接的前提下，`install --force` 先删除 `guiju-standards/` 内全部不属于当前受管清单的额外文件（及随之变空的子目录），再写入纯净受管文件并刷新收据（JSON 附 `removed_extra_files`）。保证任何场景执行 `install --force` 后立即重跑 `install` 必得到 `exit 0` (`action="unchanged"`)；
  2. **`uninstall` 边界（未受管绝不强删，含已删目录清理与团队同步）**：
     - 未受管目标（不在收据中，且不匹配官方当前或历史完整版本哈希，含目标不存在且无收据）：**即使传 `--force` 也绝不删除**，固定拒绝返回 `exit 11`（`agent_not_managed`）；
     - 收据已登记但目标目录已被手工删除：先经 `lstat` 确认目标路径既不存在也不是悬空符号链接（例如用户已先执行 `rm -rf .agents/skills/guiju-standards`）后，直接清理该条陈旧收据并返回 `exit 0`（`action="uninstalled"`，`removed_files=[]`，`preserved_files=[]`）；
     - 干净官方目标（无多余文件，且无收据时磁盘精确匹配当前/历史官方完整哈希，或有收据时磁盘精确匹配 `{本机收据记录, 当前安装包版本, history_hashes.json 任一历史官方版本}` 任一者）：安全删除受管文件与空目录并移除收据（若已登记） → `exit 0`（`action="uninstalled"`）；
     - 已受管但磁盘不匹配 `{本机收据记录, 当前安装包版本, history_hashes.json 任一历史官方版本}` 任一者或含额外文件的目标：不带 `--force` 返回 `exit 11`（`agent_user_modified`）；传 `--force` 时**仅删除收据登记的受管文件**，**保留所有非受管额外文件与非空目录**（仅空目录删除），清理该条收据并在 JSON 返回 `preserved_files`；
  3. **收据损坏处理矩阵**：
     - `status`：严格只读不写盘、不备份，直接报错 `exit 11`（`agent_receipt_corrupt`）并指出收据路径；
     - `install`（含向全新未安装目录安装）：不带 `--force` 一律拒绝返回 `exit 11`（`agent_receipt_corrupt`），绝不静默丢弃其他已有目标的登记；带 `--force` 时先将坏收据原子备份为 `agent-receipts.json.corrupt-<YYYYMMDD-HHMMSS>`，再基于全新干净收据完成安装并在 JSON 返回 `backed_up_receipt`；
     - `uninstall`：存在未损收据时按第 2 条执行；若收据损坏，则一律拒绝直接删改收据（`exit 11`, `agent_receipt_corrupt`）；
  4. **符号链接全面拒绝（含 `--force`）**：写入、读取校验或删除前（且早于路径存在性判定）对 `guiju-standards` 目标目录自身（如指向外部目录的 symlink 或悬空 symlink）及目录内全部文件/子目录执行 `lstat` 检测；**只要出现任何符号链接，即便带 `--force` 也一律拒绝并返回 `exit 11`（`reason="agent_symlink_rejected"`）**，绝不顺着符号链接越界写入或删除命名空间外文件。
- **跨工具命名空间隔离**：`guiju agent` 仅读写自身子目录 `guiju-standards/` 与自有 `GUIJU_HOME/agent-receipts.json`，**绝不扫描、校验或删除同级目录（如 `.agents/skills/`）下的任何其他工具技能或第三方状态文件**。

### D4 CLI 参数契约、JSON 输出形状与 `agent-receipts.json` Schema

> 遵循 `AGENTS.md §1`：涉及 CLI 与 JSON 形状契约先于实现在本 ADR 中定稿。

1. **CLI 子命令与参数规则**：
   - `guiju agent show [--kind {contract,skill}] [--json]`：默认 `--kind contract`；纯 text 输出原文到 `stdout`；`--json` 输出 JSON；只读不建目录不触盘。
   - `guiju agent install [--target {agents,claude} | --dir PATH] [--scope {project,user}] [--force] [--json]`：
     - 默认 `--target agents`（解析至 `<root>/.agents/skills/guiju-standards`）、`--scope project`（`<root>` 固定取当前工作目录 `cwd`；`--scope user` 时 `<root>` 取 `Path.home()`）；`--target claude` 解析至 `<root>/.claude/skills/guiju-standards`；
     - `--dir <PATH>` 指定 skill 父根目录（写入 `<PATH>/guiju-standards`）；**`--dir` 与 `--target` / `--scope` 严格互斥**（命令行显式同传任意组合即返回 `exit 2` `bad_usage`；`--dir` 为空串或仅空白时返回 `exit 2` `bad_usage`）。
   - `guiju agent uninstall [--target {agents,claude} | --dir PATH] [--scope {project,user}] [--force] [--json]`：路径解析与互斥规则同 `install`。
   - `guiju agent status [--target {agents,claude} | --dir PATH] [--scope {project,user}] [--json]`：
     - **不传路径选项**（未传 `--target` / `--scope` / `--dir`）时：读取 `GUIJU_HOME/agent-receipts.json` 列出本机全部受管目标并逐条实时核对磁盘态（无符号链接且无额外文件时：匹配当前安装包版本为 `clean`，匹配本机收据或 `history_hashes.json` 任一历史版本且非当前版为 `upgradable`，均不匹配或含额外文件为 `modified`；目录缺失且非悬空 symlink 为 `missing`；命中 symlink 为 `symlink`）；收据缺失时返回空列表与 `receipt_exists=false`；收据损坏时返回 `exit 11`（`agent_receipt_corrupt`）；
     - **显式传 `--target` / `--scope` 或 `--dir`**（同传 `--dir` 与 `--target`/`--scope` 仍报 `exit 2`）时：仅检查并报告该指定目标路径的状态（包括 `not_installed` / `adoptable_current` / `adoptable_upgrade` / `clean` / `upgradable` / `modified` / `missing` / `unmanaged_conflict` / `symlink`）；
     - 全部 `status` 分支**只读不写盘、不建任何目录**。

2. **成功载荷（`--json`，统一携带 `"ok": true`）**：
   - `show`：`{"ok": true, "kind": "contract" | "skill", "package_version": "...", "sha256": "...", "content": "..."}`
   - `install`：`{"ok": true, "action": "installed" | "unchanged" | "upgraded" | "adopted" | "adopted_and_upgraded", "target": "...", "scope": "project" | "user" | "custom", "path": "<realpath>/guiju-standards", "receipt_path": ".../agent-receipts.json", "package_version": "...", "files": [{"relpath": "SKILL.md", "sha256": "..."}], "removed_extra_files": [...], "backed_up_receipt": null | "..."}`
   - `uninstall`：`{"ok": true, "action": "uninstalled", "path": "<realpath>/guiju-standards", "receipt_path": ".../agent-receipts.json", "removed_files": [...], "preserved_files": [...]}`
   - `status`（汇总或单目标）：`{"ok": true, "mode": "all" | "single", "receipt_path": ".../agent-receipts.json", "receipt_exists": true | false, "package_version": "...", "entries": [{"path": "...", "target": "...", "scope": "...", "receipt_version": "...", "state": "...", "files": [...], "extra_files": [...]}]}`

3. **`GUIJU_HOME/agent-receipts.json` 结构（`schema: 1`）**：
   ```json
   {
     "schema": 1,
     "installs": {
       "/abs/realpath/to/.agents/skills/guiju-standards": {
         "skill": "guiju-standards",
         "target": "agents",
         "scope": "project",
         "package_version": "1.5.0",
         "installed_at": "2026-11-27 14:00:00",
         "files": {
           "SKILL.md": "<64-hex-sha256>"
         }
       }
     }
   }
   ```

### D5 退出码与一致性守卫

- 在 `guiju/errors.py` 的 `1x` 本地文件系统段正式新增 **`EXIT_CONFLICT = 11`（`AgentConflict`）**，纳入 `EXIT_TABLE`，保持既有规范采集业务退出码（`10/20/30–34/40/50`）与 `1` / `2` 语义零变更：

| 退出码 | 错误类 / `reason` | 适用场景 | 既有规范采集退出码影响 |
|---|---|---|---|
| `0` | — | `show` / `status` / `installed` / `unchanged` / `upgraded` / `adopted` / `adopted_and_upgraded` / `uninstalled` | 无 |
| `2` | `BadUsage`（`bad_usage`） | 非法 `--kind`/`--target`/`--scope`、空白 `--dir`、`--dir` 与 `--target`/`--scope` 互斥冲突 | 复用既有 `2` |
| `11` | `AgentConflict`（`agent_unmanaged_conflict` / `agent_user_modified` / `agent_not_managed` / `agent_receipt_corrupt` / `agent_symlink_rejected`） | 目标非受管冲突、用户修改/脏文件、非受管卸载拒绝、收据损坏拒绝、符号链接拒绝 | 新增 `11`（`1x` 本地域）；既有 `10`、`20`、`30–50` 零变动 |
| `1` | `GuijuError`（`internal`） | 仅保留给未预期程序缺陷（带 stderr traceback） | 复用既有 `1` |

- **MCP 边界声明**：MCP 服务面仅暴露只读规范与图集检索工具（`query` / `verify` / `batch` / `announcement` / `atlas_search`），不提供 `agent` 安装/写盘工具，**MCP 服务面永远不会返回退出码 `11`**。
- **退出码表单一来源一致性**：鉴于 `guiju/errors.py` 将 `EXIT_INTERNAL = 1` 置于 `EXIT_TABLE` 之外、且现行 `README.md` 退出码表漏列了已有的 `34`（`no_currency_record`），单测断言精确集合恒等式 **`set(EXIT_TABLE.keys()) | {EXIT_INTERNAL} == codes_in(README.md) == codes_in(contract.md)`**（S1 同步修正 `guiju/errors.py:27` 注释指向，并在 `README.md` 退出码表一并补齐已有遗漏的 `34` 与新增的 `11`）。

### D6 明确不做

- 不做 `pip` / `uv` 安装期静默写盘 hook 或后台写入进程；
- 不绑定单一厂商私有插件格式或发布单平台专有资产；
- 不把本仓开发态 `AGENTS.md`、`.agents/skills/red-team-review/`、源码内部路径/类名放入消费方资产或消费方可见提示；
- 不在 `guiju/domain/` 内直接读写文件或解析 `Path.home()`/`cwd`；
- 不接管、不整份生成或覆写宿主项目根目录 `AGENTS.md`；
- 不向目标 skill 目录写入 `.manifest.json` 等任何私有元数据文件；
- 不触碰同级目录下其他工具的技能或状态文件，不修改 `guiju/net/`，不触碰 NAS（外部边界见 `kb-system/ADR0048`）。

---

## 三、架构不变量与可证伪守卫

本 ADR 仅锁定长期有效的架构不变量与可证伪守卫（具体迭代里程碑、双 Python 矩阵、联网 `e2e.py 39/39` 与 `hk` 部署演练见各版本独立 Spec，首期为 [spec-1.5.0.md](../spec-1.5.0.md)）：

1. **双层净化守卫**：`tests/` 静态检验包数据配置与 `importlib.resources` 可加载性，`scripts/check-release.py --artifacts` 检验真实 wheel 构建物（含 `guiju/agent_assets/`、不含根 `AGENTS.md`/`.agents/`/`docs/`）；单测持续扫描 5 处消费方可见输出，确保零仓内相对死链、零源码内部类名/路径泄露、零开发技能名称泄露。
2. **真正单一真源与版本索引守卫**：仓库内仅 `guiju/agent_assets/contract.md` 承载消费方正文规则；运行时派生出的 `SKILL.md`（剥除 frontmatter）与 `contract.md` 逐字节一致；`McpServer.initialize` `instructions` 运行时由 `contract.md` 标记区间截取；`docs/AGENT-USAGE.md` 不重复正文；`set(EXIT_TABLE.keys()) | {EXIT_INTERNAL} == codes_in(README.md) == codes_in(contract.md)`；`scripts/check-release.py --artifacts` 校验 `history_hashes.json` 包含当前 `__version__` 且哈希等于运行时派生值，单测校验已发布历史条目只增不改。
3. **严格分层与纯净带外收据不变量**：AST/模块测试证明 `guiju/domain/agent_assets.py` 不触文件系统/环境路径；收据持久化与 `fcntl.flock` 全在 `guiju/store/agent_receipts.py`；`GUIJU_HOME/agent-receipts.json` 为唯一收据落点且 `catalog.db` 保持 `SCHEMA_VERSION = 2`；安装后目标 skill 目录内零私有元数据文件。
4. **完整状态机与符号链接安全不变量**：单测持续覆盖首次安装（`0`）、无变更（`0`）、旧版受管平滑升级（`0`）、已登记目标团队 `git pull` 同步当前版免写刷新收据（`unchanged`，`0`）与 `git checkout` 切回旧版平滑恢复（`upgraded`，`0`）、跨机当前/历史哈希认领（`adopted`/`adopted_and_upgraded`，`0`）、未受管冲突/用户修改/收据损坏（`11`）、`--force` 清理多余文件再安装幂等（`0`）、`uninstall` 对未受管目标即使用 `--force` 仍拒绝（`11`）、`uninstall` 对已登记但目录已删目标清理陈旧收据（`0`，`removed_files=[]`）而悬空 symlink 仍拒绝（`11`）、`uninstall --force` 只删受管文件保留额外文件（`0`）、坏收据仅 `--force` 时 `.corrupt-<ts>` 备份重开（`0`）及任意 symlink 拒绝（含 `--force`，`11`）。
5. **零安装期副作用与业务红线守卫**：`pyproject.toml` 与包加载路径零写盘 hook；同级第三方 skill 目录零触碰；MCP 服务面永不返回 `11`；既有业务退出码（`10`/`20`/`30–34`/`40`/`50`）与 `guiju/net/` 出站红线零变更。
