#!/usr/bin/env bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
PROFILE="${HERMES_PROFILE:-stock}"
POLICY_FILE="$ROOT/config/agent_trading_policy.md"
ENTRY_POLICY_FILE="$ROOT/config/agent_stock_entry_policy.md"
RUN_DIR="$ROOT/.run"
MODE="${1:-}"

case "$MODE" in
  simulated)
    LABEL="模拟盘"
    ICON="📈"
    TOOLSET="stock-simulated-trading"
    PROMPT_FILE="$ROOT/config/agent_simulated_trading_prompt.md"
    ;;
  live)
    LABEL="实盘"
    ICON="🛡️"
    TOOLSET="stock-live-trading"
    PROMPT_FILE="$ROOT/config/agent_live_trading_prompt.md"
    ;;
  *)
    echo "usage: $0 simulated|live" >&2
    exit 2
    ;;
esac

LOG_DIR="$ROOT/logs/trading_cycle/$MODE"
STAGE="$(date '+%H%M')"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
RESPONSE_FILE="$LOG_DIR/${RUN_ID}.ai-response.txt"
RETRY_RESPONSE_FILE="$LOG_DIR/${RUN_ID}.ai-response.retry.txt"
DECISION_FILE="$LOG_DIR/${RUN_ID}.decision.json"
RESULT_FILE="$LOG_DIR/${RUN_ID}.execution.json"
FORTUNE_FILE="$LOG_DIR/${RUN_ID}.fortune.txt"
LOG_FILE="$LOG_DIR/${RUN_ID}.log"
LOCK_FILE="$RUN_DIR/hermes_${MODE}_trading_cycle.lock"

mkdir -p "$LOG_DIR" "$RUN_DIR"
cd "$ROOT" || exit 2

case "$MODE:$STAGE" in
  simulated:0930|simulated:0931|simulated:1000|simulated:1001|\
  simulated:1030|simulated:1031|simulated:1100|simulated:1101|\
  simulated:1300|simulated:1301|simulated:1330|simulated:1331|\
  simulated:1400|simulated:1401|simulated:1430|simulated:1431) ;;
  live:0932|live:0933|live:0934|live:0935|live:0936|live:0937|live:0938|live:0939|\
  live:1002|live:1003|live:1004|live:1005|live:1006|live:1007|live:1008|live:1009|\
  live:1032|live:1033|live:1034|live:1035|live:1036|live:1037|live:1038|live:1039|\
  live:1102|live:1103|live:1104|live:1105|live:1106|live:1107|live:1108|live:1109|\
  live:1302|live:1303|live:1304|live:1305|live:1306|live:1307|live:1308|live:1309|\
  live:1332|live:1333|live:1334|live:1335|live:1336|live:1337|live:1338|live:1339|\
  live:1402|live:1403|live:1404|live:1405|live:1406|live:1407|live:1408|live:1409|\
  live:1432|live:1433|live:1434|live:1435|live:1436|live:1437|live:1438|live:1439) ;;
  *) exit 0 ;;
esac

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "$ICON **${STAGE:0:2}:${STAGE:2:2} ${LABEL}半小时操盘**"
  echo
  echo "上一轮${LABEL}任务尚未结束，本轮已跳过。"
  exit 0
fi

refresh_output="$("$PYTHON" scripts/refresh_trading_cycle.py \
  --stage "$STAGE" --mode "$MODE" 2>>"$LOG_FILE")"
refresh_rc=$?
if [[ "$refresh_rc" == "3" ]]; then
  # The broad half-hour cron expression also fires during the lunch break.
  # Empty stdout keeps those guard runs silent.
  exit 0
fi
if [[ "$refresh_rc" != "0" || -z "$refresh_output" ]]; then
  echo "$ICON **${STAGE:0:2}:${STAGE:2:2} ${LABEL}半小时操盘**"
  echo
  echo "本轮未执行：${LABEL}数据库事实刷新失败。"
  exit 0
fi
AS_OF="$(printf '%s\n' "$refresh_output" | tail -n 1)"
printf 'database refreshed: mode=%s stage=%s as_of=%s\n' "$MODE" "$STAGE" "$AS_OF" >>"$LOG_FILE"

MODEL_QUERY="$(cat "$ENTRY_POLICY_FILE"; printf '\n\n'; cat "$POLICY_FILE"; printf '\n\n'; cat "$PROMPT_FILE")"
timeout 240s "$HERMES_BIN" -p "$PROFILE" chat --ignore-rules --toolsets "$TOOLSET" --quiet \
  --query "$MODEL_QUERY" >"$RESPONSE_FILE" 2>>"$LOG_FILE"
model_rc=$?
if [[ "$model_rc" != "0" || ! -s "$RESPONSE_FILE" ]]; then
  printf 'AI decision attempt 1 failed (rc=%s); retrying once\n' "$model_rc" >>"$LOG_FILE"
  timeout 180s "$HERMES_BIN" -p "$PROFILE" chat --ignore-rules --toolsets "$TOOLSET" --quiet \
    --query "$MODEL_QUERY" >"$RETRY_RESPONSE_FILE" 2>>"$LOG_FILE"
  retry_rc=$?
  if [[ "$retry_rc" == "0" && -s "$RETRY_RESPONSE_FILE" ]]; then
    mv "$RETRY_RESPONSE_FILE" "$RESPONSE_FILE"
    model_rc=0
  else
    printf 'AI decision attempt 2 failed (rc=%s); persisting failed cycle\n' "$retry_rc" >>"$LOG_FILE"
    if [[ -s "$RETRY_RESPONSE_FILE" ]]; then
      mv "$RETRY_RESPONSE_FILE" "$RESPONSE_FILE"
    fi
    # Even a timeout must leave auditable decision/execution artifacts and a
    # user-facing no-trade report.  The executor converts partial/empty model
    # output into a structured failed decision without changing the account.
    "$PYTHON" scripts/execute_trading_cycle.py \
      --mode "$MODE" \
      --stage "$STAGE" \
      --expected-as-of "$AS_OF" \
      --response "$RESPONSE_FILE" \
      --decision-out "$DECISION_FILE" \
      --result-out "$RESULT_FILE" 2>>"$LOG_FILE"
    exit 0
  fi
fi

dry_args=()
if [[ "${STOCK_TRADING_DRY_RUN:-0}" == "1" ]]; then
  dry_args+=(--dry-run)
fi

if "$PYTHON" scripts/execute_trading_cycle.py \
  --mode "$MODE" \
  --stage "$STAGE" \
  --expected-as-of "$AS_OF" \
  --response "$RESPONSE_FILE" \
  --decision-out "$DECISION_FILE" \
  --result-out "$RESULT_FILE" \
  "${dry_args[@]}" 2>>"$LOG_FILE"; then
  # Fortune is a strictly downstream cultural annotation.  It only sees an
  # already-created live intent, cannot write trading state, and is allowed to
  # fail or time out without changing the validated advice above.
  if [[ "$MODE" == "live" && "${STOCK_TRADING_DRY_RUN:-0}" != "1" ]]; then
    timeout 180s "$PYTHON" scripts/render_live_trade_fortune.py \
      --result "$RESULT_FILE" \
      --as-of "$AS_OF" \
      --hermes-bin "$HERMES_BIN" \
      --profile "$PROFILE" \
      --output "$FORTUNE_FILE" 2>>"$LOG_FILE"
    fortune_rc=$?
    if [[ "$fortune_rc" != "0" && "$fortune_rc" != "3" ]]; then
      printf 'live fortune annotation unavailable (rc=%s); trading advice unchanged\n' \
        "$fortune_rc" >>"$LOG_FILE"
    fi
  fi
else
  echo "$ICON **${STAGE:0:2}:${STAGE:2:2} ${LABEL}半小时操盘**"
  echo
  echo "本轮未执行：${LABEL}数据库版本或执行层校验失败。"
fi
