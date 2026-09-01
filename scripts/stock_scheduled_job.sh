#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
JOB="${1:-}"

# Market data and messaging must go direct to avoid consuming limited proxy
# traffic. CodexCliProvider loads the private proxy file only for Codex itself.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
unset http_proxy https_proxy all_proxy no_proxy

cd "$ROOT"

case "$JOB" in
  premarket-selection)
    exec scripts/stock_agent_selection_cycle.sh
    ;;
  night-selection)
    exec scripts/stock_agent_selection_cycle.sh \
      --target next-trading-day --label 夜间预选股
    ;;
  simulated-trading)
    exec scripts/stock_agent_trading_cycle.sh simulated
    ;;
  live-trading)
    exec scripts/stock_agent_trading_cycle.sh live
    ;;
  auction-cancelable)
    exec "$PYTHON" scripts/capture_opening_auction.py --phase cancelable_end
    ;;
  auction-locked)
    exec "$PYTHON" scripts/capture_opening_auction.py --phase locked_end
    ;;
  auction-final)
    "$PYTHON" scripts/capture_opening_auction.py --phase final
    "$PYTHON" scripts/refresh_candidate_board.py
    exec scripts/stock_agent_candidate_promotion.sh
    ;;
  intraday-radar)
    set +e
    "$PYTHON" scripts/run_scheduled_intraday_radar.py
    rc=$?
    set -e
    [[ "$rc" == "3" ]] && exit 0
    [[ "$rc" == "0" ]] || exit "$rc"
    "$PYTHON" scripts/refresh_candidate_board.py
    exec scripts/stock_agent_candidate_promotion.sh
    ;;
  candidate-board)
    exec "$PYTHON" scripts/refresh_candidate_board.py
    ;;
  close-review)
    exec scripts/system_stock_command_job.sh "每日收盘复盘"
    ;;
  policy-radar)
    exec scripts/system_stock_command_job.sh "政策热点雷达"
    ;;
  nightly-update)
    message="$(mktemp)"
    trap 'rm -f "$message"' EXIT
    set +e
    scripts/hermes_nightly_update.sh | tee "$message"
    rc=${PIPESTATUS[0]}
    set -e
    "$PYTHON" scripts/send_configured_message.py \
      --file "$message" --message-type text \
      --idempotency-key "nightly_$(date '+%Y%m%d')" || true
    exit "$rc"
    ;;
  *)
    echo "usage: $0 {premarket-selection|night-selection|simulated-trading|live-trading|auction-cancelable|auction-locked|auction-final|intraday-radar|candidate-board|close-review|policy-radar|nightly-update}" >&2
    exit 2
    ;;
esac
