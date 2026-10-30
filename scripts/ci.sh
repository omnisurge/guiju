#!/usr/bin/env bash
# 本地 CI —— 与 .github/workflows/ci.yml 同一套门，但不依赖网络。
#
# 用法：
#   scripts/ci.sh                 # 默认用本机 python3
#   PYTHON=python3.10 scripts/ci.sh
#   WITH_PY310=1 scripts/ci.sh    # 若装了 uv，额外用 3.10 跑一遍单测
#
# 退出码：0 全绿；1 有门没过（pre-push 会据此拒绝推送）。
set -euo pipefail

cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
export PYTHONWARNINGS=default

echo "本地 CI · Python: $($PY -V 2>&1)"

echo "== 1/4 静态检查 (ruff) =="
if command -v ruff >/dev/null 2>&1; then
  ruff check .
elif command -v uv >/dev/null 2>&1; then
  uv tool run ruff check .
else
  echo "  · 跳过：未安装 ruff（GitHub CI 仍会跑）。安装：uv tool install ruff"
fi

echo "== 2/4 单元测试 =="
"$PY" -m unittest discover -s tests -t .

echo "== 3/4 离线冒烟（装配 + 退出码契约） =="
"$PY" scripts/e2e.py --offline

echo "== 4/4 入口自检 =="
"$PY" -m guiju version --json >/dev/null
"$PY" -m guiju status --json >/dev/null
"$PY" -m guiju --help >/dev/null

if [ "${WITH_PY310:-0}" = "1" ]; then
  echo "== 附赠：Python 3.10 单测（声明的下限） =="
  if command -v uv >/dev/null 2>&1; then
    PY310="$(uv python find 3.10 2>/dev/null || uv python install 3.10 >/dev/null 2>&1 && uv python find 3.10)"
    "$PY310" -m unittest discover -s tests -t .
  else
    echo "  · 跳过：需要 uv 才能取到 3.10"
  fi
fi

echo "本地 CI 全绿。"
