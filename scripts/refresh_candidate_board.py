#!/usr/bin/env python3
"""Materialize the versioned active candidate board independently of trading."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.candidate_board import refresh_candidate_board  # noqa: E402
from data.market_calendar import market_day  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="独立刷新正式候选池")
    parser.add_argument("--allow-closed", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    now = datetime.now()
    market = market_day(now.date())
    if not args.allow_closed and not market.is_open:
        return 0
    try:
        result = refresh_candidate_board(now=now)
    except Exception as exc:
        print(f"candidate board refresh failed: {exc}", file=sys.stderr)
        return 1
    if result.get("status") != "unchanged":
        print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
        from data import opportunity_trial as trial
        from data.market_calendar import is_actionable_trading_time
        if trial.enabled() and is_actionable_trading_time(now):
            subprocess.run([sys.executable,str(ROOT/"scripts/run_opportunity_monitor.py"),"--wake"],check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
