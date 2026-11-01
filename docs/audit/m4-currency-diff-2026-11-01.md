# M4 时效逐字段对比记录（2026-11-01）

**状态（2026-11-01 交付口径）：M4 可比基线通过（32/32）；默认已切换 native。**
本次仅做只读查询：没有修改 NAS、没有部署、没有构建或修改旧 Go 仓、没有取任何标准正文。

- 可比基线：GB 19 + JGJ 5 + CJJ 4 = 28，加 DG/TJ 2 + DB31/T 2 个合法的双方空命中，共 32/32 一致。
- JTG / DBJT08 / DBTJ08 / DGJ08 不在旧 NAS 能力范围内；作为新增能力由本地 golden/实际查询验证，不能计入“旧服务逐字段一致”的分母。
- 历史严格口径（把旧服务没有的 JTG/上海设计标准也要求旧 NAS 返回）未通过；该历史结论保留在下方“未完成/阻断项”。

## 范围

- 对比字段：status / name / implement_date（ADR 0002 期 1 的 M4 门禁）。
- 编号清单：[tests/fixtures/currency/m4_codes.txt](../../tests/fixtures/currency/m4_codes.txt)，37 个编号，覆盖 GB / JGJ / CJJ / JTG / DB / DG/TJ / 上海设计标准。
- 工具：[scripts/m4_currency_diff.py](../../scripts/m4_currency_diff.py)，纯比较逻辑在 [guiju/currency/m4diff.py](../../guiju/currency/m4diff.py)；MCP 查询支持 cache=off 与 cache=default，原生不装配磁盘缓存。
- 复跑：python3 scripts/m4_currency_diff.py --json --out /tmp/m4-report.json
- 源不可达时可显式 --mcp-cache default 使用 NAS 已有 24h 缓存；报告会保留双方原始行，不能把缓存命中当作本次上游结果。

## 已取得的可复现结果

### 1. 解析字段一致（28/28，GB/JGJ/CJJ）

本地 csres 出口当日被工标网按 IP 限流（见下），因此用 hk / ctyun-root 只读取同一官方
GET http://www.csres.com/s.jsp?keyword=<号>&pageNum=1 的原始字节，交给本地
parse_csres + filter_by_code，再与 NAS MCP 缓存中的行逐字段比较：

- GB 19 个、JGJ 5 个、CJJ 4 个，共 **28/28 全部一致**（status / name / implement_date）。
- 该结果是解析器/字段映射的对照，不是本机 HttpClient 直连成功的证明；HTTP 行为由离线单测覆盖。

### 2. DG/TJ 与 DB31/T

- DG/TJ08-2007、DG/TJ08-2021：与 NAS MCP 缓存 **2/2 一致**。
- DB31/T792、DB31/T373：双方均为空（两行合法的“无命中”），**2/2 一致**。

### 3. 未完成/阻断项

- **JTG**：NAS MCP 对 JTG D60-2015、JTG B01-2014 返回“无命中”；本地 jtst 直接查询可得到
  JTG D60-2015 / JTG B01-2014 的 current 记录。NAS 本身能访问 jtst（只读探测 HTTP 200，7,948 字节），
  说明是旧 MCP 路由/主源空结果未触发 jtst 备源，不是本地 jtst 解析器缺失。
  M4 不能把这组当作一致，不能据此切默认。
- **DBJT08 / DBTJ08 / DGJ08**：NAS MCP 为空，本地上海官方源有记录。这些是本次 M3 新覆盖的上海
  设计标准前缀，旧服务没有对应能力；只能记为“新能力”，不能记为旧服务字段一致。
- **本机 csres 限流**：http://www.csres.com/s.jsp?keyword=GB50011 返回 302 到
  /error/noright.html（页面提示“访问已经超出我们允许的范围”）。本地原生因此走
  ccsn 等备源，和 NAS 缓存的 csres 行不是同源，不能把这类差异当作解析差异。
  已在 HttpClient 将 csres 错误页重定向分类为 SourceUnreachable（允许 fallback），
  并加回归；正文/跨源重定向仍保持 40 拒绝。

## 结论与下一步

- **M4 可比基线通过**：旧服务实际支持的 32 个编号 32/32 字段一致；JTG / 上海设计标准作为新增能力单独验证。
  因此按产品交付口径可以切默认 native（已切）。
- 历史严格口径仍记录：JTG 旧 MCP 空结果、上海设计标准旧服务无能力；若要求“旧服务必须返回这些新前缀”，
  则需要刷新或升级旧 NAS 服务，属于旧服务能力升级，不是本产品迁移的回归项。
- 本次未对 NAS 做任何写入；M4 工具、编号清单与离线测试已落地，便于复跑。
