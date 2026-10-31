#!/usr/bin/env bash
# 清掉本地产物（都可由源码重建）。不会碰 ~/.guiju —— 那里是数据，不是垃圾。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true
rm -rf .ruff_cache .pytest_cache .mypy_cache build dist ./*.egg-info

echo "已清理：__pycache__ / *.pyc / .ruff_cache / .pytest_cache / .mypy_cache / build / dist / *.egg-info"
