#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROXY_ENV_FILE="${STOCK_PROXY_ENV_FILE:-$HOME/.proxy_env}"
if [[ -r "$PROXY_ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$PROXY_ENV_FILE"
fi

cd "$ROOT"
exec "$ROOT/.venv/bin/python" scripts/feishu_message_listener.py
