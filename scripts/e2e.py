#!/usr/bin/env python3
"""端到端验收：把「产品是否跑通」变成可重复执行的断言。

用法：
    python3 scripts/e2e.py            # 全流程（联网；含目录全量对账 + 一次正文下载）
    python3 scripts/e2e.py --offline  # 只跑不联网的部分
    python3 scripts/e2e.py --home DIR # 复用已有数据目录

退出码：0 全部通过；1 有失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[str, bool, str]] = []


def guiju(*args: str, home: str, timeout: int = 300) -> tuple[int, dict, str]:
    env = dict(os.environ, GUIJU_HOME=home)
    proc = subprocess.run(
        [sys.executable, "-m", "guiju", *args, "--json"],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT), env=env,
    )
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"_nonjson": proc.stdout[:200]}
    return proc.returncode, payload, proc.stderr


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="跳过联网步骤")
    parser.add_argument("--home", default="", help="复用已有数据目录")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()

    home = args.home or tempfile.mkdtemp(prefix="guiju-e2e-")
    print(f"数据目录：{home}\n")

    try:
        # ---- 1. 空库可用 ----
        code, p, _ = guiju("status", home=home)
        check("status 空库可用", code == 0 and p.get("announcements") == 0 and p.get("schema_version") == 2)
        check("status 报出时效适配器", bool(p.get("currency_source")), str(p.get("currency_source")))
        code, p, _ = guiju("version", home=home)
        check("version 带 ok 字段", code == 0 and p.get("ok") is True and bool(p.get("version")))
        if args.offline:
            code, p, _ = guiju("search", "防火", home=home)
            check("search 空库不崩", code == 0 and p.get("total") == 0)
            return _summary()

        # ---- 2. 目录同步（全量对账 + 幂等） ----
        code, p, _ = guiju("sync", home=home, timeout=900)
        check("sync 落库 3254 条", p.get("total_in_db") == 3254, str(p.get("total_in_db")))
        check("sync 与上游对账", p.get("total_in_db") == p.get("total_reported"),
              f"db={p.get('total_in_db')} 上游={p.get('total_reported')}")
        code, p, _ = guiju("sync", home=home, timeout=900)
        check("sync 二次幂等（0 新增）", code == 0 and p.get("new") == 0, f"new={p.get('new')}")

        # ---- 3. 名称检索 ----
        code, p, _ = guiju("search", "《防火》", "--all", home=home)
        check("search 书名号可用", code == 0 and p.get("total", 0) >= 40, f"total={p.get('total')}")
        code, p, _ = guiju("search", "防火规范", home=home)
        check("search 模糊补召回", p.get("mode") in ("and+fuzzy", "fuzzy"), str(p.get("mode")))
        code, p, _ = guiju("search", "量子", home=home)
        check("search 无命中 → 0 且不报错", code == 0 and p.get("total") == 0, f"total={p.get('total')}")

        # ---- 4. 编号 → 权威状态 → 本地公告（零索引） ----
        code, p, _ = guiju("which", "GB55037-2022", home=home, timeout=180)
        check("which 权威状态 current", bool(p.get("currency")) and p["currency"][0]["status"] == "current")
        check("which 定位本地公告", bool(p.get("announcement")), str((p.get("announcement") or {}).get("id")))
        check("which 标注可得性 in_window",
              (p.get("announcement") or {}).get("availability") == "in_window")

        code, p, _ = guiju("which", "JGJ 147-2004", home=home, timeout=180)
        check("which 旧版 → superseded", bool(p.get("currency")) and p["currency"][0]["status"] == "superseded")

        # ---- 5. 按编号下载正文（本地未索引，靠权威名称 + 实施日窗口定位） ----
        code, p, _ = guiju("pull", "JGJ/T450-2018", home=home, timeout=600)
        path = p.get("path", "") or ""
        check("pull 按编号成功", code == 0 and p.get("magic") == "pdf" and (p.get("bytes") or 0) > 0,
              f"{p.get('bytes')}B code={p.get('code')}")
        check("pull 内容寻址命名", "__" in Path(path).name and Path(path).suffix == ".pdf", Path(path).name[:48])
        check("pull 用实施日窗口选中原版公告（2018）",
              "2018" in str(p.get("source_page", "")), p.get("source_page", "")[-28:])

        code, p2, _ = guiju("pull", "JGJ/T450-2018", home=home, timeout=600)
        check("pull 幂等返回 10", code == 10 and p2.get("already") is True)

        # ---- 6. 诚实歧义：同名多公告不替用户猜 ----
        code, p, _ = guiju("pull", "老年人照料设施建筑设计标准", home=home, timeout=180)
        check("同名多公告 → 32 + candidates", code == 32 and len(p.get("candidates") or []) >= 2,
              f"exit={code} candidates={len(p.get('candidates') or [])}")

        # ---- 7. 校验与溯源 ----
        code, p, _ = guiju("verify", home=home)
        check("verify 通过", code == 0 and p.get("ok") is True and p.get("total", 0) >= 1, f"total={p.get('total')}")

        code, p, _ = guiju("provenance", path, home=home)
        url = str((p.get("announcement") or {}).get("url", ""))
        check("provenance 反查来源", code == 0 and url.startswith("https://www.mohurd.gov.cn"), url[-24:])

        moved = str(Path(path).with_name("renamed-for-test.pdf"))
        shutil.copy2(path, moved)
        code, p, _ = guiju("provenance", moved, home=home)
        check("provenance 与路径无关（按 sha256）", code == 0 and bool(p.get("sha256")))
        Path(moved).unlink(missing_ok=True)

        # ---- 8. 引用串（三种入口 + 严格模式） ----
        code, p, _ = guiju("cite", "JGJ/T450-2018", "5.2.1", home=home, timeout=180)
        check("cite 按编号", code == 0 and "第5.2.1条" in (p.get("cite") or ""), (p.get("cite") or "")[:40])
        check("cite 带权威状态", p.get("status") in ("current", "unknown"), str(p.get("status")))

        code, p, _ = guiju("cite", str((p.get("code_key") and "") or ""), "1.0.1", home=home) if False else (0, {}, "")
        code, p, _ = guiju("cite", "GB55037-2022", "5.2.1", home=home, timeout=180)
        check("cite 按编号（另一本）", code == 0 and "GB 55037-2022" in (p.get("cite") or ""),
              (p.get("cite") or "")[:40])

        code, p, _ = guiju("cite", "JGJ 147-2004", "1.0.1", "--strict", home=home, timeout=180)
        check("cite --strict 拒绝被替代版本", code == 32 and not p.get("ok"),
              f"exit={code} status={p.get('status')}")

        code, p, _ = guiju("cite", "老年人照料设施建筑设计标准", "5.2.1", home=home, timeout=180)
        check("cite 同名多公告 → 32", code == 32, p.get("reason", ""))

        # ---- 9. 退出码契约 ----
        code, p, _ = guiju("pull", "GB99999-2099", home=home, timeout=180)
        check("未知编号 → 31", code == 31 and p.get("reason") == "not_indexed", p.get("reason", ""))
        code, p, _ = guiju("provenance", "/tmp/definitely-missing.pdf", home=home)
        check("缺文件 → 30", code == 30, p.get("reason", ""))
        code, p, _ = guiju("cite", "GB55037-2022", "not-a-clause", home=home)
        check("非法条款号 → 2", code == 2 and p.get("reason") == "bad_usage", p.get("reason", ""))

        # ---- 10. 输出契约 ----
        env = dict(os.environ, GUIJU_HOME=home)
        proc = subprocess.run([sys.executable, "-m", "guiju", "status", "--json"],
                              capture_output=True, text=True, cwd=str(ROOT), env=env)
        check("--json 时 stdout 只有 JSON", proc.stdout.strip().startswith("{"), proc.stdout[:20])
        check("--json 时人读输出不混入 stdout", "库：" not in proc.stdout)
    finally:
        if not args.keep and not args.home:
            shutil.rmtree(home, ignore_errors=True)

    return _summary()


def _summary() -> int:
    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"合计 {total} 项，通过 {total - len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  · FAIL {name} {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
