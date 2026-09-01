#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${STOCK_PYTHON:-$ROOT/.venv/bin/python}"
TARGET="today"
LABEL="盘前选股"
REPORT_OUT=""
SEND_OUTBOX=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) TARGET="${2:?}"; shift 2 ;;
    --label) LABEL="${2:?}"; shift 2 ;;
    --report-out|--json-output) REPORT_OUT="${2:?}"; shift 2 ;;
    --no-send) SEND_OUTBOX=0; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

RUN_ID="$(date '+%Y%m%d_%H%M%S')"
LOG_DIR="$ROOT/logs/stock_selection"
STAGE_FILE="$LOG_DIR/${RUN_ID}.stage.json"
[[ -n "$REPORT_OUT" ]] || REPORT_OUT="$LOG_DIR/${RUN_ID}.report.json"
mkdir -p "$LOG_DIR"
cd "$ROOT"

"$PYTHON" scripts/daily_screen.py \
  --target "$TARGET" --label "$LABEL" --json-output "$STAGE_FILE"
if ! "$PYTHON" scripts/run_stock_agent.py \
  --task selection --report-out "$REPORT_OUT" \
  --max-attempts "${STOCK_SELECTION_AGENT_ATTEMPTS:-2}" \
  --retry-delay "${STOCK_SELECTION_AGENT_RETRY_DELAY:-15}"; then
  alert="$(mktemp)"
  trap 'rm -f "$alert"' EXIT
  printf '⚠️ %s Agent 最终选股失败\n\n量化候选已保留，但正式候选没有被本轮失败结果覆盖。\n' \
    "$LABEL" >"$alert"
  "$PYTHON" scripts/send_feishu_message.py --file "$alert" --message-type text \
    --idempotency-key "agent_fail_selection_$(date '+%Y%m%d_%H%M')" || true
  exit 1
fi
if [[ "$SEND_OUTBOX" == "1" ]]; then
  "$PYTHON" scripts/send_agent_outbox.py
fi
