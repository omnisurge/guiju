# ADR 0008 · 开发态与使用态 Agent 约束/技能解耦及跨 Harness 分发

- 状态：**提议中（待所有者审阅确认后启动 S1）**（2026-11-27 修订）
- 决策人：所有者
- 触发：`v1.4.0` 收口时已将仓库根 `AGENTS.md`（开发者/发版约束）与 [AGENT-USAGE.md](../AGENT-USAGE.md)（消费方使用契约）拆分，并在本仓 `.agents/skills/red-team-review/SKILL.md` 沉淀了开发审查技能；但下游消费者安装 `guiju` 后，仍缺少随产品包分发、通用于任意 AI agent / harness 且内容彻底净化的产品态使用契约与可移植 Agent Skills。
- 影响：`pyproject.toml`、`guiju/errors.py`、`guiju/mcp.py`、`guiju/cli/__init__.py`、新增 `guiju/agent_assets/`、新增 `guiju/domain/agent_assets.py`、新增 `guiju/application/agent.py`、新增 `guiju/cli/commands/agent.py`、[AGENT-USAGE.md](../AGENT-USAGE.md)、`tests/`
- 关系：延续 [ADR 0004](0004-contract-first-hardening.md)（契约优先）、[ADR 0006](0006-v1-3-0-architecture-and-mcp-purification.md)（架构分层与 MCP 纯净化）及 [ADR 0007](0007-atlas-currency-and-catalog.md)（PEP 639 打包治理）；具体迭代排期与发版门禁见对应版本 Spec（首期为 [spec-1.5.0.md](../spec-1.5.0.md)）。

---

## 一、背景与已核实的六类问题

1. **职责混杂与内容级泄露**：开发约束关心源码分层、`guiju/net/` AST 边界、`scripts/ci.sh` 与发版审查；消费方 agent 关心 `--json` 协议、退出码动作决策、`which/pull/cite/atlas` 选用与 MCP 工具边界。仅做“文件路径排除”不够——现行 [AGENT-USAGE.md](../AGENT-USAGE.md) 正文中已存在三处开发态/仓内泄露，若直接打包或输出给离仓消费者会构成死链或越层泄露：
   - 顶部保留仓内相对链接 `../README.md` 与 `../AGENTS.md`（`docs/AGENT-USAGE.md:4`）；
   - 退出码 `50` 描述直接点名修改源码文件 `guiju/sources/mohurd.py`（`docs/AGENT-USAGE.md:73`）；
   - “不要做的事”列出源码内部模块与类 `guiju/net/（BodyPolicy / MetadataPolicy / RedirectGuard / Throttle）`（`docs/AGENT-USAGE.md:158`），消费方只调 CLI/MCP 根本无法也不需要理解源码内部类名。
2. **多头副本破坏单一真源**：若同时在 `docs/AGENT-USAGE.md`、`guiju/agent_assets/` 与 `guiju/mcp.py` 维护三份使用契约再靠测试做关键词对账，必然演化为双写/三写漂移。
3. **退出码语义错乱风险**：既有 `EXIT_TABLE` 中，`1` 专指未捕获程序缺陷并附 stderr traceback，`10` 专指正文/文件已存在（幂等成功），`30–50` 属规范检索与采集业务域。强行复用业务退出码去表达“目标目录冲突 / 用户手工修改 / 非受管卸载拒绝”，会让消费方 agent 无法区分业务态与本地资产状态。
4. **消费方两种接入形态未切清**：支持开放 Agent Skills 的 harness 需要标准 skill 目录（`.agents/skills/` 等），而只认工作区指令文件的消费方工程（如具体监理项目工作区）需要一份自包含的使用契约片段嵌入自身的 `AGENTS.md` / `CLAUDE.md` / system prompt，同时 `guiju` 不得越界接管宿主项目整份 `AGENTS.md`。
5. **受管状态存放、平滑升级与同级空间边界**：在目标 skill 目录内塞入 `.manifest.json` 等私有文件可能被部分 harness 当作异常或脏文件；若不定义平滑升级，产品升级时即使目标未改也会逼用户盲目使用 `--force`，架空篡改保护；此外同级 `.agents/skills/` 下可能共存其他工具管理的技能或状态，必须严守自身命名空间。
6. **ADR 与单次发版绑定过紧**：ADR 应锁定长期架构边界与可证伪守卫，具体版本号、测试计数、联网 `e2e.py` 与 `hk` 演练属于各版本 Spec / 发版验收文档，不应写进 ADR 关闭条件。

---

## 二、决策

### D1 双层净化隔离（路径级 + 内容级）

- **路径级隔离（包产物边界）**：仓库根 `AGENTS.md`、`.agents/skills/red-team-review/SKILL.md`、`CONTRIBUTING.md`、`docs/`、`scripts/`、`tests/` 属开发与治理资产，**严禁打入 wheel**。
- **内容级词法与链接纯净守卫**：所有包内消费方资产（`guiju/agent_assets/`）及运行时通过 CLI / MCP 输出的使用说明文本，必须通过静态内容级扫描，**严禁包含**：
  - 仓内相对路径或相对 Markdown 链接（如 `../`、`./docs/`）；
  - 源码内部模块路径、内部类名、测试或脚本名（如 `guiju/net/`、`guiju/sources/`、`BodyPolicy`、`MetadataPolicy`、`RedirectGuard`、`Throttle`、`scripts/`、`test_*.py`、`pyproject.toml` 等）；
  - 开发态约束与开发技能标识（如 `red-team-review`、本仓开发 `AGENTS.md`、发版检查、`git push` 等）。
  - 对应消费方语义须改写为纯外接口表述（如退出码 `50` 指导“带错误输出上报维护者”，合规红线指导“工具仅采自官方站点并强制限速，不提供绕过参数”）。

### D2 通用跨 Agent 两种交付形态与边界（方法 2）

- 采用开放通用的纯文本 Markdown 与开放 Agent Skills（`SKILL.md` + YAML frontmatter）结构，**零第三方依赖**，不绑定任何单一 IDE 私有扩展格式或插件市场。
- 包内提供两种标准交付形态：
  1. **完整可移植 Skill 目录**：`guiju-standards`（`SKILL.md`），覆盖 `which → pull → cite → atlas` 标准闭环、`--json`、退出码动作表、`warnings`/strict 与图集版权边界；供任意支持 Agent Skills 约定的 harness 通过 `guiju agent install` 落地；
  2. **消费方工作区指令片段**：独立自包含文本 `contract.md`，供消费方项目（如具体工程/监理工作区）按需通过 `guiju agent show --kind contract` 复制或拼接到自身的 `AGENTS.md`、`CLAUDE.md` 或自定义 system prompt 中。
- **宿主工作区边界**：`guiju` 作为无状态规范取源工具，**不接管也不整份生成或修改消费方宿主项目的 `AGENTS.md`**；宿主项目的语言风格、工程资料纪律与业务审批流程归消费方工程自身。

### D3 显式 CLI 分发、带外收据与四态平滑升级状态机

- **零安装期副作用**：严禁在 `pip install` / `uv tool install` 期间用 hook 静默写入 `~/.agents/`、`~/.claude/` 或工作目录；所有磁盘写入仅由用户显式执行 `guiju agent install` / `uninstall` 触发（`show` 与 `status` 严格只读、不建目录）。
- **多目标与任意目录支持**：
  - `guiju agent show --kind contract|skill [--json]` 直接打印消费方契约片段或 `SKILL.md` 原文，供任何自研 harness、项目工作区或人工检视使用；
  - `guiju agent install` 支持 `--target agents`（`.agents/skills/`）、`--target claude`（`.claude/skills/`）、`--dir <PATH>` 任意自定义目标根目录、以及 `--scope project|user`、`--force`、`--json`。
- **带外受管收据（保持目标目录 100% 干净）**：
  - **禁止在目标 skill 目录内写入 `.manifest.json` 等任何私有元数据文件**；
  - 受管收据统一带外存放于 `GUIJU_HOME/agent-receipts.json`（与 `atlas-catalog.json` 同原则独立于 `catalog.db`，保持 `SCHEMA_VERSION = 2` 不变，采用临时文件 + `fsync` + 原子替换写入），以规范化绝对路径（`realpath`）为键记录 `package_version`、`managed_files` 相对路径及各自 SHA-256。
- **四态状态机（含无感知平滑升级）**：
  - **首次安装**：目标不存在 → 写入受管文件并记入收据 → `exit 0`（`action="installed"`）；
  - **无变更**：目标受管，磁盘文件清单与各自 SHA-256 既匹配收据又匹配当前安装包资产 → 零写盘 → `exit 0`（`action="unchanged"`）；
  - **平滑升级**：目标受管，且磁盘当前文件清单与各自 SHA-256 **完全匹配旧版收据记录**（用户未手工编辑，且目标目录无收据之外的多余脏文件），而当前安装包资产已更新 → 直接原子更新并刷新收据 → `exit 0`（`action="upgraded"`，**不需要 `--force`**）；
  - **冲突与篡改拒绝**：以下情形默认拒绝覆盖并保留现场不变，返回 `EXIT_CONFLICT = 11`（仅显式传 `--force` 才接管/覆盖）：
    - 目标已存在但无对应受管收据 → `exit 11`（`reason="agent_unmanaged_conflict"`）；
    - 目标已受管但存在内容被手工修改或收据之外多余文件 → `exit 11`（`reason="agent_user_modified"`）；
    - 收据文件损坏无法校验完整状态 → `exit 11`（`reason="agent_receipt_corrupt"`）。
- **安全卸载与跨工具命名空间隔离**：
  - `guiju agent uninstall` 仅在目标受管且未修改（或显式 `--force`）时删除收据登记的受管文件与随之变空的 `guiju-standards` 目录并清理该条收据；未受管目标拒绝删除（`exit 11`, `reason="agent_not_managed"`），手工修改未带 `--force` 同样拒绝（`exit 11`, `reason="agent_user_modified"`）；
  - `guiju agent` 严格限定仅读写自身技能子目录 `guiju-standards/` 与自有 `agent-receipts.json`，**绝不扫描、修改或清理同级目录下其他工具的技能或第三方状态文件**。

### D4 真正单一真源（SSOT，消灭双写与多写）

- **`guiju/agent_assets/` 为消费方资产唯一真源**：
  - 目录存放规范化单一真源（`contract.md`、`skills/guiju-standards/SKILL.md` 及基于同源消费方契约提炼的 `mcp-instructions.txt`），通过 `pyproject.toml` `[tool.setuptools.package-data]` 随包分发，运行时由 `importlib.resources` 读取（兼容 editable 与 wheel 安装）；
  - 禁止在测试、CLI、MCP 代码内手工复制第二份长文案再靠关键字匹配对账。
- **MCP 运行时直读真源**：`guiju/mcp.py` 在 `initialize` 响应中直接读取包内 `agent_assets` 提供的 `instructions` 文本，不手写冗余副本。
- **`docs/AGENT-USAGE.md` 改为指针入口页**：仓储文档 `docs/AGENT-USAGE.md` 不再复制大段规则正文，只作为极简索引指针，明确指出真源位于 `guiju/agent_assets/contract.md` 及离仓消费者可通过 `guiju agent show --kind contract` 获取最新自包含使用契约。

### D5 明确独立退出码与机器可读 `reason` 契约

- 既不复用 `1`（在 `guiju` 中保留给未捕获程序缺陷 + traceback），也不复用 `10`（规范正文幂等跳过“文件已存在可直接用”，语义完全相反），更不将规范采集域 `30–50` 错配给包内资产安装冲突。
- 在 `guiju/errors.py` 的 `1x` 本地文件系统域新增 **`EXIT_CONFLICT = 11`（`AgentConflict`）**，同步列入 `EXIT_TABLE` 与统一 JSON 失败体（`{"ok": false, "exit": 11, "reason": ..., "error": ..., "hint": ...}`）：

| 退出码 | 错误类 / `reason` | 适用场景 | 既有规范采集退出码影响 |
|---|---|---|---|
| `0` | — | `show` / `status` / 首次安装 / 无变更 / 平滑升级 / 合法卸载成功 | 无 |
| `2` | `BadUsage`（`bad_usage`） | 子命令/target/scope 参数非法或互冲突 | 复用既有 `2` |
| `11` | `AgentConflict`（`agent_unmanaged_conflict` / `agent_user_modified` / `agent_not_managed` / `agent_receipt_corrupt`） | 目标非受管、用户手工改动或含脏文件、非受管卸载、收据损坏拒绝写删 | 新增 `11`，既有 `10/20/30–34/40/50` 语义零变更 |
| `1` | `GuijuError`（`internal`） | 仅保留给未预期程序缺陷（带 stderr traceback） | 复用既有 `1` |

- 继续严守运行时零第三方依赖、Python 3.10+ POSIX-only、`catalog.db` `SCHEMA_VERSION = 2` 不变、`guiju/net/` 一字不动、不触碰 NAS（ADR0048）。

### D6 明确不做

- 不做 `pip` / `uv` 安装期静默写盘 hook 或后台常驻写入服务；
- 不绑定单一厂商私有插件格式或发布单平台专有资产；
- 不把本仓 `AGENTS.md` 开发红线、`red-team-review` 技能或源码内部细节打入消费方安装包；
- 不接管、不整份生成或覆写宿主工作区根 `AGENTS.md`；
- 不向目标 skill 目录写入 `.manifest.json` 等任何私有元数据文件；
- 不触碰或扫描同级 `.agents/skills/` 下任何其他工具的技能或状态文件。

---

## 三、架构不变量与可证伪守卫

本 ADR 仅锁定长期有效的架构不变量与自动化测试守卫（具体批次里程碑、双 Python 矩阵、联网 `e2e.py 39/39` 与 `hk` 部署演练见各版本独立 Spec，首期为 [spec-1.5.0.md](../spec-1.5.0.md)）：

1. **双层净化守卫**：构建物与 AST/内容测试必须持续证明——wheel 包内 **含 `guiju/agent_assets/`**、**不含根 `AGENTS.md` 与 `.agents/`**；且包内全部消费方资产文本及 MCP 导出引导文本 **零仓内相对死链（`../`）、零源码内部路径/类名、零开发态技能名称**。
2. **单一真源守卫**：仓库内不得存在 `docs/AGENT-USAGE.md`、`mcp.py` 与 `guiju/agent_assets/` 的多份并行正文副本；`McpServer.initialize` `instructions` 须由包内同一资源路径加载。
3. **目标目录纯净与带外收据不变量**：`guiju agent install` 写入后，目标 skill 目录内除标准资产文件外**零私有元数据文件**；收据仅存于 `GUIJU_HOME/agent-receipts.json`，且 `catalog.db` `SCHEMA_VERSION = 2` 保持不升版本。
4. **四态状态机与退出码 `11` 不变量**：单测须持续覆盖首次安装（`0`）、无变更（`0`）、旧版受管匹配平滑升级（`0`，无需 `--force`）、非受管冲突/用户修改/收据损坏（`11`）、带 `--force` 接管与安全卸载（`0` / 非受管 `11`），且同级第三方 skill 目录零触碰、`show`/`status` 只读零建目录。
5. **零安装期副作用**：`pyproject.toml` 与包加载路径不得包含任何安装期写宿主/项目目录逻辑，既有业务命令 `10/20/30–34/40/50` 契约零破坏。
