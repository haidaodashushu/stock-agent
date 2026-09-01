#!/usr/bin/env bash
set -u
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
LOG_DIR="$ROOT/logs/system_cron"
RUN_DIR="$ROOT/.run"
LOCK_FILE="$RUN_DIR/nightly_update.lock"
mkdir -p "$LOG_DIR" "$RUN_DIR"

RUN_ID="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOG_DIR/nightly_update_${RUN_ID}.log"
LATEST_LOG="$LOG_DIR/nightly_update_latest.log"
MSG_FILE="$LOG_DIR/nightly_update_${RUN_ID}.message.txt"

cd "$ROOT" || exit 2
: > "$LOG_FILE"

emit() {
  cat "$MSG_FILE"
}

human_duration() {
  local total="$1"
  local minutes=$((total / 60))
  local seconds=$((total % 60))
  if (( minutes > 0 )); then
    printf '%d 分 %d 秒' "$minutes" "$seconds"
  else
    printf '%d 秒' "$seconds"
  fi
}

format_count() {
  "$PYTHON_BIN" - "$1" <<'PY'
import sys

try:
    print(f"{int(sys.argv[1]):,}")
except (ValueError, IndexError):
    print(sys.argv[1] if len(sys.argv) > 1 else "未知")
PY
}

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  {
    echo "⏸️ 夜间数据更新已跳过"
    echo
    echo "上一轮任务仍在运行，本轮未重复启动。"
  } > "$MSG_FILE"
  emit
  exit 0
fi

if ! MARKET_INFO="$("$PYTHON_BIN" - <<'PY'
from data.market_calendar import market_day
md = market_day()
print(f"{md.date}|{1 if md.is_open else 0}|{md.reason}")
PY
)"; then
  {
    echo "❌ 夜间数据更新失败"
    echo
    echo "交易日历检查失败。"
    echo
    echo "日志：\`$LOG_FILE\`"
  } > "$MSG_FILE"
  emit
  exit 1
fi

MARKET_DATE="${MARKET_INFO%%|*}"
MARKET_REST="${MARKET_INFO#*|}"
MARKET_OPEN="${MARKET_REST%%|*}"
MARKET_REASON="${MARKET_REST#*|}"

if [[ "$MARKET_OPEN" != "1" ]]; then
  {
    echo "⏸️ 夜间数据更新已跳过"
    echo
    echo "**$MARKET_DATE** 为$MARKET_REASON，无需更新。"
  } > "$MSG_FILE"
  ln -sf "$LOG_FILE" "$LATEST_LOG"
  emit
  exit 0
fi

START_TS="$(date +%s)"
echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] nightly_update start" > "$LOG_FILE"

if [[ "${STOCK_CRON_DRY_RUN:-0}" == "1" ]]; then
  RC=0
  echo "DRY_RUN: skip $PYTHON_BIN scripts/nightly_update.py" >> "$LOG_FILE"
else
  "$PYTHON_BIN" scripts/nightly_update.py >> "$LOG_FILE" 2>&1
  RC=$?
fi

END_TS="$(date +%s)"
DURATION="$((END_TS - START_TS))"
DB_STATS="$("$PYTHON_BIN" - <<'PY' 2>>"$LOG_FILE"
import sqlite3
conn = sqlite3.connect("data/stock_data.db")
try:
    latest, rows = conn.execute(
        "SELECT COALESCE(MAX(date),'unknown'), COUNT(*) FROM daily_prices"
    ).fetchone()
    print(f"{latest}|{rows}")
finally:
    conn.close()
PY
)"
LATEST_DATE="${DB_STATS%%|*}"
TOTAL_ROWS="${DB_STATS#*|}"
SUMMARY_LINE="$(grep -E '夜间更新完成|DRY_RUN' "$LOG_FILE" | tail -1 || true)"
SUCCESS="$(printf '%s\n' "$SUMMARY_LINE" | sed -n 's/.*成功:\([0-9][0-9]*\).*/\1/p')"
FAIL="$(printf '%s\n' "$SUMMARY_LINE" | sed -n 's/.*失败:\([0-9][0-9]*\).*/\1/p')"

DURATION_TEXT="$(human_duration "$DURATION")"
TOTAL_ROWS_TEXT="$(format_count "$TOTAL_ROWS")"
SUCCESS_TEXT="$(format_count "${SUCCESS:-未知}")"
FAIL_TEXT="$(format_count "${FAIL:-未知}")"

{
  if [[ "$RC" != "0" ]]; then
    echo "❌ 夜间数据更新失败"
    echo
    echo "- 数据日期：$LATEST_DATE"
    echo "- 已耗时：$DURATION_TEXT"
    echo "- 日志：\`$LOG_FILE\`"
  elif [[ "${STOCK_CRON_DRY_RUN:-0}" == "1" ]]; then
    echo "🧪 夜间数据更新演练完成"
    echo
    echo "未请求行情，也未写入数据库。"
  elif [[ -n "$SUCCESS" || -n "$FAIL" ]]; then
    if [[ "${FAIL:-0}" != "0" ]]; then
      echo "⚠️ 夜间数据更新完成（部分标的异常）"
    else
      echo "✅ 夜间数据更新完成"
    fi
    echo
    echo "**日K数据已更新至 $LATEST_DATE**"
    echo
    echo "- 更新成功：$SUCCESS_TEXT 只"
    echo "- 更新失败：$FAIL_TEXT 只"
    echo "- 数据总量：$TOTAL_ROWS_TEXT 条"
    echo "- 耗时：$DURATION_TEXT"
    if [[ "${FAIL:-0}" != "0" ]]; then
      echo
      echo "异常标的已记录到运行日志。"
    fi
  else
    echo "⚠️ 夜间数据更新完成，但结果统计缺失"
    echo
    echo "- 日K数据日期：$LATEST_DATE"
    echo "- 数据总量：$TOTAL_ROWS_TEXT 条"
    echo "- 耗时：$DURATION_TEXT"
    echo "- 日志：\`$LOG_FILE\`"
  fi
} > "$MSG_FILE"

ln -sf "$LOG_FILE" "$LATEST_LOG"
emit
exit "$RC"
