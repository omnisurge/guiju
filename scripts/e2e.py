#!/usr/bin/env python3
"""端到端验收：把「产品是否跑通」变成可重复执行的断言。

两种形态：
  · 有时效服务（默认 / 本地）      → 全量断言
  · 无时效服务（异地 runner/断网）  → 跳过编号定位类断言，按公告ID跑本地路径（汇总会写明跳过项）
    `--require-currency` 把"没有时效服务"升级为失败（发版门用）。

用法：
    python3 scripts/e2e.py                  # 全流程（联网；含正文下载）
    python3 scripts/e2e.py --offline        # 只跑不联网的部分
    python3 scripts/e2e.py --require-currency
    python3 scripts/e2e.py --home DIR --keep
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
SKIPPED: list[tuple[str, str]] = []


def guiju(*args: str, home: str, timeout: int = 300) -> tuple[int, dict, str]:
    env = dict(os.environ, GUIJU_HOME=home, GUIJU_NO_AUTO_SYNC="1")
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


def detail_of(code: int, p: dict) -> str:
    """失败时把退出码/原因/错误文本带出来 —— 否则 CI 里只看到 None，没法判读。"""
    if p.get("ok") is False or code != 0:
        return f"exit={code} {p.get('reason', '')} {str(p.get('error', ''))[:70]}".strip()
    return ""


def skip(name: str, why: str) -> None:
    SKIPPED.append((name, why))
    print(f"  [SKIP] {name} —— {why}")


def currency_available(home: str) -> tuple[bool, str]:
    code, p, _ = guiju("which", "GB55037-2022", home=home, timeout=180)
    if code == 0 and p.get("currency"):
        return True, f"来源 {p.get('currency_source')}"
    return False, (p.get("error") or f"exit={code}")[:70]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true", help="跳过联网步骤")
    parser.add_argument("--home", default="", help="复用已有数据目录")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--require-currency", action="store_true", help="没有时效服务即视为失败")
    args = parser.parse_args()

    home = args.home or tempfile.mkdtemp(prefix="guiju-e2e-")
    print(f"数据目录：{home}\n")

    try:
        code, p, _ = guiju("status", home=home)
        check("status 空库可用", code == 0 and p.get("announcements") == 0 and p.get("schema_version") == 2)
        check("status 报出时效适配器", bool(p.get("currency_source")), str(p.get("currency_source")))
        code, p, _ = guiju("version", home=home)
        check("version 带 ok 字段", code == 0 and p.get("ok") is True and bool(p.get("version")))
        if args.offline:
            code, p, _ = guiju("search", "防火", home=home)
            check("search 空库不崩", code == 0 and p.get("total") == 0)
            return _summary()

        code, p, _ = guiju("sync", home=home, timeout=900)
        check("sync 落库 3254 条", p.get("total_in_db") == 3254,
              detail_of(code, p) or str(p.get("total_in_db")))
        check("sync 与上游对账", p.get("total_in_db") == p.get("total_reported"),
              f"db={p.get('total_in_db')} 上游={p.get('total_reported')}")
        code, p, _ = guiju("sync", home=home, timeout=900)
        check("sync 二次幂等（0 新增）", code == 0 and p.get("new") == 0, f"new={p.get('new')}")

        code, p, _ = guiju("search", "《防火》", "--all", home=home)
        check("search 书名号可用", code == 0 and p.get("total", 0) >= 40, f"total={p.get('total')}")
        code, p, _ = guiju("search", "防火规范", home=home)
        check("search 模糊补召回", p.get("mode") in ("and+fuzzy", "fuzzy"), str(p.get("mode")))
        code, p, _ = guiju("search", "量子", home=home)
        check("search 无命中 → 0 且不报错", code == 0 and p.get("total") == 0, f"total={p.get('total')}")

        cur_ok, cur_why = currency_available(home)
        if cur_ok:
            check("时效服务可用", True, cur_why)
        elif args.require_currency:
            check("时效服务可用（--require-currency）", False, cur_why)
            return _summary()
        else:
            skip("编号定位与按编号取正文", cur_why)

        if cur_ok:
            code, p, _ = guiju("which", "GB55037-2022", home=home, timeout=180)
            check("which 权威状态 current", bool(p.get("currency")) and p["currency"][0]["status"] == "current")
            check("which 定位本地公告", bool(p.get("announcement")), str((p.get("announcement") or {}).get("id")))
            check("which 标注可得性 in_window",
                  (p.get("announcement") or {}).get("availability") == "in_window")

            code, p, _ = guiju("which", "JGJ 147-2004", home=home, timeout=180)
            check("which 旧版 → superseded",
                  bool(p.get("currency")) and p["currency"][0]["status"] == "superseded")

            code, p, _ = guiju("which", "GB 50016-2014", home=home, timeout=240)
            nm = (p.get("announcement") or {}).get("name") or ""
            check("被代替引用不误导定位（GB 50016）", "建筑设计防火规范" in nm, nm)
            code, p, _ = guiju("which", "JGJ 130-2011", home=home, timeout=240)
            nm = (p.get("announcement") or {}).get("name") or ""
            check("被代替引用不误导定位（JGJ 130）", "脚手架" in nm, nm)

            code, p, _ = guiju("pull", "JGJ/T450-2018", home=home, timeout=600)
            path = p.get("path", "") or ""
            check("pull 按编号成功", code == 0 and p.get("magic") == "pdf" and (p.get("bytes") or 0) > 0,
                  f"{p.get('bytes')}B code={p.get('code')}")
            check("pull 指纹命名", "__" in Path(path).name and Path(path).suffix == ".pdf",
                  Path(path).name[:48])
            check("pull 用实施日窗口选中原版公告（2018）",
                  "2018" in str(p.get("source_page", "")), p.get("source_page", "")[-28:])

            code, p2, _ = guiju("pull", "JGJ/T450-2018", home=home, timeout=600)
            check("pull 幂等返回 10", code == 10 and p2.get("already") is True)

            mixed = Path(home) / "batch.txt"
            mixed.write_text("# 冒烟清单\nJGJ/T450-2018  # 已下载\nGB99999-2099\n", encoding="utf-8")
            code, p, _ = guiju("pull", "--list", str(mixed), home=home, timeout=600)
            counts = p.get("counts", {})
            check("批量按退出码汇总", code == 0 and counts.get("10") == 1 and counts.get("31") == 1, str(counts))
            check("批量给出产出目录", bool(p.get("out_dir")), str(p.get("out_dir")))
            check("批量 ok 只排除严重失败（31 不算）", p.get("ok") is True)

        # ---- 上海地标（独立源，不需要时效服务） ----
        code, p, _ = guiju("pull", "--source", "shzjw", "DG/TJ08-2007", home=home, timeout=600)
        check(
            "上海地标可按编号下载（DG/TJ）",
            code == 0 and p.get("magic") == "pdf" and (p.get("bytes") or 0) > 0,
            detail_of(code, p) or f"{p.get('bytes')}B {p.get('code')}",
        )
        check(
            "地标状态来自源站（现行/废止分栏）",
            p.get("status") in ("current", "withdrawn"),
            str(p.get("status")),
        )
        code, p2, _ = guiju("pull", "--source", "shzjw", "DG/TJ08-2007", home=home, timeout=600)
        check("上海地标幂等返回 10", code == 10 and p2.get("already") is True, str(code))

        # ---- 本地路径：按公告ID取正文（不依赖时效服务） ----
        code, p, _ = guiju("search", "城市给水工程项目建设标准", home=home)
        ann_id = str((p.get("results") or [{}])[0].get("id", "") or "")
        if not ann_id:
            for nm in (
                "pull 按公告ID（无编号亦可下载）", "pull 幂等（公告ID路径）",
                "cite 按公告ID（自解析编号）",
            ):
                skip(nm, "目录为空：sync 未成功，跳过后继本地路径断言")
            return _summary_outputs(home)

        code, p, _ = guiju("pull", ann_id, home=home, timeout=600)
        check("pull 按公告ID（无编号亦可下载）", code == 0 and p.get("magic") == "pdf",
              detail_of(code, p) or f"{p.get('bytes')}B code={p.get('code')}")

        code, p, _ = guiju("pull", ann_id, home=home, timeout=600)
        check("pull 幂等（公告ID路径）", code == 10 and p.get("already") is True)

        # ---- 引用串：按公告ID自解析编号（不需要时效服务） ----
        code, p, _ = guiju("search", "建筑防火通用规范", home=home)
        fire_id = str((p.get("results") or [{}])[0].get("id", ""))
        code, p, _ = guiju("cite", fire_id, "5.2.1", home=home, timeout=240)
        check("cite 按公告ID（自解析编号）", code == 0 and "第5.2.1条" in (p.get("cite") or ""),
              (p.get("cite") or "")[:44])

        if cur_ok:
            # 同名多公告 = 版本迭代（2024 局部修订 / 2019 发布 / 2014 / 2006）：
            # 必须按权威时效自动定版，而不是把人叫来从 4 个同名候选里挑
            code, p, _ = guiju("pull", "绿色建筑评价标准", home=home, timeout=900)
            check(
                "同名多版本 → 按时效自动定版（不返回 32）",
                code in (0, 10) and p.get("code") == "GB/T 50378-2019" and p.get("status") == "current",
                f"{p.get('code')} / {p.get('published_on')}",
            )
            code, p, _ = guiju("cite", "JGJ/T450-2018", "5.2.1", home=home, timeout=240)
            check("cite 按编号", code == 0 and "第5.2.1条" in (p.get("cite") or ""), (p.get("cite") or "")[:40])
            code, p, _ = guiju("cite", "JGJ 147-2004", "1.0.1", "--strict", home=home, timeout=240)
            check("cite --strict 拒绝被替代版本", code == 32 and not p.get("ok"), f"exit={code}")
        else:
            code, p, _ = guiju("cite", "JGJ/T450-2018", "5.2.1", "--offline", home=home, timeout=240)
            check("无时效服务 + --offline 按编号引用 → 31（说清而非编造）", code == 31, p.get("reason", ""))

        # ---- 诚实歧义与退出码契约（纯本地） ----
        code, p, _ = guiju("pull", "老年人照料设施建筑设计标准", home=home, timeout=240)
        if cur_ok:
            # v0.14.0 起同名多公告会按时效自动定版；32 只留给"定不出唯一现行版"的情况
            check("同名多公告 → 按时效定版（不再默认 32）",
                  code in (0, 10) and bool(p.get("code")), f"exit={code} code={p.get('code')}")
        else:
            check("无时效服务时同名多公告仍 32（交人）",
                  code == 32 and len(p.get("candidates") or []) >= 2, f"exit={code}")

        code, p, _ = guiju("cite", fire_id, "not-a-clause", home=home)
        check("非法条款号 → 2", code == 2 and p.get("reason") == "bad_usage", p.get("reason", ""))

        code, p, _ = guiju("cite", "量子计算施工验收规范", "1.0.1", home=home, timeout=240)
        check("本地没有的标准 → 31", code == 31, p.get("reason", ""))

        code, p, _ = guiju("cite", "钢结构设计标准", "1.0.1", home=home, timeout=240)
        check("同名多公告的编号 → 32（交人决断）", code == 32, p.get("reason", ""))

        return _summary_outputs(home)
    finally:
        if not args.keep and not args.home:
            shutil.rmtree(home, ignore_errors=True)

    return _summary()


def _summary_outputs(home: str) -> int:
    env = dict(os.environ, GUIJU_HOME=home, GUIJU_NO_AUTO_SYNC="1")
    proc = subprocess.run([sys.executable, "-m", "guiju", "status", "--json"],
                          capture_output=True, text=True, cwd=str(ROOT), env=env)
    check("--json 时 stdout 只有 JSON", proc.stdout.strip().startswith("{"), proc.stdout[:20])
    check("--json 时人读输出不混入 stdout", "库：" not in proc.stdout)
    return _summary()


def _summary() -> int:
    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[1]]
    print()
    print(f"合计 {total} 项，通过 {total - len(failed)}，失败 {len(failed)}，跳过 {len(SKIPPED)}")
    for name, why in SKIPPED:
        print(f"  · SKIP {name} —— {why}")
    for name, _, detail in failed:
        print(f"  · FAIL {name} {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
