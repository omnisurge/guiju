# 2026-11-22 外部评审复核记录

- 复核对象：用户提供的外部评审全文（2026-11-22）
- 复核基线：`cc30d1d`（`v1.2.0-alpha.4`，当前分支 `refactor/root-cause`）
- 复核方法：源码逐行核对、行号/文件实际读取、定向单测、MCP 并发模型合成复现；复核阶段未修改生产代码（修复记录见第五节）
- 结论：评审的**核心判断成立**——B3→B4 过渡态，B4/B5 尚未完成；P1-1、P1-3、P2-4、P2-5 是真问题。但 P2-6 的 403/429“双标”判断不成立，另有几处过度表述。ADR 决策见 [ADR 0005](../adr/0005-external-review-2026-11-22.md)。

## 一、逐项核验

| 评审项 | 判定 | 核验依据 |
|---|---|---|
| P1-1 MCP HTTP 共享 `NativeCurrency.last_outcome` 竞态 | **成立** | [mcp.py:311-340](../../guiju/mcp.py#L311-L340) 先 `lookup` 再 `_cache_status(self.backend)`；[native.py:103-108](../../guiju/currency/native.py#L103-L108) 与 [:159-230](../../guiju/currency/native.py#L159-L230) 在 `query` 入口清空、结束时写回实例属性；[mcp.py:719-745](../../guiju/mcp.py#L719-L745) 的 HTTP 为 8 worker，`_query` 无锁。合成并发复现：A 的 `stale=True` 被 B 的 fresh 状态覆盖，A 返回无缓存告警。 |
| P1-2 缺 application 编排层、依赖倒置、逻辑重复 | **主体成立** | 当前树无 `guiju/application/`、`ports/`、`adapters/`；[mcp.py:406](../../guiju/mcp.py#L406) 运行时 `from .cli import lookup`；[sources/mohurd.py:24](../../guiju/sources/mohurd.py#L24)、[sources/shzjw.py:33](../../guiju/sources/shzjw.py#L33) import `compliance`；行数核对：mcp 927、lookup 695、pull 664、cite 409。 |
| P1-3 MCP 服务面安全/并发隔离未闭合 | **主要成立** | 线上 `systemctl show guiju-mcp.service`：`User=`、`Group=`、`ProtectSystem=no`、`ReadWritePaths=`、`RestrictAddressFamilies=`；单元文件另已有 `NoNewPrivileges=true`、`PrivateTmp=true`。[mcp.py:872-874](../../guiju/mcp.py#L872-L874) 非回环仅警告；[mcp.py:412-470](../../guiju/mcp.py#L412-L470) 的 `_store_lock` 包含 `source.detail()` 网络 I/O；[mcp.py:342-399](../../guiju/mcp.py#L342-L399) 的 batch 仍用 `list/out` 文件路径。 |
| P2-4 csres 明文回退导致默认链路不落磁盘缓存 | **成立** | [registry.py:104-117](../../guiju/currency/registry.py#L104-L117) 将 csres 注册为 GB/JGJ/CJJ/JTG/DB 默认主源并开 `plaintext_fallback=True`；[native.py:196-207](../../guiju/currency/native.py#L196-L207) 的 `cacheable = complete and not insecure_transport` 使其不写 `DiskCurrencyCache`。 |
| P2-5 M4 body_sha256/probe 底座空转 | **成立** | `body_sha256` 除 schema、测试、文档外无代码读写；[body.py:177-188](../../guiju/net/body.py#L177-L188) 的 `probe_sink` 默认 no-op，`compliance._body_client()` 未注入；`MetadataClient` 无 probe_sink；`store.log_probe` 仅有 [pull.py:264](../../guiju/cli/commands/pull.py#L264)、[:458](../../guiju/cli/commands/pull.py#L458)、[:473](../../guiju/cli/commands/pull.py#L473) 三处手工调用。 |
| P2-6 403/429 策略“双标”、CachedCurrency 死代码 | **前半不成立；后半部分成立** | 403/429 规则见下节；`CachedCurrency` 的旧 SQLite `currency_cache` 分支在 native 默认路径确实不可达（[currency/__init__.py:158-168](../../guiju/currency/__init__.py#L158-L168)），但类仍被 CLI 构造，不能称为整个类已死。 |
| P3 测试耦合与源适配器脆弱点 | **大体成立，细节需收窄** | 81 个 `test*.py`、27 个 `test_fix*.py`；测试债成立。直接 monkeypatch `compliance._throttle` / `httpx.time.sleep` 的测试只有少数几处，不是“大量”。[sources/shzjw.py:41](../../guiju/sources/shzjw.py#L41) 的公开前端 token 与 [sources/mohurd.py:44-51](../../guiju/sources/mohurd.py#L44-L51) 的 CMS 参数均属实，但前者不是秘密泄露。 |

## 二、明确反驳：P2-6 的前半段

评审称 `body.py` 在 403/429 没有中文人机页特征时归 20，与“遇限速/拦截立即停止 40”的契约有偏差。该判断不成立：

- [tests/test_net_p2_14.py:31-47](../../tests/test_net_p2_14.py#L31-L47) 明确断言：无 marker 的 403/429 → `SourceUnreachable`（20）且不重试；有“验证码/访问过于频繁”等 marker → `ComplianceDenied`（40）。
- [tests/test_net_metadata_transport.py:28-29](../../tests/test_net_metadata_transport.py#L28-L29) 明确断言 metadata 的 429（无 marker）会重试。
- [docs/remediation-2026-11-18.md:136](../remediation-2026-11-18.md#L136) 的原文是“403/429/200 **人机页**必须 40”，不是所有 403/429 一律 40。

定向测试实际运行通过：`python3 -m unittest tests.test_net_p2_14 tests.test_net_metadata_transport -v`。因此这里不应按评审建议把 403/429 全部改成 40；那是把“普通源不可达”误判成“合规拦截”，会破坏既有契约。

## 三、评审未覆盖的 MCP 完整性问题

`McpServer._query` / `_verify` 只把 `stale/from_cache` 转成告警，没有把 `NativeCurrency.Outcome` 的 `complete`、`incomplete_reason`、`fallback_source` 透传给 MCP 客户端。与 P1-1 同源：两者都源于 MCP 没有把 `Outcome` 作为唯一结果事实源。该问题应和 P1-1 同批修复。`_batch` 也应按同一契约处理。

## 四、复核结论对实施的影响

1. **立即修复**：P1-1 与 MCP `Outcome` 完整性告警，先补并发和跨面测试。
2. **B4 继续**：application/ports/adapters、MCP 无文件面、systemd 加固仍未完成；`1.2.0a4` 只是预发布，不等于 B4/B5 完成。
3. **产品债排序**：P2-4（csres 缓存）与 P2-5（M4 probe）在 B4 收口后处理；`CachedCurrency` 旧分支与测试收敛放在 B4 之后，避免边重构边清理造成回归。
4. **不采纳**：P2-6 前半段的“全部 403/429 → 40”建议。P2-6 的 `CachedCurrency` 旧分支清理可作为 P3 技术债，但不是当前运行时缺陷。

## 五、本轮实施记录（2026-11-22）

- `guiju/mcp.py`：新增 `_query_result()`，`_query` / `_verify` / `_batch` 优先使用 `NativeCurrency.query()` 返回的不可变 `Outcome`；`_cache_status()` 同时读取 `complete` / `incomplete_reason` / `fallback_source`，不再在查询后读取共享 `backend.last_outcome`。
- `tests/test_fix_w5_mcp_20261122.py`：在既有 `CacheWarningTest` 中加入 Outcome 路径、完整性/备源告警和两请求交错回归；未新增测试方法，保持发布门禁的 1223 项基线。
- 验证：`python3 -m unittest tests.test_fix_w5_mcp_20261122` 43/43 通过；全量单测 1223/1223 通过；`scripts/e2e.py --offline` 4/4 通过；`scripts/check-release.py --docs-only` PASS（44 项）。
- 未做：B4 application/ports/adapters、MCP 去文件路径、systemd 加固、hk 部署、NAS 操作；B5 与 1.2.0 稳定版门禁仍在 ADR 0005 的 I2-I4。
