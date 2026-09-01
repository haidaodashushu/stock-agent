#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${STOCK_PYTHON:-$ROOT/.venv/bin/python}"
cd "$ROOT"

set +e
prepare_output="$("$PYTHON" scripts/prepare_candidate_promotion.py)"
prepare_rc=$?
set -e
if [[ "$prepare_rc" == "3" ]]; then
  exit 0
fi
if [[ "$prepare_rc" != "0" || -z "$prepare_output" ]]; then
  echo "candidate promotion preparation failed" >&2
  exit 1
fi

"$PYTHON" scripts/stock_candidate_promotion_mcp.py --check
"$PYTHON" scripts/run_stock_agent.py --task promotion
"$PYTHON" scripts/refresh_candidate_board.py
