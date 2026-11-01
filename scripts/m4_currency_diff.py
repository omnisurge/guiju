#!/usr/bin/env python3
"""M4 只读对比：NAS spec-currency MCP 与 guiju 原生时效逐字段 diff。

用法：
    python3 scripts/m4_currency_diff.py
    python3 scripts/m4_currency_diff.py --json --out /tmp/m4-report.json
    python3 scripts/m4_currency_diff.py --mcp-cache default  # 源不可达时用 NAS 已缓存结果
    python3 scripts/m4_currency_diff.py --codes-file tests/fixtures/currency/m4_codes.txt

只读：所有 MCP 查询带 cache=off，原生不装配磁盘缓存；不写 NAS、不取正文。
退出码：0 全部一致；1 存在差异或查询失败（M4 阻断）；2 输入/环境错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from guiju.currency import DEFAULT_MCP_URL, McpCurrency  # noqa: E402
from guiju.currency.m4diff import compare_rows, record_view  # noqa: E402
from guiju.currency.model import Record  # noqa: E402
from guiju.currency.native import NativeCurrency  # noqa: E402
from guiju.errors import GuijuError  # noqa: E402

DEFAULT_CODES = ROOT / "tests" / "fixtures" / "currency" / "m4_codes.txt"


def load_codes(path: Path) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line not in seen:
            seen.add(line)
            codes.append(line)
    return codes


def query_side(label: str, code: str, fn) -> dict:
    try:
        return {"ok": True, "rows": [record_view(row) for row in fn()]}
    except GuijuError as exc:
        return {"ok": False, "exit": exc.exit, "reason": exc.reason, "error": exc.message}
    except Exception as exc:  # 对比工具不吞编程错误，但要让报告显示为失败
        return {"ok": False, "exit": None, "reason": type(exc).__name__, "error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes-file", type=Path, default=DEFAULT_CODES)
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    parser.add_argument("--mcp-cache", choices=("off", "default"), default="off",
                        help="MCP 查询是否绕过 24h 缓存；默认 off 取最新，源不可达时可显式 default")
    parser.add_argument("--out", type=Path, help="完整 JSON 报告输出路径")
    parser.add_argument("--json", action="store_true", help="stdout 只输出完整 JSON")
    args = parser.parse_args(argv)

    try:
        codes = load_codes(args.codes_file)
    except OSError as exc:
        print(f"读取编号清单失败：{exc}", file=sys.stderr)
        return 2
    if not codes:
        print("编号清单为空", file=sys.stderr)
        return 2

    mcp = McpCurrency(args.mcp_url)
    native = NativeCurrency()
    results = []
    counts = {"matched": 0, "mismatched": 0, "errors": 0, "both_empty": 0}

    for index, code in enumerate(codes, 1):
        print(f"[{index}/{len(codes)}] {code}", file=sys.stderr, flush=True)

        def mcp_query(c=code):
            return mcp.lookup(c, cache=None if args.mcp_cache == "default" else "off")

        def native_query(c=code):
            return native.query(c).results

        mcp_result = query_side("mcp", code, mcp_query)
        native_result = query_side("native", code, native_query)
        if native_result["ok"] and native.last_outcome is not None:
            native_result["source"] = native.last_outcome.source
        if mcp_result["ok"] and native_result["ok"]:
            # 直接把展示字段重新映射为 Record，避免把展示字段当业务字段。
            mcp_rows = [Record(**row) for row in mcp_result["rows"]]
            native_rows = [Record(**row) for row in native_result["rows"]]
            comparison = compare_rows(mcp_rows, native_rows)
            status = "match" if comparison["match"] else "mismatch"
            counts["matched" if comparison["match"] else "mismatched"] += 1
            counts["both_empty"] += int(comparison["both_empty"])
        else:
            comparison = None
            status = "error"
            counts["errors"] += 1
        results.append({
            "input": code,
            "status": status,
            "mcp": mcp_result,
            "native": native_result,
            "comparison": comparison,
        })

    report = {
        "schema": "guiju-m4-diff-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mcp_url": args.mcp_url,
        "codes_file": str(args.codes_file),
        "summary": {"total": len(codes), **counts},
        "results": results,
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report["summary"]
        print(f"M4 逐字段 diff：{summary['total']} 个编号，match={summary['matched']}，"
              f"mismatch={summary['mismatched']}，error={summary['errors']}")
        for item in results:
            if item["status"] == "match":
                continue
            if item["status"] == "error":
                print(f"ERROR {item['input']}: mcp={item['mcp']} native={item['native']}")
                continue
            print(f"MISMATCH {item['input']}:")
            for diff in item["comparison"]["differences"]:
                print(f"  {diff}")
    return 0 if counts["mismatched"] == 0 and counts["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
