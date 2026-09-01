#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
python -m uvicorn web.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8899}" --log-level info
