#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BASELINE_BRANCH="stock-workspace-initial"
EXPERIMENT_BRANCH="experiment/stateful-entry-strategy"
GATEWAY_SERVICE="hermes-gateway-stock.service"
WEB_SERVICE="stock-web.service"
ACTION="${1:-status}"
AGENT_RUNTIME_POINTER="$ROOT/data/runtime/scheduler-backups/active-backup"

status() {
  printf 'workspace=%s\n' "$ROOT"
  printf 'branch=%s\n' "$(git -C "$ROOT" branch --show-current)"
  printf 'baseline=%s\n' "$(git -C "$ROOT" rev-parse "$BASELINE_BRANCH")"
  printf 'experiment=%s\n' "$(git -C "$ROOT" rev-parse "$EXPERIMENT_BRANCH")"
  systemctl --user is-active "$GATEWAY_SERVICE" || true
  systemctl --user is-active "$WEB_SERVICE" || true
}

case "$ACTION" in
  status)
    status
    exit 0
    ;;
  baseline)
    TARGET="$BASELINE_BRANCH"
    ;;
  experiment)
    TARGET="$EXPERIMENT_BRANCH"
    ;;
  *)
    echo "usage: $0 status|baseline|experiment" >&2
    exit 2
    ;;
esac

if [[ -n "$(git -C "$ROOT" status --porcelain)" ]]; then
  echo "refusing strategy switch: workspace has uncommitted changes" >&2
  git -C "$ROOT" status --short >&2
  exit 1
fi

if [[ -f "$AGENT_RUNTIME_POINTER" ]]; then
  echo "refusing strategy branch switch while Codex Agent scheduling is active" >&2
  echo "run: $ROOT/.venv/bin/python $ROOT/scripts/switch_agent_runtime.py rollback" >&2
  exit 1
fi

gateway_was_active=0
if systemctl --user is-active --quiet "$GATEWAY_SERVICE"; then
  gateway_was_active=1
  systemctl --user stop "$GATEWAY_SERVICE"
fi

restore_gateway() {
  if [[ "$gateway_was_active" == "1" ]]; then
    systemctl --user start "$GATEWAY_SERVICE"
  fi
}
trap restore_gateway EXIT

git -C "$ROOT" switch "$TARGET"

# Remove stale membership from the strategy that was just paused.  Both
# branches support this command; it rebuilds only the derived candidate board
# and does not touch positions, orders, cash, prices or live trade intents.
"$ROOT/.venv/bin/python" "$ROOT/scripts/refresh_candidate_board.py" --allow-closed
systemctl --user restart "$WEB_SERVICE"

status
