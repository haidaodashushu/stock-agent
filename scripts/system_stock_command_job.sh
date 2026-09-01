#!/usr/bin/env bash
set -u
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/logs/system_cron/command_jobs"
RUN_DIR="$ROOT/.run"
PYTHON_BIN="${STOCK_CRON_PYTHON:-/usr/bin/python3}"

JOB_NAME="${1:-}"
if [[ -z "$JOB_NAME" ]]; then
  echo "usage: $0 <job-name>" >&2
  exit 2
fi

mkdir -p "$LOG_DIR" "$RUN_DIR"
cd "$ROOT" || exit 2

SAFE_NAME="$(printf '%s' "$JOB_NAME" | "$PYTHON_BIN" -c 'import hashlib,re,sys; raw=sys.stdin.read(); slug=re.sub(r"[^0-9A-Za-z_.-]+", "_", raw).strip("_") or "job"; print(f"{slug}_{hashlib.sha1(raw.encode()).hexdigest()[:8]}")')"
RUN_ID="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOG_DIR/${SAFE_NAME}_${RUN_ID}.log"
LATEST_LOG="$LOG_DIR/${SAFE_NAME}_latest.log"
MSG_FILE="$LOG_DIR/${SAFE_NAME}_${RUN_ID}.message.txt"
REPORT_FILE="$LOG_DIR/${SAFE_NAME}_${RUN_ID}.report.json"
PRESENTATION_FILE="$LOG_DIR/${SAFE_NAME}_${RUN_ID}.presentation.json"
LOCK_FILE="$RUN_DIR/system_command_${SAFE_NAME}.lock"
REPORT_PROFILE="plain_text"

notify() {
  local message_file="$1"
  if [[ "${STOCK_CRON_NO_NOTIFY:-0}" == "1" ]]; then
    return 0
  fi
  "$ROOT/.venv/bin/python" scripts/send_feishu_message.py \
    --file "$message_file" --message-type text \
    --idempotency-key "${SAFE_NAME}_${RUN_ID}_text" \
    >>"$LOG_FILE" 2>&1 || true
}

send_card_report() {
  local card_file="$1"
  if [[ "${STOCK_CRON_NO_NOTIFY:-0}" == "1" ]]; then
    return 0
  fi
  "$ROOT/.venv/bin/python" scripts/send_feishu_message.py \
    --file "$card_file" --message-type interactive \
    --idempotency-key "${SAFE_NAME}_${RUN_ID}_card" \
    >>"$LOG_FILE" 2>&1
}

case "$JOB_NAME" in
  "每日盘前选股")
    COMMAND=(scripts/stock_agent_selection_cycle.sh --json-output "$REPORT_FILE" --no-send)
    REPORT_PROFILE="screening_report"
    ;;
  "夜间预选股")
    COMMAND=(scripts/stock_agent_selection_cycle.sh --target next-trading-day --label 夜间预选股 --json-output "$REPORT_FILE" --no-send)
    REPORT_PROFILE="screening_report"
    ;;
  "自选监控-配置化轮询")
    COMMAND=("$PYTHON_BIN" scripts/run_watchlist_monitors.py)
    ;;
  "持仓新闻扫描")
    COMMAND=("$PYTHON_BIN" scripts/monitor_news.py)
    ;;
  "候选池新闻公告扫描")
    COMMAND=("$PYTHON_BIN" scripts/scan_candidate_intelligence.py --max-codes 25 --max-screen 30 --per-stock 3)
    ;;
  "政策热点雷达")
    COMMAND=("$PYTHON_BIN" scripts/scan_policy_hotspots.py)
    ;;
  "每日收盘复盘")
    COMMAND=("$PYTHON_BIN" scripts/monitor_close.py --json-output "$REPORT_FILE")
    REPORT_PROFILE="close_review"
    ;;
  *)
    echo "unknown stock command cron job: $JOB_NAME" >&2
    exit 2
    ;;
esac

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  {
    echo "$JOB_NAME：跳过"
    echo ""
    echo "- 原因：上一轮同名任务仍在运行，已避免重复执行。"
    echo "- 时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
    echo "- 日志：$LOG_FILE"
  } > "$MSG_FILE"
  notify "$MSG_FILE"
  exit 0
fi

START_TS="$(date +%s)"
{
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] start: $JOB_NAME"
  printf 'command:'
  printf ' %q' "${COMMAND[@]}"
  echo
  echo "root=$ROOT"
  echo
} > "$LOG_FILE"

if [[ "${STOCK_CRON_DRY_RUN:-0}" == "1" ]]; then
  echo "DRY_RUN: command skipped" >> "$LOG_FILE"
  RC=0
else
  "${COMMAND[@]}" >> "$LOG_FILE" 2>&1
  RC=$?
fi

END_TS="$(date +%s)"
DURATION="$((END_TS - START_TS))"
ln -sf "$LOG_FILE" "$LATEST_LOG"

SHOULD_NOTIFY="$("$PYTHON_BIN" - "$JOB_NAME" "$RC" "$DURATION" "$LOG_FILE" "$MSG_FILE" <<'PY'
import json
import re
import sys
from pathlib import Path

job_name, rc_raw, duration, log_path, msg_path = sys.argv[1:6]
rc = int(rc_raw)
text = Path(log_path).read_text(encoding="utf-8", errors="replace")
body = text.split("\n\n", 1)[1] if "\n\n" in text else text
body = body.strip()

def trim(s: str, limit: int = 1200) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[:800].rstrip() + "\n...\n" + s[-350:].lstrip()


def yes(value) -> bool:
    return str(value).strip().lower() not in ("", "0", "false", "none", "[]", "{}")


def compact_json_summary(job: str, parsed: dict) -> tuple[str, bool]:
    """Return (summary, important_enough_to_notify) for successful JSON jobs."""
    if job == "持仓新闻扫描":
        high = parsed.get("high") or []
        lines = [
            f"持仓: {parsed.get('positions', 0)}",
            f"新增事件: {parsed.get('inserted', 0)} / 扫描 {parsed.get('events_seen', 0)}",
        ]
        if high:
            lines.append("")
            lines.append("| 代码 | 名称 | 风险 | 标题 |")
            lines.append("|---|---|---|---|")
            for item in high[:5]:
                lines.append(
                    f"| {item.get('code','')} | {item.get('name','')} | {item.get('risk_level','')} | {trim(str(item.get('title','')), 80)} |"
                )
        return "\n".join(lines), bool(high)

    if job == "候选池新闻公告扫描":
        changes = parsed.get("top_logic_changes") or []
        notable = []
        for item in changes:
            try:
                boost = float(item.get("boost") or 0)
            except Exception:
                boost = 0.0
            if boost >= 3:
                notable.append(item)
        lines = [
            f"候选: {parsed.get('candidate_count', 0)}",
            f"新增事件: {parsed.get('inserted', 0)} / 扫描 {parsed.get('events_seen', 0)}",
            f"源错误: {parsed.get('source_errors', 0)}",
        ]
        if notable:
            lines.append("")
            lines.append("| 代码 | 名称 | 逻辑分 | 事件数 | 依据 |")
            lines.append("|---|---|---:|---:|---|")
            for item in notable[:5]:
                lines.append(
                    f"| {item.get('code','')} | {item.get('name','')} | {item.get('boost','')} | {item.get('event_count','')} | {trim(str(item.get('summary') or item.get('reason') or ''), 80)} |"
                )
        return "\n".join(lines), bool(notable)

    if job == "政策热点雷达":
        high = parsed.get("high") or []
        lines = [
            f"查询组: {parsed.get('queries', 0)}",
            f"新增政策: {parsed.get('inserted', 0)} / 命中 {parsed.get('events_seen', 0)}",
            f"源错误: {parsed.get('source_errors', 0)}",
        ]
        if high:
            lines.append("")
            lines.append("| 时间 | 分 | 标题 |")
            lines.append("|---|---:|---|")
            for item in high[:6]:
                lines.append(
                    f"| {item.get('publish_at','')} | {item.get('score','')} | {trim(str(item.get('title','')), 72)} |"
                )
        return "\n".join(lines), bool(high) or yes(parsed.get("source_errors", 0))

    keys = [
        "timestamp", "date", "target_date", "positions", "candidate_count",
        "events_seen", "inserted", "high", "top_logic_changes", "executed",
        "skipped", "error", "status",
    ]
    lines = []
    important = False
    for key in keys:
        if key not in parsed:
            continue
        val = parsed[key]
        if key in ("high", "error") and yes(val):
            important = True
        if isinstance(val, (dict, list)):
            val = json.dumps(val, ensure_ascii=False)
        lines.append(f"- {key}: {val}")
    return "\n".join(lines) if lines else trim(json.dumps(parsed, ensure_ascii=False)), important


def compact_watchlist_summary(raw: str) -> tuple[str, bool]:
    ran: list[str] = []
    skipped: list[str] = []
    header = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("自选监控 "):
            header = stripped
        elif stripped.startswith("已执行:"):
            value = stripped.split(":", 1)[1].strip()
            if value and value != "无":
                ran = [x.strip() for x in value.split(",") if x.strip()]
        elif stripped.startswith("跳过:"):
            value = stripped.split(":", 1)[1].strip()
            if value and value != "无":
                skipped = [x.strip() for x in value.split(",") if x.strip()]

    if not ran:
        summary = header or "自选监控：无到期任务"
        if skipped:
            summary += f"\n跳过: {','.join(skipped)}"
        return summary, False

    lines = [header or "自选监控结果", ""]
    lines.append("| 代码 | 名称 | 阶段 | 建议 | 更新时间 |")
    lines.append("|---|---|---|---|---|")
    for code in ran:
        path = Path("monitoring") / f"{code}_washout.json"
        try:
            monitor = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            monitor = {}
        card = (monitor.get("monitors") or [{}])[0]
        name = monitor.get("name") or card.get("name") or ""
        phase = card.get("phase") or (monitor.get("washed_out_pattern") or {}).get("current_phase") or ""
        advice = card.get("advice") or (monitor.get("washed_out_pattern") or {}).get("next_trigger") or ""
        updated = card.get("updated_at") or monitor.get("last_updated_at") or ""
        lines.append(f"| {code} | {name} | {phase} | {trim(str(advice), 32)} | {updated} |")
    if skipped:
        lines.extend(["", f"未到期/跳过: {','.join(skipped)}"])
    return "\n".join(lines), True


def parse_json_payload(raw: str):
    """Parse a JSON result even when warnings were written before it."""
    try:
        return json.loads(raw)
    except Exception:
        pass

    def score(obj: dict) -> int:
        result_keys = {
            "timestamp", "candidate_count", "events_seen", "inserted",
            "source_errors", "top_logic_changes", "positions", "high",
            "executed", "skipped", "status", "error",
        }
        return sum(1 for key in result_keys if key in obj)

    decoder = json.JSONDecoder()
    best = None
    best_score = 0
    for match in re.finditer(r"\{", raw):
        start = match.start()
        try:
            parsed, end = decoder.raw_decode(raw[start:])
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        parsed_score = score(parsed)
        tail = raw[start + end:].strip()
        if not tail and parsed_score:
            return parsed
        if parsed_score > best_score:
            best = parsed
            best_score = parsed_score
    return best


summary = ""
parsed = parse_json_payload(body)

if isinstance(parsed, dict):
    summary, important = compact_json_summary(job_name, parsed)
elif job_name == "自选监控-配置化轮询":
    summary, important = compact_watchlist_summary(body)
else:
    summary = trim(body)
    important = any(token in body for token in ("ERROR", "Error", "Traceback", "失败", "异常", "高风险"))

status = "完成" if rc == 0 else "失败"
always_notify = job_name in {"每日盘前选股", "夜间预选股", "每日收盘复盘"}
quiet_success = job_name in {"自选监控-配置化轮询", "持仓新闻扫描", "候选池新闻公告扫描", "政策热点雷达"}
should_notify = rc != 0 or always_notify or important
if quiet_success and rc == 0 and not important:
    should_notify = False

lines = [
    f"{job_name}：{status}",
    f"耗时：{duration}s",
    f"日志：{log_path}",
]
if summary:
    lines.extend(["", summary])
else:
    lines.extend(["", "脚本无输出。"])
if not should_notify and rc == 0:
    lines.append("")
    lines.append("本轮为常规成功采集，默认不推送飞书。")

Path(msg_path).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
print("1" if should_notify else "0")
PY
)"

if [[ "$SHOULD_NOTIFY" == "1" ]]; then
  if [[ "$REPORT_PROFILE" != "plain_text" && -s "$REPORT_FILE" ]] && "$PYTHON_BIN" scripts/render_cron_report.py \
    --report "$REPORT_FILE" \
    --presentation-out "$PRESENTATION_FILE" \
    >>"$LOG_FILE" 2>&1; then
    if ! send_card_report "$PRESENTATION_FILE"; then
      {
        echo "$JOB_NAME：飞书卡片发送失败"
        echo ""
        echo "- 报告已正常生成，但卡片投递失败。"
        echo "- 时间：$(date '+%Y-%m-%d %H:%M:%S %z')"
        echo "- 日志：$LOG_FILE"
      } >"$MSG_FILE"
      notify "$MSG_FILE"
    fi
  else
    notify "$MSG_FILE"
  fi
fi
exit "$RC"
