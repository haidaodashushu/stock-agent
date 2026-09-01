#!/usr/bin/env python3
"""Refresh current DB trading state without constructing an AI prompt payload."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.market_calendar import is_actionable_trading_time, market_day  # noqa: E402
from data.trading_state import refresh_trading_state  # noqa: E402


def refresh(stage: str, mode: str, *, allow_closed: bool = False) -> str:
    now = datetime.now()
    market = market_day(now.date())
    if not allow_closed and not is_actionable_trading_time(now):
        reason = market.reason if not market.is_open else "outside actionable trading window"
        raise RuntimeError(f"SKIP:{reason}")
    state = refresh_trading_state(stage=stage, mode=mode)
    return str(state["as_of"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh the DB state for one AI trading decision")
    parser.add_argument("--stage", default=datetime.now().strftime("%H%M"))
    parser.add_argument("--mode", required=True, choices=("simulated", "live"))
    parser.add_argument("--allow-closed", action="store_true")
    args = parser.parse_args()
    try:
        print(refresh(args.stage, args.mode, allow_closed=args.allow_closed))
        return 0
    except RuntimeError as exc:
        message = str(exc)
        if message.startswith("SKIP:"):
            print(message.removeprefix("SKIP:"))
            return 3
        print(f"database refresh failed: {message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"database refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
