#!/usr/bin/env bash
# C0 baseline characterization gate.
#
# Usage:
#   scripts/ci-c0-baseline.sh <worktree>
#   GUIJU_C0_COMMIT=<c0-commit> scripts/ci-c0-baseline.sh <worktree>
#   GUIJU_C0_COMMIT=2a7be88 scripts/ci-c0-baseline.sh --no-git <archive-tree>
#
# The no-git mode exists for git archive output on 2a7be88, which has no .git.
# It is deliberately explicit; a normal worktree without a matching HEAD is
# rejected rather than silently skipped.
set -euo pipefail

no_git=0
if [ "${1:-}" = "--no-git" ]; then
  no_git=1
  shift
fi
tree="${1:-}"
if [ -z "$tree" ] || [ ! -d "$tree" ]; then
  echo "usage: $0 [--no-git] <tree>" >&2
  exit 2
fi

expected="${GUIJU_C0_COMMIT:-2a7be88}"
if [ "$no_git" -eq 0 ]; then
  if ! git -C "$tree" rev-parse --git-dir >/dev/null 2>&1; then
    echo "C0 gate: $tree has no git metadata; use --no-git only for a 2a7be88 archive" >&2
    exit 2
  fi
  head="$(git -C "$tree" rev-parse HEAD)"
  if [ "$head" != "$expected" ] && [ "${head:0:${#expected}}" != "$expected" ] && [ "${head:0:7}" != "2a7be88" ]; then
    echo "C0 gate: HEAD $head is not baseline 2a7be88 / GUIJU_C0_COMMIT=$expected" >&2
    exit 2
  fi
  echo "C0 gate: git worktree HEAD=$head"
else
  if [ "$expected" != "2a7be88" ]; then
    echo "C0 gate: --no-git is only for the 2a7be88 archive (GUIJU_C0_COMMIT=$expected)" >&2
    exit 2
  fi
  echo "C0 gate: no-git baseline: 2a7be88"
fi

home="$(mktemp -d)"
trap 'rm -rf "$home"' EXIT
export GUIJU_HOME="$home"
export GUIJU_NO_AUTO_SYNC=1
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"
cd "$tree"
log="/tmp/guiju-c0-baseline-${head:-archive}.log"
echo "C0 gate log: $log"
python3 -m unittest -v tests.char_baseline_20261121 2>&1 | tee "$log"
