# 原生时效离线输入与上海官方字段投影

此处只有查询页元数据，不含标准正文文件。八份 Go golden 全部来自旧仓
`internal/adapter/testdata/`，按原始字节复制，不解码重存；[manifest](manifest.json)
锁定八份文件的长度与 SHA-256。另有一份上海官方字段投影，仅用于钉住实际字段类型，
不属于 Go golden、不写入 manifest。回归不依赖旧仓存在，也不请求任何上游。

| 原始输入 | 对应语义回归 |
|---|---|
| [csres_gb50011.gbk](csres_gb50011.gbk) | [csres 六字段金标准](../../test_currency_csres.py) |
| [ccsn_gb50011.json](ccsn_gb50011.json)、[ccsn_jgj147.json](ccsn_jgj147.json) | [ccsn 明确期望](../../test_currency_ccsn.py) |
| [stdsamr_gbt19001.json](stdsamr_gbt19001.json) | [SAMR 明确期望](../../test_currency_stdsamr.py)，标准名称中的编号不是该行标准号 |
| [jtst_jtgd60.html](jtst_jtgd60.html)、[jtst_empty.html](jtst_empty.html) | [jtst 明确期望](../../test_currency_jtst.py)，保留 Go 的嵌套布局行重复口径 |
| [dbba_db31t792.json](dbba_db31t792.json)、[dbba_db31_373.json](dbba_db31_373.json) | [dbba 明确期望](../../test_currency_dbba.py)，毫秒日期固定 +08:00 |
| [shzjw_current_projection.json](shzjw_current_projection.json) | [上海官方字段投影](../../test_currency_shzjw.py)：`standardType` 为字符串 "0"、未来实施日期仍沿源站 current |

其他源的逐字段期望也均人工照录原 Go 测试/真实响应，不由新解析器生成。
`shzjw_current_projection.json` 是 2026-10-31 固定官方 list 端点的单行精简投影
（原响应 1,219 字节中的已用字段），不是完整响应、不是新解析器生成，也不含 PDF 或下载令牌。

- `csres_gb50011.gbk`：从合并前 spec-currency 仓的
  `internal/adapter/testdata/csres_gb50011.gbk` **按原始字节复制**。
  10,039 字节，264 个 CRLF；SHA-256：
  `142c161609a8181247e11a2b347ac291d6729572ce9251deb77c1d0c13a6b039`。
- `csres_gb50011.gbk` 的语义期望人工照录旧仓
  `internal/adapter/golden_test.go::TestCsres_GoldenGB50011` 的两个有序结果、六个字段，
  写在 `tests/test_currency_csres.py`；不是用新的 Python 解析器生成的自证期望。
- 根目录 [.gitattributes](../../../.gitattributes) 对 `.gbk` / `.json` / `.html` 使用 `-text`，避免 checkout 时自动转换换行。

## 必须保留的原站异常

两行 title 都重复使用“发布日期：”，第二次实际写的是实施日期。
解析取第一个发布日期，实施日期只取直接 `td` 的第 4 列；不要“修正”夹具。
属性中的 `&#xA;` 被 HTML 解析器转为真实换行，原始 title 保留在 `Record.raw`，
不得让后续字段吞进 `publish_date`。

## 离线复跑

```bash
python3 -m unittest tests.test_currency_golden tests.test_currency_csres tests.test_currency_ccsn tests.test_currency_stdsamr tests.test_currency_jtst tests.test_currency_dbba
python3 -m unittest tests.test_shzjw_core tests.test_shzjw tests.test_currency_shzjw
python3 -m unittest tests.test_currency_registry tests.test_currency_fallback tests.test_currency_native tests.test_currency_disk_cache tests.test_currency_native_integration tests.test_currency_httpx tests.test_currency_boundary
```

不依赖旧仓或 NAS，不请求上游；HTTP 测试使用内存响应以及真实标准库
`HTTPResponse` / `urllib` opener 配内存 transport，避免 mock 掩盖截断和重定向行为。
非法 GBK 采用显式失败（exit 50），不以乱码或空数组冒充无命中；
该边缘策略是新增 fail-closed 约束，不声称与 Go 解码器对所有非法字节完全等价。
