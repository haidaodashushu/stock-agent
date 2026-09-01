#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
MCP_PYTHON="${HERMES_MCP_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
PROFILE="${HERMES_PROFILE:-stock}"
RUN_DIR="$ROOT/.run"
LOG_DIR="$ROOT/logs/candidate_promotion"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESPONSE_FILE="$LOG_DIR/${RUN_ID}.ai-response.txt"
RESULT_FILE="$LOG_DIR/${RUN_ID}.result.json"
LOG_FILE="$LOG_DIR/${RUN_ID}.log"
LOCK_FILE="$RUN_DIR/hermes_candidate_promotion.lock"

mkdir -p "$LOG_DIR" "$RUN_DIR"
cd "$ROOT" || exit 2
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

prepare_output="$($PYTHON scripts/prepare_candidate_promotion.py 2>>"$LOG_FILE")"
prepare_rc=$?
if [[ "$prepare_rc" == "3" ]]; then
  exit 0
fi
if [[ "$prepare_rc" != "0" || -z "$prepare_output" ]]; then
  printf 'candidate promotion preparation failed rc=%s\n' "$prepare_rc" >>"$LOG_FILE"
  exit 1
fi
AS_OF="$(printf '%s\n' "$prepare_output" | tail -n 1)"
MODEL_QUERY="$(cat config/agent_stock_entry_policy.md; printf '\n\n'; cat config/agent_candidate_promotion_prompt.md)"

PREFLIGHT_FILE="$LOG_DIR/${RUN_ID}.preflight.txt"
timeout 20s "$MCP_PYTHON" scripts/stock_candidate_promotion_mcp.py --check \
  >"$PREFLIGHT_FILE" 2>>"$LOG_FILE"
preflight_rc=$?
if [[ "$preflight_rc" != "0" ]]; then
  preflight_error="$(tail -n 12 "$LOG_FILE" | tr '\n' ' ' | cut -c1-1000)"
  [[ -n "$preflight_error" ]] || preflight_error="promotion MCP preflight failed rc=$preflight_rc"
  "$PYTHON" scripts/execute_candidate_promotion.py \
    --expected-as-of "$AS_OF" --error "$preflight_error" --output "$RESULT_FILE" \
    >>"$LOG_FILE" 2>&1
  exit 1
fi

timeout 240s "$HERMES_BIN" -p "$PROFILE" chat --ignore-rules \
  --toolsets stock-candidate-promotion --quiet --query "$MODEL_QUERY" \
  >"$RESPONSE_FILE" 2>>"$LOG_FILE"
model_rc=$?
if [[ "$model_rc" != "0" || ! -s "$RESPONSE_FILE" ]]; then
  model_error="candidate promotion model failed rc=$model_rc"
  printf '%s\n' "$model_error" >>"$LOG_FILE"
  "$PYTHON" scripts/execute_candidate_promotion.py \
    --expected-as-of "$AS_OF" --error "$model_error" --output "$RESULT_FILE" \
    >>"$LOG_FILE" 2>&1
  exit 1
fi

if ! "$PYTHON" scripts/execute_candidate_promotion.py \
  --expected-as-of "$AS_OF" --response "$RESPONSE_FILE" --output "$RESULT_FILE" \
  >>"$LOG_FILE" 2>&1; then
  exit 1
fi

# The promotion is a candidate-management fact.  Rebuild the board now so the
# next normal trading cycle can consume it; this path never runs trading.
"$PYTHON" scripts/refresh_candidate_board.py >>"$LOG_FILE" 2>&1
