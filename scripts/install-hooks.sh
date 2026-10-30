#!/usr/bin/env bash
# 安装仓库自带 git 钩子（本地 CI 约束）。
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
chmod +x scripts/ci.sh .githooks/pre-push
git config core.hooksPath .githooks
echo "已启用 .githooks（pre-push 会跑 scripts/ci.sh）"
echo "临时跳过：SKIP_CI=1 git push"
