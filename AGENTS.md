# AGENTS.md —— 给使用本工具的 agent 的操作契约

> 你是被指派去"查规范 / 取正文 / 出引用串"的 agent。读这一份就够，不要猜命令行为。
> 人类用户看 [README.md](README.md)。

---

## 一、三条硬规则

1. **主路径是 `--json`**：stdout 只有 JSON，日志在 stderr。直接解析 stdout，不要去正则人类输出。
2. **全程无交互**：不存在确认提示。需要人决策时，工具用非零退出码 + `candidates` 告诉你"叫人"，**不要自己替人挑**。
3. **退出码就是你的决策表**（见第三节）。特别地：`30/32/33` 都不是故障，`40` 必须立即停止。

---

## 二、你该用哪个命令

```
要先确认环境吗？          → guiju status --json
只想找条目（不联网）吗？   → guiju search <关键词> --json [--all]
   · 书名号可带可不带（《防火》与 防火 等价）
   · 返回 mode=and/fuzzy/chars，total 是总数、count 是本次返回数
   · --limit 默认 20，要"全部"用 --all
想知道某编号是什么/是否现行？ → guiju which <编号> --json   （不需要预先索引）
   · 返回 currency[{code,status,name}] + announcement{id,...}
   · status: current/superseded/upcoming/withdrawn/unknown，unknown ≠ 现行
要正文文件吗？             → guiju pull <编号|规范名|公告ID|URL> --json
要一整批吗？               → guiju pull --list <文件|-> --json
   · 清单每行一个 ref，# 注释与行尾注释都支持
   · 汇总里 counts 按退出码统计，ok=false 表示有 1/20/40/50 这类严重失败
只要引用串吗？             → guiju cite <编号|公告ID|URL> [条款号] --json
怀疑本地文件被改/丢失？     → guiju verify --json
这个 PDF 哪来的？          → guiju provenance <路径> --json
目录为空/过期？            → guiju sync --json            （全量 33 页 ≈60s）
   · 之后增量同步只抓 2 页 ≈3s（连续两页无新增即停）
   · 定时任务用 guiju sync --if-stale 7 --json（新则 0 请求秒退）
   · status.catalog_stale=true 说明目录 >7 天没更新
```

---

## 三、退出码 → 你的动作

| 码 | reason | 你做什么 |
|---|---|---|
| 0 | — | 继续 |
| 10 | — | 已完成，直接用 `path`，**不要再 pull 一次** |
| 20 | `unreachable` | 退避后重试一次；仍失败就报告"源不可达" |
| 30 | `no_attachment` / `bad_attachment` | 读 `hint` 与 `availability`；若是 `pre_window`，这是**正常事实**，不要重试，直接向用户说明 |
| 31 | `not_indexed` | 名称检索无命中，或时效服务也给不出名称 → 先 `guiju sync`；仍无则说明该标准没有公开正文公告（**不要重试**，先 `guiju which` 确认） |
| 32 | `ambiguous_code` | **停下来**，把 `candidates` 原样交给人选。禁止按年份/字母序自行判断哪个是现行版 |
| 33 | `ambiguous_attachment` | **停下来**，把 `candidates`（label + url）交给人选。禁止兜底取第一个 |
| 40 | `compliance` | **立即停止**。不重试、不换 UA、不换代理、不找镜像绕过 |
| 50 | `needs_human` | 上报维护者（多半是站点改版，修 `guiju/sources/mohurd.py`） |
| 2 | `bad_usage` | 输入无法解析（条款号形态错误等）：按 `hint` 改正，别重复同样的输入 |
| 1 | `internal` | 程序缺陷：把 stderr 的 traceback 带上去上报 |

---

### 20 有两种来源，别把"服务挂了"当成"没这本书"

| 码 | 含义 | 你做什么 |
|---|---|---|
| `20` + 错误里带"时效服务" | **时效服务**不可达（目录本身没问题） | **不要** `sync`。改走名称路径：`guiju search <名称>` → `guiju pull <名称>`；或请用户检查 `GUIJU_CURRENCY_URL` |
| `20` + 错误里带 URL/HTTP | 政府站点不可达 | 退避后重试一次 |
| `31` | 服务是通的，但确实没有这个编号 / 名称检索无命中 | 先 `sync`；仍无则说明没有公开正文公告，别反复重试 |

### cite 的额外请求与 confidence

- 按**编号**引用：时效服务 1 次，无额外抓取。
- 按**名称 / 公告ID**引用：自动取 1 次详情页解析编号（`--offline` 可跳过，跳过则只能按编号引用）。
- `confidence`：`parsed` = 编号来自正文解析；`unknown` = 编号来自时效服务的名称映射（同样可用，`--strict` 不因此报错）。**warnings 非空才是必须复核的信号。**

---

## 四、必须知道的事实（否则你会误判）

### 1. 正文可得性按年份分档

- **2014 年及以后**：基本都能取到正文。取不到属异常，值得排查。
- **2014 年之前**：公告通常**只发布名称与编号，不附正文文件**。此时 `pull` 退出 30 是**正确答案**，不是失败。

判定字段：JSON 里的 `availability` = `in_window` / `pre_window` / `unknown`。
所有失败体都带 `availability` 与一句中文 `hint`。**向用户汇报时请把这条事实一起说出来**，否则用户会以为是工具坏了。

### 2. 编号可能不存在，也可能不止一个

约 39% 的公告正文里没有标准编号（如"工作计划""产品标准"类）。此时：
- `code` 为 `null`（`pull` 仍可下载，按名称归档）
- `cite` 会失败（31）—— 这是正确的，不要编一个编号出来

**现行性以 `spec-currency` 的权威结论为准**（`which`/`cite` 自带），不要据年份或页面上"修改为"字样自行判断。
同一公告里出现新旧两个编号是常态（`JGJ 147-2016` 与 `JGJ 147-2004`）。
JSON 的 `codes[].role` 会告诉你身份：

| role | 含义 | 可用于引用 |
|---|---|---|
| `target_of_revision` | 局部修订后改写的新编号 | ✅ |
| `primary` | 本次公告发布的版本 | ✅ |
| `superseded_ref` | 被代替/废止的旧版本 | ❌ 不要引用它 |
| `mentioned` | 提到了但判不出身份 | ⚠️ 需要人确认 |

### 3. 引用串可能带 warnings

`cite --json` 返回 `warnings` 数组（编号未核实、编号有歧义…）。
**warnings 非空时，不得直接写进正式文书**；要么加 `--strict` 让工具硬失败，要么请人复核。

---

## 五、不要做的事

1. 不要并发轰炸：工具内部限速 1.2s/请求，**不要用多进程/多线程并行调用它**。
2. 不要把 `30/32/33` 当故障反复重试 —— 重试不会改变结果，只会白耗请求。
3. 不要自己拼 `cite` 字符串绕过工具：角色判定与条款号规范化都在工具里。
4. 不要修改或绕过 `guiju/compliance.py` 的白名单/限速来"提高效率"。
5. 不要把 `pull` 下来的 PDF 再分发出去（尤其不要上传到公共存储）。

---

## 六、JSON 形状速查

```jsonc
// pull 成功
{"ok":true,"code":"GB 55037-2022","path":"/…/library/…__3754d6a9ccc6.pdf",
 "sha256":"…","bytes":5115350,"magic":"pdf","confidence":"parsed",
 "published_on":"2026-09-23","availability":"in_window","source_page":"…","attachment":"…"}

// cite 成功
{"ok":true,"cite":"《老年人照料设施建筑设计标准》JGJ/T 450-2018 第5.2.1条",
 "code":"JGJ/T 450-2018","code_key":"JGJ/T450-2018","clause":"5.2.1",
 "confidence":"parsed","warnings":[],"url":"…"}

// 失败（任何命令）
{"ok":false,"exit":33,"reason":"ambiguous_attachment","error":"…","hint":"…",
 "candidates":[{"label":"…","url":"…"}]}

// status
{"ok":true,"announcements":3254,"editions":N,"artifacts":M,"synced_at":"…",
 "coverage":{"full_text_since_year":2014,"in_window":1777,"pre_window":1477,"statement":"…"}}
```

---

## 七、最小心智模型

```
公告（政府网页，3254 条）
   └─ 可能包含 1..N 个标准编号（39% 没有）
         └─ 每个编号是一个版本（有 role：新 / 旧 / 修订后）
               └─ 可能有 1 份正文（PDF，sha256 内容寻址）
                     └─ 可用于生成引用串（cite）
```

工具永远不让"非法状态"静默通过：宁可 32/33 让你叫人，也不会抓一个差不多对的文件给你。
