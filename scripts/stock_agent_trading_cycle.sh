#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${STOCK_PYTHON:-$ROOT/.venv/bin/python}"
MODE="${1:-}"
case "$MODE" in
  simulated) TASK="trading-simulated" ;;
  live) TASK="trading-live" ;;
  *) echo "usage: $0 simulated|live" >&2; exit 2 ;;
esac

STAGE="$(date '+%H%M')"
LOG_DIR="$ROOT/logs/trading_cycle/$MODE"
mkdir -p "$LOG_DIR"
cd "$ROOT"

# The broad cron expressions also fire at 09:00 and during the lunch break.
# Keep the effective stages explicit; execution-time guards below the model
# boundary provide a second line of defense if a valid run finishes late.
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

set +e
refresh_output="$("$PYTHON" scripts/refresh_trading_cycle.py --stage "$STAGE" --mode "$MODE")"
refresh_rc=$?
set -e
if [[ "$refresh_rc" == "3" ]]; then
  exit 0
fi
if [[ "$refresh_rc" != "0" || -z "$refresh_output" ]]; then
  echo "${MODE} trading facts refresh failed" >&2
  exit 1
fi

dry_args=()
if [[ "${STOCK_TRADING_DRY_RUN:-0}" == "1" ]]; then
  dry_args+=(--dry-run)
fi
if ! "$PYTHON" scripts/run_stock_agent.py \
  --task "$TASK" --stage "$STAGE" "${dry_args[@]}"; then
  alert="$(mktemp)"
  trap 'rm -f "$alert"' EXIT
  printf '⚠️ %s %s Agent 决策失败\n\n本轮没有执行成交或生成新建议，请查看 agent_scheduler.log。\n' \
    "$STAGE" "$MODE" >"$alert"
  "$PYTHON" scripts/send_feishu_message.py --file "$alert" --message-type text \
    --idempotency-key "agent_fail_${MODE}_$(date '+%Y%m%d_%H%M')" || true
  exit 1
fi
"$PYTHON" scripts/send_agent_outbox.py
