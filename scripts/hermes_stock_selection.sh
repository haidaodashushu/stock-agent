#!/usr/bin/env bash
set -u
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${STOCK_SELECTION_PYTHON:-$ROOT/.venv/bin/python}"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
PROFILE="${HERMES_PROFILE:-stock}"
PROMPT_FILE="$ROOT/config/agent_stock_selection_prompt.md"
LOG_DIR="$ROOT/logs/stock_selection"
RUN_DIR="$ROOT/.run"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
STAGE_FILE="$LOG_DIR/${RUN_ID}.stage.json"
RESPONSE_FILE="$LOG_DIR/${RUN_ID}.ai-response.txt"
DECISION_FILE="$LOG_DIR/${RUN_ID}.decision.json"
LOG_FILE="$LOG_DIR/${RUN_ID}.log"
LOCK_FILE="$RUN_DIR/hermes_stock_selection.lock"
REPORT_FILE=""
TARGET="today"
LABEL="盘前选股"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET="${2:-}"
      shift 2
      ;;
    --label)
      LABEL="${2:-}"
      shift 2
      ;;
    --json-output)
      REPORT_FILE="${2:-}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$LOG_DIR" "$RUN_DIR"
cd "$ROOT" || exit 2
if [[ -z "$REPORT_FILE" ]]; then
  REPORT_FILE="$LOG_DIR/${RUN_ID}.report.json"
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "AI 最终选股任务仍在运行，本轮拒绝重复启动。" >&2
  exit 1
fi

{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] start: $LABEL"
  echo "target=$TARGET"
} >"$LOG_FILE"

if ! "$PYTHON" scripts/daily_screen.py \
  --target "$TARGET" --label "$LABEL" --json-output "$STAGE_FILE" \
  >>"$LOG_FILE" 2>&1; then
  echo "量化候选池生成失败，未调用 AI。" >&2
  tail -n 20 "$LOG_FILE" >&2
  exit 1
fi

AS_OF="$($PYTHON - "$STAGE_FILE" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(payload.get("as_of") or "")
PY
)"
if [[ -z "$AS_OF" ]]; then
  echo "候选池元数据缺少 as_of，未调用 AI。" >&2
  exit 1
fi

timeout "${STOCK_SELECTION_MODEL_TIMEOUT:-360}s" \
  "$HERMES_BIN" -p "$PROFILE" chat --ignore-rules --toolsets stock-selection --quiet \
  --query "$(<"$PROMPT_FILE")" \
  >"$RESPONSE_FILE" 2>>"$LOG_FILE"
MODEL_RC=$?
printf 'model_rc=%s as_of=%s\n' "$MODEL_RC" "$AS_OF" >>"$LOG_FILE"

if [[ ! -f "$RESPONSE_FILE" ]]; then
  : >"$RESPONSE_FILE"
fi
if ! "$PYTHON" scripts/execute_stock_selection.py \
  --expected-as-of "$AS_OF" \
  --response "$RESPONSE_FILE" \
  --decision-out "$DECISION_FILE" \
  --report-out "$REPORT_FILE" \
  >>"$LOG_FILE" 2>&1; then
  echo "AI 最终选股失败；量化候选池已保留，screen_records 未使用脚本结果降级覆盖。" >&2
  tail -n 20 "$LOG_FILE" >&2
  exit 1
fi

cat "$DECISION_FILE"
